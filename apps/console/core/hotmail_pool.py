"""Hotmail/Outlook credential pool with persistent alias lifecycle state.

Credential file format::

    email----password----client_id----refresh_token

The state file is intentionally separate from the credential file.  It is shared
between the Console process and registration workers so aliases cannot be handed
out twice when more than one worker is running.
"""
from __future__ import annotations

import email
import html
import imaplib
import json
import os
import re
import secrets
import string
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from curl_cffi import requests

TOKEN_ENDPOINTS = (
    (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    (
        "https://login.live.com/oauth20_token.srf",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    ("https://login.live.com/oauth20_token.srf", {}),
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_credential_line(line: str) -> dict[str, str] | None:
    parts = line.rstrip("\r\n").split("----", 3)
    if len(parts) != 4:
        return None
    address, password, client_id, refresh_token = (part.strip() for part in parts)
    if not address or "@" not in address or not client_id or not refresh_token:
        return None
    return {
        "email": address,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
    }


def load_credentials(path: str | Path) -> list[dict[str, str]]:
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Hotmail 凭证文件不存在: {source}")
    accounts: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in source.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        account = parse_credential_line(raw)
        if not account:
            continue
        key = account["email"].lower()
        if key not in seen:
            seen.add(key)
            accounts.append(account)
    if not accounts:
        raise ValueError("Hotmail 凭证文件中没有有效四段记录")
    return accounts


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return str(value or "")


def _message_body(message: Any) -> str:
    def decode_part(part: Any) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")

    if not message.is_multipart():
        return decode_part(message)
    text_body = ""
    html_body = ""
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not text_body:
            text_body = decode_part(part)
        elif part.get_content_type() == "text/html" and not html_body:
            html_body = decode_part(part)
    if text_body:
        return text_body
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html_body))).strip()


def extract_verification_code(text: str, subject: str = "") -> str:
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.I)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.I)
    if match:
        return match.group(1)
    for pattern in (
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
        r"(?<!\d)(\d{6})(?!\d)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return ""


class HotmailPool:
    def __init__(
        self,
        credentials_path: str,
        *,
        state_path: str = "",
        max_aliases: int = 5,
        alias_mode: str = "random",
        alias_length: int = 8,
        poll_interval: float = 5,
        recent_seconds: int = 900,
        imap_last_n: int = 30,
        imap_hosts: list[str] | None = None,
        require_recipient_match: bool = True,
        proxy: str = "",
        reservation_ttl_seconds: int = 1800,
        stop_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path).expanduser().resolve()
        self.state_path = (
            Path(state_path).expanduser().resolve()
            if state_path
            else self.credentials_path.with_suffix(".state.json")
        )
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.max_aliases = max(1, int(max_aliases))
        self.alias_mode = str(alias_mode or "random").lower()
        self.alias_length = max(1, min(int(alias_length), 24))
        self.poll_interval = max(1.0, float(poll_interval))
        self.recent_seconds = max(60, int(recent_seconds))
        self.imap_last_n = max(1, int(imap_last_n))
        self.imap_hosts = imap_hosts or ["outlook.office365.com", "imap-mail.outlook.com"]
        self.require_recipient_match = bool(require_recipient_match)
        self.proxy = proxy
        self.reservation_ttl_seconds = max(60, int(reservation_ttl_seconds))
        self.stop_event = stop_event
        self.log = log or (lambda _: None)
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._selected: dict[str, dict[str, str]] = {}

    def account_count(self) -> int:
        return len(load_credentials(self.credentials_path))

    @contextmanager
    def _file_lock(self, timeout: float = 5.0) -> Iterator[None]:
        """Small dependency-free inter-process lock for the JSON state file."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode("ascii"))
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Hotmail 状态文件正被其他进程占用")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state must be an object")
        except Exception:
            payload = {}
        payload.setdefault("version", 2)
        payload.setdefault("consumed", [])
        payload.setdefault("reservations", {})
        payload.setdefault("verifications", {})
        payload.setdefault("accounts", {})
        return payload

    def _save_state(self, state: dict[str, Any]) -> None:
        state["version"] = 2
        state["updated_at"] = _now()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=".hotmail-", suffix=".tmp", dir=str(self.state_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _prune_reservations(self, state: dict[str, Any]) -> None:
        reservations = state.get("reservations") or {}
        now_ts = time.time()
        expired = []
        for alias, item in reservations.items():
            try:
                expires_at = float((item or {}).get("expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at and expires_at <= now_ts:
                expired.append(alias)
        for alias in expired:
            reservations.pop(alias, None)
            verification = (state.get("verifications") or {}).get(alias)
            if isinstance(verification, dict) and verification.get("status") in {
                "reserved",
                "waiting",
            }:
                verification.update(
                    {"status": "released", "last_error": "reservation expired", "updated_at": _now()}
                )

    @staticmethod
    def _is_alias(address: str, main: str) -> bool:
        try:
            local, domain = address.lower().rsplit("@", 1)
            main_local, main_domain = main.lower().rsplit("@", 1)
        except ValueError:
            return False
        return domain == main_domain and (local == main_local or local.startswith(main_local + "+"))

    def _candidate(self, main: str, unavailable: set[str]) -> str:
        if main.lower() not in unavailable:
            return main
        local, domain = main.rsplit("@", 1)
        if self.alias_mode == "sequential":
            for index in range(1, self.max_aliases):
                alias = f"{local}+{index}@{domain}"
                if alias.lower() not in unavailable:
                    return alias
            return ""
        alphabet = string.ascii_lowercase + string.digits
        for _ in range(200):
            suffix = "".join(secrets.choice(alphabet) for _ in range(self.alias_length))
            alias = f"{local}+{suffix}@{domain}"
            if alias.lower() not in unavailable:
                return alias
        return ""

    def acquire(self, *, owner: str = "registration") -> tuple[str, dict[str, str]]:
        with self._lock, self._file_lock():
            state = self._state()
            self._prune_reservations(state)
            consumed = {str(value).lower() for value in state.get("consumed", [])}
            reservations = state.get("reservations") or {}
            reserved = {str(value).lower() for value in reservations}
            unavailable = consumed | reserved
            for account in load_credentials(self.credentials_path):
                used = sum(self._is_alias(value, account["email"]) for value in unavailable)
                if used >= self.max_aliases:
                    continue
                alias = self._candidate(account["email"], unavailable)
                if not alias:
                    continue
                key = alias.lower()
                reservations[key] = {
                    "alias": alias,
                    "main_email": account["email"],
                    "owner": str(owner or "registration"),
                    "reserved_at": _now(),
                    "expires_at": time.time() + self.reservation_ttl_seconds,
                }
                verifications = state.get("verifications") or {}
                verifications[key] = {
                    "alias": alias,
                    "main_email": account["email"],
                    "status": "reserved",
                    "attempts": 0,
                    "updated_at": _now(),
                }
                state["reservations"] = reservations
                state["verifications"] = verifications
                self._save_state(state)
                self._selected[key] = dict(account)
                return alias, dict(account)
        raise RuntimeError("Hotmail/Outlook 主邮箱及 alias 已耗尽")

    def release(self, alias: str, *, consumed: bool) -> None:
        key = str(alias or "").lower()
        if not key:
            return
        with self._lock, self._file_lock():
            state = self._state()
            reservations = state.get("reservations") or {}
            reservations.pop(key, None)
            values = {str(value).lower() for value in state.get("consumed", [])}
            if consumed:
                values.add(key)
            verification = (state.get("verifications") or {}).get(key)
            if isinstance(verification, dict):
                if consumed and verification.get("status") not in {"received", "timeout", "error"}:
                    verification["status"] = "used"
                elif not consumed and verification.get("status") in {"reserved", "waiting"}:
                    verification["status"] = "released"
                verification["updated_at"] = _now()
            state["reservations"] = reservations
            state["consumed"] = sorted(values)
            self._save_state(state)
            self._selected.pop(key, None)

    def _set_verification(self, alias: str, **values: Any) -> None:
        key = str(alias or "").lower()
        if not key:
            return
        with self._lock, self._file_lock():
            state = self._state()
            verifications = state.get("verifications") or {}
            current = verifications.get(key) if isinstance(verifications.get(key), dict) else {}
            current.update(values)
            current.setdefault("alias", alias)
            current["updated_at"] = _now()
            verifications[key] = current
            state["verifications"] = verifications
            self._save_state(state)

    def verification_status(self, alias: str = "") -> dict[str, Any]:
        with self._lock, self._file_lock():
            state = self._state()
            self._prune_reservations(state)
            self._save_state(state)
            entries = list((state.get("verifications") or {}).values())
        if alias:
            key = alias.lower()
            entry = next((item for item in entries if str(item.get("alias") or "").lower() == key), None)
            return {"item": entry}
        entries.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"items": entries}

    def snapshot(self) -> dict[str, Any]:
        accounts = load_credentials(self.credentials_path)
        with self._lock, self._file_lock():
            state = self._state()
            self._prune_reservations(state)
            self._save_state(state)
        consumed = {str(value).lower() for value in state.get("consumed", [])}
        reservations = {str(value).lower(): item for value, item in (state.get("reservations") or {}).items()}
        health = state.get("accounts") or {}
        rows = []
        for account in accounts:
            main = account["email"]
            used_aliases = sorted(value for value in consumed if self._is_alias(value, main))
            reserved_aliases = sorted(value for value in reservations if self._is_alias(value, main))
            used = len(used_aliases)
            reserved = len(reserved_aliases)
            rows.append(
                {
                    "email": main,
                    "healthy": (health.get(main.lower()) or {}).get("healthy"),
                    "last_probe_at": (health.get(main.lower()) or {}).get("last_probe_at", ""),
                    "last_error": (health.get(main.lower()) or {}).get("last_error", ""),
                    "used": used,
                    "reserved": reserved,
                    "remaining": max(0, self.max_aliases - used - reserved),
                    "used_aliases": used_aliases,
                    "reserved_aliases": reserved_aliases,
                }
            )
        return {
            "accounts": rows,
            "summary": {
                "main_accounts": len(rows),
                "capacity": len(rows) * self.max_aliases,
                "used": len(consumed),
                "reserved": len(reservations),
                "available": sum(item["remaining"] for item in rows),
                "healthy": sum(item["healthy"] is True for item in rows),
                "unhealthy": sum(item["healthy"] is False for item in rows),
            },
            "updated_at": state.get("updated_at", ""),
        }

    def export_credential(self, email: str) -> str:
        """Return one current credential in the canonical four-part format.

        The status snapshot intentionally excludes secrets.  Callers use this
        on demand after authentication so passwords and refresh tokens are not
        included in the three-second status polling response.
        """
        target = str(email or "").strip().lower()
        if not target:
            raise KeyError("email is required")
        with self._lock, self._file_lock():
            account = next(
                (
                    item
                    for item in load_credentials(self.credentials_path)
                    if item["email"].lower() == target
                ),
                None,
            )
        if not account:
            raise KeyError("account not found")
        return (
            f"{account['email']}----{account['password']}----"
            f"{account['client_id']}----{account['refresh_token']}"
        )

    def _update_refresh_token(self, account: dict[str, str], new_token: str) -> None:
        if not new_token or new_token == account.get("refresh_token"):
            return
        with self._lock:
            lines = self.credentials_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
            output: list[str] = []
            for raw in lines:
                item = parse_credential_line(raw)
                if item and item["email"].lower() == account["email"].lower():
                    output.append(
                        f"{item['email']}----{item['password']}----{item['client_id']}----{new_token}\n"
                    )
                else:
                    output.append(raw)
            temp = self.credentials_path.with_suffix(self.credentials_path.suffix + ".tmp")
            temp.write_text("".join(output), encoding="utf-8")
            os.replace(temp, self.credentials_path)
            account["refresh_token"] = new_token
            self.log(f"[Hotmail] refresh_token 已轮换回写: {account['email']}")

    def refresh_access_token(self, account: dict[str, str]) -> str:
        key = account["email"].lower()
        with self._lock:
            lock = self._refresh_locks.setdefault(key, threading.Lock())
        with lock:
            last_error = ""
            for url, extra in TOKEN_ENDPOINTS:
                try:
                    response = requests.post(
                        url,
                        data={
                            "client_id": account["client_id"],
                            "refresh_token": account["refresh_token"],
                            "grant_type": "refresh_token",
                            **extra,
                        },
                        proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                        timeout=30,
                    )
                    payload = response.json() if response.content else {}
                    access_token = str(payload.get("access_token") or "")
                    if access_token:
                        self._update_refresh_token(account, str(payload.get("refresh_token") or ""))
                        return access_token
                    last_error = str(
                        payload.get("error_description") or payload.get("error") or response.text[:200]
                    )
                except Exception as exc:
                    last_error = str(exc)
            raise RuntimeError(f"Hotmail OAuth2 refresh 失败: {last_error}")

    @staticmethod
    def _imap_auth(account: dict[str, str], access_token: str) -> bytes:
        return f"user={account['email']}\x01auth=Bearer {access_token}\x01\x01".encode()

    @staticmethod
    def _imap_scan_folders(client: imaplib.IMAP4_SSL) -> list[tuple[str, str]]:
        """Return INBOX plus any server-advertised Junk/Spam folders.

        Outlook normally advertises its junk folder with the ``\\Junk``
        special-use flag.  Keep the raw mailbox token returned by LIST so a
        folder containing spaces remains correctly quoted when passed back to
        SELECT.
        """
        folders: list[tuple[str, str]] = [("INBOX", "INBOX")]
        try:
            status, rows = client.list()
        except Exception:
            return folders
        if status != "OK" or not rows:
            return folders
        for raw in rows:
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            match = re.match(
                r'^\((?P<flags>[^)]*)\)\s+(?:NIL|"(?:\\.|[^"])*")\s+(?P<mailbox>.+)$',
                text.strip(),
            )
            if not match:
                continue
            flags = match.group("flags").lower()
            mailbox = match.group("mailbox").strip()
            label = mailbox.strip('"').replace(r'\"', '"')
            if "\\junk" not in flags and label.lower() not in {
                "junk",
                "junk email",
                "junk e-mail",
                "spam",
            }:
                continue
            if all(existing[1].lower() != mailbox.lower() for existing in folders):
                folders.append((label, mailbox))
        return folders

    def _scan_host(self, account: dict[str, str], alias: str, access_token: str, host: str) -> str:
        client = imaplib.IMAP4_SSL(host, 993, timeout=45)
        client.authenticate("XOAUTH2", lambda _: self._imap_auth(account, access_token))
        try:
            cutoff = time.time() - self.recent_seconds
            for folder_label, mailbox in self._imap_scan_folders(client):
                status, _ = client.select(mailbox, readonly=True)
                if status != "OK":
                    continue
                status, data = client.search(None, "ALL")
                if status != "OK" or not data or not data[0]:
                    continue
                for message_id in reversed(data[0].split()[-self.imap_last_n :]):
                    _, message_data = client.fetch(message_id, "(RFC822)")
                    if not message_data or not isinstance(message_data[0], tuple):
                        continue
                    message = email.message_from_bytes(message_data[0][1])
                    date_value = message.get("Date")
                    if date_value:
                        try:
                            sent_at = parsedate_to_datetime(date_value)
                            if sent_at.tzinfo is None:
                                sent_at = sent_at.replace(tzinfo=timezone.utc)
                            if sent_at.timestamp() < cutoff:
                                continue
                        except Exception:
                            pass
                    subject = _decode_header(message.get("Subject", ""))
                    sender = _decode_header(message.get("From", ""))
                    recipients = " ".join(
                        _decode_header(message.get(name, ""))
                        for name in (
                            "To",
                            "Cc",
                            "Delivered-To",
                            "X-Original-To",
                            "Original-Recipient",
                            "Envelope-To",
                            "X-Envelope-To",
                            "X-MS-Exchange-Organization-OriginalEnvelopeRecipients",
                        )
                    ).lower()
                    if self.require_recipient_match and alias.lower() not in recipients:
                        continue
                    body = _message_body(message)
                    combined = f"{subject}\n{sender}\n{recipients}\n{body}"
                    if not any(
                        word in combined.lower()
                        for word in ("x.ai", "xai", "grok", "verification", "code", "验证码")
                    ):
                        continue
                    code = extract_verification_code(combined, subject)
                    if code:
                        self.log(f"[Hotmail] 已从 {folder_label} 获取验证码: {alias}")
                        return code
            return ""
        finally:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass

    def wait_for_code(self, alias: str, account: dict[str, str], timeout: int = 180) -> str:
        deadline = time.time() + timeout
        access_token = ""
        attempts = 0
        self._set_verification(
            alias,
            main_email=account.get("email", ""),
            status="waiting",
            requested_at=_now(),
            timeout_seconds=int(timeout),
            last_error="",
        )
        self.log(f"[Hotmail] 开始轮询验证码: {alias}，超时={int(timeout)}s")
        try:
            while time.time() < deadline:
                if self.stop_event and self.stop_event.is_set():
                    raise RuntimeError("Task stopped by user")
                attempts += 1
                self._set_verification(alias, status="waiting", attempts=attempts)
                try:
                    if not access_token:
                        access_token = self.refresh_access_token(account)
                    errors: list[str] = []
                    for host in self.imap_hosts:
                        try:
                            code = self._scan_host(account, alias, access_token, host)
                            if code:
                                self._set_verification(
                                    alias,
                                    status="received",
                                    attempts=attempts,
                                    code_received_at=_now(),
                                    last_error="",
                                )
                                self.release(alias, consumed=True)
                                return code
                            break
                        except Exception as exc:
                            errors.append(f"{host}: {exc}")
                    if errors and len(errors) >= len(self.imap_hosts):
                        raise RuntimeError("; ".join(errors))
                    self.log(
                        f"[Hotmail] 第 {attempts} 次轮询未发现验证码: {alias}，"
                        f"{self.poll_interval:g}s 后重试"
                    )
                except Exception as exc:
                    access_token = ""
                    self._set_verification(alias, status="waiting", attempts=attempts, last_error=str(exc)[:300])
                    self.log(f"[Hotmail] 本轮取码失败: {exc}")
                if self.stop_event:
                    if self.stop_event.wait(self.poll_interval):
                        raise RuntimeError("Task stopped by user")
                else:
                    time.sleep(self.poll_interval)
            self._set_verification(
                alias,
                status="timeout",
                attempts=attempts,
                last_error=f"{timeout}s 内未收到验证码",
            )
            raise TimeoutError(f"Hotmail/Outlook 在 {timeout}s 内未收到验证码: {alias}")
        except Exception as exc:
            current = self.verification_status(alias).get("item") or {}
            if current.get("status") not in {"timeout", "received"}:
                self._set_verification(alias, status="error", attempts=attempts, last_error=str(exc)[:300])
            self.release(alias, consumed=True)
            raise

    def _probe_account(self, account: dict[str, str]) -> dict[str, Any]:
        access_token = self.refresh_access_token(account)
        errors: list[str] = []
        for host in self.imap_hosts:
            try:
                client = imaplib.IMAP4_SSL(host, 993, timeout=30)
                client.authenticate("XOAUTH2", lambda _: self._imap_auth(account, access_token))
                client.logout()
                return {"ok": True, "email": account["email"], "host": host, "message": "XOAUTH2 IMAP 登录成功"}
            except Exception as exc:
                errors.append(f"{host}: {exc}")
        return {"ok": False, "email": account["email"], "error": "; ".join(errors)}

    def probe_accounts(self, *, workers: int = 4, limit: int = 0) -> dict[str, Any]:
        accounts = load_credentials(self.credentials_path)
        if limit > 0:
            accounts = accounts[:limit]
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(max(1, workers), 12)) as executor:
            futures = {executor.submit(self._probe_account, account): account for account in accounts}
            for future in as_completed(futures):
                account = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "email": account["email"], "error": str(exc)}
                results.append(result)
        checked_at = _now()
        with self._lock, self._file_lock():
            state = self._state()
            health = state.get("accounts") or {}
            for result in results:
                health[str(result.get("email") or "").lower()] = {
                    "healthy": bool(result.get("ok")),
                    "last_probe_at": checked_at,
                    "last_error": str(result.get("error") or "")[:300],
                    "host": str(result.get("host") or ""),
                }
            state["accounts"] = health
            self._save_state(state)
        results.sort(key=lambda item: str(item.get("email") or ""))
        return {
            "ok": all(item.get("ok") for item in results),
            "total": len(results),
            "healthy": sum(bool(item.get("ok")) for item in results),
            "failed": sum(not bool(item.get("ok")) for item in results),
            "items": results,
            "checked_at": checked_at,
        }

    def delete_used_accounts(self) -> dict[str, int]:
        accounts = load_credentials(self.credentials_path)
        with self._lock, self._file_lock():
            state = self._state()
            consumed = {str(value).lower() for value in state.get("consumed", [])}
            exhausted = {
                account["email"].lower()
                for account in accounts
                if sum(self._is_alias(value, account["email"]) for value in consumed)
                >= self.max_aliases
            }
            if not exhausted:
                return {"deleted": 0, "remaining": len(accounts)}
            remaining = [account for account in accounts if account["email"].lower() not in exhausted]
            payload = "".join(
                f"{item['email']}----{item['password']}----{item['client_id']}----{item['refresh_token']}\n"
                for item in remaining
            )
            temp = self.credentials_path.with_suffix(self.credentials_path.suffix + ".tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.credentials_path)
            state["consumed"] = sorted(
                value
                for value in consumed
                if not any(self._is_alias(value, main) for main in exhausted)
            )
            state["reservations"] = {
                key: value
                for key, value in (state.get("reservations") or {}).items()
                if str((value or {}).get("main_email") or "").lower() not in exhausted
            }
            state["verifications"] = {
                key: value
                for key, value in (state.get("verifications") or {}).items()
                if str((value or {}).get("main_email") or "").lower() not in exhausted
            }
            state["accounts"] = {
                key: value for key, value in (state.get("accounts") or {}).items() if key not in exhausted
            }
            self._save_state(state)
        return {"deleted": len(exhausted), "remaining": len(remaining)}

    def test(self) -> dict[str, Any]:
        result = self.probe_accounts(workers=1, limit=1)
        first = result.get("items", [{}])[0] if result.get("items") else {}
        return {
            "ok": bool(first.get("ok")),
            "host": first.get("host", ""),
            "accounts": self.account_count(),
            "message": first.get("message") or first.get("error") or "",
            "error": first.get("error", ""),
        }

"""Hotmail/Outlook four-field credential pool with XOAUTH2 IMAP."""
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
from datetime import timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

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
        stop_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path).expanduser().resolve()
        self.state_path = Path(state_path).expanduser().resolve() if state_path else self.credentials_path.with_suffix(".state.json")
        self.max_aliases = max(1, int(max_aliases))
        self.alias_mode = str(alias_mode or "random").lower()
        self.alias_length = max(1, min(int(alias_length), 24))
        self.poll_interval = max(1.0, float(poll_interval))
        self.recent_seconds = max(60, int(recent_seconds))
        self.imap_last_n = max(1, int(imap_last_n))
        self.imap_hosts = imap_hosts or ["outlook.office365.com", "imap-mail.outlook.com"]
        self.require_recipient_match = bool(require_recipient_match)
        self.proxy = proxy
        self.stop_event = stop_event
        self.log = log or (lambda _: None)
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._reserved: set[str] = set()
        self._selected: dict[str, dict[str, str]] = {}

    def account_count(self) -> int:
        return len(load_credentials(self.credentials_path))

    def _state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {"consumed": []}
        except Exception:
            return {"consumed": []}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".hotmail-", suffix=".tmp", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _is_alias(address: str, main: str) -> bool:
        try:
            local, domain = address.lower().rsplit("@", 1)
            main_local, main_domain = main.lower().rsplit("@", 1)
        except ValueError:
            return False
        return domain == main_domain and (local == main_local or local.startswith(main_local + "+"))

    def _candidate(self, main: str, consumed: set[str]) -> str:
        if main.lower() not in consumed and main.lower() not in self._reserved:
            return main
        local, domain = main.rsplit("@", 1)
        if self.alias_mode == "sequential":
            for index in range(1, self.max_aliases):
                alias = f"{local}+{index}@{domain}"
                if alias.lower() not in consumed and alias.lower() not in self._reserved:
                    return alias
            return ""
        alphabet = string.ascii_lowercase + string.digits
        for _ in range(200):
            suffix = "".join(secrets.choice(alphabet) for _ in range(self.alias_length))
            alias = f"{local}+{suffix}@{domain}"
            if alias.lower() not in consumed and alias.lower() not in self._reserved:
                return alias
        return ""

    def acquire(self) -> tuple[str, dict[str, str]]:
        with self._lock:
            state = self._state()
            consumed = {str(value).lower() for value in state.get("consumed", [])}
            for account in load_credentials(self.credentials_path):
                used = sum(self._is_alias(value, account["email"]) for value in consumed | self._reserved)
                if used >= self.max_aliases:
                    continue
                alias = self._candidate(account["email"], consumed)
                if alias:
                    key = alias.lower()
                    self._reserved.add(key)
                    self._selected[key] = account
                    return alias, dict(account)
        raise RuntimeError("Hotmail/Outlook 主邮箱及 alias 已耗尽")

    def release(self, alias: str, *, consumed: bool) -> None:
        key = str(alias or "").lower()
        with self._lock:
            self._reserved.discard(key)
            self._selected.pop(key, None)
            if consumed and key:
                state = self._state()
                values = {str(value).lower() for value in state.get("consumed", [])}
                values.add(key)
                state["consumed"] = sorted(values)
                self._save_state(state)

    def _update_refresh_token(self, account: dict[str, str], new_token: str) -> None:
        if not new_token or new_token == account.get("refresh_token"):
            return
        with self._lock:
            lines = self.credentials_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
            output: list[str] = []
            for raw in lines:
                item = parse_credential_line(raw)
                if item and item["email"].lower() == account["email"].lower():
                    output.append(f"{item['email']}----{item['password']}----{item['client_id']}----{new_token}\n")
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
                    last_error = str(payload.get("error_description") or payload.get("error") or response.text[:200])
                except Exception as exc:
                    last_error = str(exc)
            raise RuntimeError(f"Hotmail OAuth2 refresh 失败: {last_error}")

    def _scan_host(self, account: dict[str, str], alias: str, access_token: str, host: str) -> str:
        client = imaplib.IMAP4_SSL(host, 993, timeout=45)
        auth = f"user={account['email']}\x01auth=Bearer {access_token}\x01\x01"
        client.authenticate("XOAUTH2", lambda _: auth.encode())
        try:
            client.select("INBOX")
            status, data = client.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return ""
            cutoff = time.time() - self.recent_seconds
            for message_id in reversed(data[0].split()[-self.imap_last_n:]):
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
                    for name in ("To", "Cc", "Delivered-To", "X-Original-To", "Original-Recipient", "Envelope-To")
                ).lower()
                if self.require_recipient_match and alias.lower() not in recipients:
                    continue
                body = _message_body(message)
                combined = f"{subject}\n{sender}\n{recipients}\n{body}"
                if not any(word in combined.lower() for word in ("x.ai", "xai", "grok", "verification", "code", "验证码")):
                    continue
                code = extract_verification_code(combined, subject)
                if code:
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
        try:
            while time.time() < deadline:
                if self.stop_event and self.stop_event.is_set():
                    raise RuntimeError("Task stopped by user")
                try:
                    if not access_token:
                        access_token = self.refresh_access_token(account)
                    errors: list[str] = []
                    for host in self.imap_hosts:
                        try:
                            code = self._scan_host(account, alias, access_token, host)
                            if code:
                                self.release(alias, consumed=True)
                                return code
                            break
                        except Exception as exc:
                            errors.append(f"{host}: {exc}")
                    if errors and len(errors) >= len(self.imap_hosts):
                        raise RuntimeError("; ".join(errors))
                except Exception as exc:
                    access_token = ""
                    self.log(f"[Hotmail] 本轮取码失败: {exc}")
                if self.stop_event:
                    if self.stop_event.wait(self.poll_interval):
                        raise RuntimeError("Task stopped by user")
                else:
                    time.sleep(self.poll_interval)
            raise TimeoutError(f"Hotmail/Outlook 在 {timeout}s 内未收到验证码: {alias}")
        except Exception:
            self.release(alias, consumed=True)
            raise

    def test(self) -> dict[str, Any]:
        account = load_credentials(self.credentials_path)[0]
        access_token = self.refresh_access_token(account)
        host_errors: list[str] = []
        for host in self.imap_hosts:
            try:
                client = imaplib.IMAP4_SSL(host, 993, timeout=30)
                auth = f"user={account['email']}\x01auth=Bearer {access_token}\x01\x01"
                client.authenticate("XOAUTH2", lambda _: auth.encode())
                client.logout()
                return {
                    "ok": True,
                    "host": host,
                    "accounts": self.account_count(),
                    "message": f"XOAUTH2 IMAP 登录成功: {host}",
                }
            except Exception as exc:
                host_errors.append(f"{host}: {exc}")
        error = "; ".join(host_errors)
        return {"ok": False, "accounts": self.account_count(), "error": error, "message": error}

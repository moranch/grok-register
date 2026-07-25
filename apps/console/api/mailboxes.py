"""
邮箱 Provider 路由（覆盖旧实现，等价于 app.py 原 `/api/mailboxes/*` 端点）。

- GET    /api/mailboxes
- POST   /api/mailboxes
- PATCH  /api/mailboxes/{id}
- DELETE /api/mailboxes/{id}
- POST   /api/mailboxes/import-default
- POST   /api/mailboxes/{id}/test
- GET    /api/mailboxes/{id}/domains
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ._shared import (
    MailboxItem,
    MailboxUpdate,
    _request_with_optional_proxy,
    check_auth,
    fetch_one,
    mailbox_add,
    mailbox_delete,
    mailbox_list,
    mailbox_update,
    merged_defaults,
    now_iso,
    seed_mailbox_from_defaults,
)

router = APIRouter(prefix="/api/mailboxes", tags=["mailboxes"])


class HotmailImportRequest(BaseModel):
    name: str = Field("Hotmail / Outlook", min_length=1, max_length=120)
    credentials: str = Field(..., min_length=1)
    provider_type: str = Field("hotmail", pattern="^(hotmail|outlookmail)$")
    alias_mode: str = Field("random", pattern="^(random|sequential)$")
    max_aliases: int = Field(5, ge=1, le=100)
    alias_length: int = Field(8, ge=1, le=24)
    poll_interval: float = Field(5, ge=1, le=60)
    recent_seconds: int = Field(900, ge=60, le=86400)
    imap_last_n: int = Field(30, ge=1, le=500)
    imap_hosts: str = "outlook.office365.com,imap-mail.outlook.com"
    require_recipient_match: bool = True
    reservation_ttl_seconds: int = Field(1800, ge=60, le=86400)
    replace: bool = False


class HotmailProbeRequest(BaseModel):
    workers: int = Field(4, ge=1, le=12)
    limit: int = Field(0, ge=0, le=10000)


class HotmailReserveRequest(BaseModel):
    owner: str = Field("console", min_length=1, max_length=120)


class HotmailReleaseRequest(BaseModel):
    alias: str = Field(..., min_length=3, max_length=320)
    consumed: bool = False


def _hotmail_row(mbox_id: int):
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    return row


def _hotmail_pool_from_row(row: Any):
    if row["provider_type"] not in {"hotmail", "outlookmail"}:
        raise HTTPException(status_code=400, detail="该 Provider 不是 Hotmail/Outlook 类型")
    import json
    from core.hotmail_pool import HotmailPool

    try:
        config = json.loads(row["config_json"] or "{}")
    except Exception:
        config = {}
    hosts = [
        part.strip()
        for part in str(config.get("imap_hosts") or "").replace("，", ",").split(",")
        if part.strip()
    ]
    return HotmailPool(
        row["api_base"],
        state_path=str(config.get("state_path") or ""),
        max_aliases=int(config.get("max_aliases", 5) or 5),
        alias_mode=str(config.get("alias_mode") or "random"),
        alias_length=int(config.get("alias_length", 8) or 8),
        poll_interval=float(config.get("poll_interval", 5) or 5),
        recent_seconds=int(config.get("recent_seconds", 900) or 900),
        imap_last_n=int(config.get("imap_last_n", 30) or 30),
        imap_hosts=hosts or None,
        require_recipient_match=bool(config.get("require_recipient_match", True)),
        reservation_ttl_seconds=int(config.get("reservation_ttl_seconds", 1800) or 1800),
        proxy=str(merged_defaults().get("proxy") or ""),
    )


@router.get("")
def api_list_mailboxes(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"mailboxes": mailbox_list()}


@router.post("")
def api_add_mailbox(request: Request, payload: MailboxItem) -> dict[str, Any]:
    check_auth(request)
    return {"mailbox": mailbox_add(payload)}


@router.post("/hotmail/import")
def api_import_hotmail(request: Request, payload: HotmailImportRequest) -> dict[str, Any]:
    """导入 邮箱----密码----ClientID----refresh_token 四段凭证池。"""
    check_auth(request)
    from core.hotmail_pool import load_credentials, parse_credential_line

    imported_by_email: dict[str, dict[str, str]] = {}
    invalid = 0
    for raw in payload.credentials.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        item = parse_credential_line(raw)
        if not item:
            invalid += 1
            continue
        imported_by_email[item["email"].lower()] = item
    if not imported_by_email:
        raise HTTPException(status_code=400, detail="没有有效四段凭证")

    base_dir = Path(os.getenv("GROK_HOTMAIL_DATA_DIR", "/workspace/runtime/hotmail")).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", payload.name).strip("-") or "hotmail"
    path = base_dir / f"{safe_name}.txt"
    merged_by_email: dict[str, dict[str, str]] = {}
    if path.exists() and not payload.replace:
        try:
            merged_by_email.update(
                {item["email"].lower(): item for item in load_credentials(path)}
            )
        except (FileNotFoundError, ValueError):
            pass
    before_count = len(merged_by_email)
    merged_by_email.update(imported_by_email)
    valid_lines = [
        f"{item['email']}----{item['password']}----{item['client_id']}----{item['refresh_token']}"
        for item in merged_by_email.values()
    ]
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text("\n".join(valid_lines) + "\n", encoding="utf-8")
    os.replace(temp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    accounts = load_credentials(path)
    config = {
        "alias_mode": payload.alias_mode,
        "max_aliases": payload.max_aliases,
        "alias_length": payload.alias_length,
        "poll_interval": payload.poll_interval,
        "recent_seconds": payload.recent_seconds,
        "imap_last_n": payload.imap_last_n,
        "imap_hosts": payload.imap_hosts,
        "require_recipient_match": payload.require_recipient_match,
        "reservation_ttl_seconds": payload.reservation_ttl_seconds,
        "state_path": str(path.with_suffix(".state.json")),
    }
    existing = fetch_one(
        "SELECT * FROM mailbox_providers WHERE provider_type IN ('hotmail','outlookmail') "
        "AND (name=? OR api_base=?) ORDER BY id LIMIT 1",
        (payload.name, str(path)),
    )
    if existing:
        mailbox = mailbox_update(
            int(existing["id"]),
            MailboxUpdate(
                name=payload.name,
                provider_type=payload.provider_type,
                api_base=str(path),
                config=config,
                enabled=True,
            ),
        )
    else:
        mailbox = mailbox_add(
            MailboxItem(
                name=payload.name,
                provider_type=payload.provider_type,
                api_base=str(path),
                config=config,
                enabled=True,
            )
        )
    return {
        "ok": True,
        "mailbox": mailbox,
        "imported": len(imported_by_email),
        "added": max(0, len(accounts) - before_count),
        "total": len(accounts),
        "invalid": invalid,
        "format": "email----password----ClientID----refresh_token",
    }


@router.get("/{mbox_id}/hotmail/status")
def api_hotmail_status(request: Request, mbox_id: int) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    return {"ok": True, **pool.snapshot()}


@router.get("/{mbox_id}/hotmail/verifications")
def api_hotmail_verifications(
    request: Request,
    mbox_id: int,
    alias: str = Query("", max_length=320),
) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    return {"ok": True, **pool.verification_status(alias)}


@router.post("/{mbox_id}/hotmail/probe")
def api_hotmail_probe(
    request: Request, mbox_id: int, payload: HotmailProbeRequest
) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    return pool.probe_accounts(workers=payload.workers, limit=payload.limit)


@router.post("/{mbox_id}/hotmail/reserve")
def api_hotmail_reserve(
    request: Request, mbox_id: int, payload: HotmailReserveRequest
) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    alias, account = pool.acquire(owner=payload.owner)
    return {"ok": True, "alias": alias, "main_email": account["email"]}


@router.post("/{mbox_id}/hotmail/release")
def api_hotmail_release(
    request: Request, mbox_id: int, payload: HotmailReleaseRequest
) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    pool.release(payload.alias, consumed=payload.consumed)
    return {"ok": True, "alias": payload.alias, "consumed": payload.consumed}


@router.delete("/{mbox_id}/hotmail/used")
def api_hotmail_delete_used(request: Request, mbox_id: int) -> dict[str, Any]:
    check_auth(request)
    pool = _hotmail_pool_from_row(_hotmail_row(mbox_id))
    return {"ok": True, **pool.delete_used_accounts()}


@router.patch("/{mbox_id}")
def api_update_mailbox(
    request: Request, mbox_id: int, payload: MailboxUpdate
) -> dict[str, Any]:
    check_auth(request)
    return {"mailbox": mailbox_update(mbox_id, payload)}


@router.delete("/{mbox_id}")
def api_delete_mailbox(request: Request, mbox_id: int) -> dict[str, Any]:
    check_auth(request)
    mailbox_delete(mbox_id)
    return {"ok": True}


@router.post("/import-default")
def api_mailboxes_import_default(
    request: Request, force: bool = Query(False)
) -> dict[str, Any]:
    """一键导入系统默认邮箱配置到 Provider 池。"""
    check_auth(request)
    defaults = merged_defaults()
    api_base = str(defaults.get("temp_mail_api_base") or "").strip()
    if not api_base:
        return {
            "ok": False,
            "message": "系统默认配置里没有 temp_mail_api_base，无法导入",
        }
    created = seed_mailbox_from_defaults(force=bool(force))
    if not created:
        return {
            "ok": True,
            "skipped": True,
            "message": "已存在 Provider，跳过导入；如需强制追加，使用 ?force=true",
        }
    return {"ok": True, "skipped": False, "mailbox": created}


@router.post("/{mbox_id}/test")
def api_test_mailbox(request: Request, mbox_id: int) -> dict[str, Any]:
    """对单个邮箱 Provider 做一次可达性检测。"""
    check_auth(request)
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    if row["provider_type"] in {"hotmail", "outlookmail"}:
        try:
            pool = _hotmail_pool_from_row(row)
            result = pool.test()
            return {**result, "checked_at": now_iso()}
        except Exception as exc:
            return {"ok": False, "message": str(exc), "checked_at": now_iso()}
    target = (row["api_base"] or "").strip()
    if not target:
        return {"ok": False, "message": "api_base 未配置"}
    try:
        response = _request_with_optional_proxy(target, timeout=15)
        ok = response.status_code < 500
        return {
            "ok": ok,
            "status_code": response.status_code,
            "message": f"HTTP {response.status_code}",
            "checked_at": now_iso(),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc), "checked_at": now_iso()}


@router.get("/{mbox_id}/domains")
def api_mailbox_domains(request: Request, mbox_id: int) -> dict[str, Any]:
    """拉取某个邮箱 Provider 可用的域名列表。"""
    check_auth(request)
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    if row["provider_type"] in {"hotmail", "outlookmail"}:
        return {
            "ok": True,
            "items": ["outlook.com", "hotmail.com", "live.com"],
            "message": "域名由导入的主邮箱决定，plus alias 保持相同域名",
            "checked_at": now_iso(),
        }
    base = (row["api_base"] or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "items": [], "message": "api_base 未配置"}

    candidates = [
        f"{base}/api/domains",
        f"{base}/domains",
        f"{base}/api/v1/domains",
    ]
    headers: dict[str, str] = {}
    admin_pw = (row["admin_password"] or "").strip()
    if admin_pw:
        headers["Authorization"] = f"Bearer {admin_pw}"
        headers["x-admin-auth"] = admin_pw
    site_pw = (row["site_password"] or "").strip()
    if site_pw:
        headers["x-custom-auth"] = site_pw

    last_error = ""
    for url in candidates:
        try:
            response = _request_with_optional_proxy(
                url, timeout=10, headers=headers if headers else None
            )
            if response.status_code != 200:
                last_error = f"{url} → HTTP {response.status_code}"
                continue
            try:
                data = response.json()
            except Exception:
                last_error = f"{url} → 响应不是 JSON"
                continue
            items: list[str] = []
            if isinstance(data, list):
                for it in data:
                    if isinstance(it, str):
                        items.append(it)
                    elif isinstance(it, dict):
                        items.append(
                            str(it.get("domain") or it.get("name") or "")
                        )
            elif isinstance(data, dict):
                domains = (
                    data.get("hydra:member")
                    or data.get("data")
                    or data.get("results")
                    or data.get("domains")
                    or []
                )
                if isinstance(domains, list):
                    for it in domains:
                        if isinstance(it, str):
                            items.append(it)
                        elif isinstance(it, dict):
                            items.append(
                                str(it.get("domain") or it.get("name") or "")
                            )
            items = [d.strip() for d in items if d and isinstance(d, str)]
            if items:
                return {
                    "ok": True,
                    "items": items,
                    "endpoint": url,
                    "checked_at": now_iso(),
                }
        except Exception as exc:
            last_error = f"{url} → {exc}"

    return {
        "ok": False,
        "items": [],
        "message": last_error or "所有候选端点都拉取失败",
        "checked_at": now_iso(),
    }

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

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

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


@router.get("")
def api_list_mailboxes(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"mailboxes": mailbox_list()}


@router.post("")
def api_add_mailbox(request: Request, payload: MailboxItem) -> dict[str, Any]:
    check_auth(request)
    return {"mailbox": mailbox_add(payload)}


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

"""Sub2API management client used by the post-registration import queue."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from core.cpa_auth import token_to_sub2api_record

_AUTH_CACHE_TTL_SECONDS = 5 * 60
_AUTH_CACHE_LOCK = threading.RLock()
_AUTH_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, str]]] = {}


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text[:420] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error") or detail
        if detail:
            return str(detail)[:420]
    return json.dumps(payload, ensure_ascii=False)[:420]


def _retryable(status_code: int, error: str = "") -> bool:
    if status_code in {408, 425, 429, 500, 502, 503, 504}:
        return True
    lowered = str(error or "").lower()
    return any(
        marker in lowered
        for marker in ("timeout", "timed out", "connection", "network", "temporarily")
    )


def _auth_headers(
    client: httpx.Client,
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "api_key",
    admin_email: str = "",
    admin_password: str = "",
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    base = str(base_url or "").strip().rstrip("/")
    mode = str(auth_mode or "api_key").strip().lower()
    if not base:
        return None, {"ok": False, "retryable": False, "error": "Sub2API 地址未配置"}
    if mode == "api_key":
        key = str(api_key or "").strip()
        if not key:
            return None, {"ok": False, "retryable": False, "error": "Sub2API 管理员 API Key 未配置"}
        return {"x-api-key": key}, None
    if mode != "password":
        return None, {"ok": False, "retryable": False, "error": "不支持的 Sub2API 认证方式"}
    email = str(admin_email or "").strip()
    password = str(admin_password or "")
    if not email or not password:
        return None, {"ok": False, "retryable": False, "error": "Sub2API 管理员邮箱或密码未配置"}
    cache_key = (base, email.lower(), hashlib.sha256(password.encode()).hexdigest())
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return dict(cached[1]), None
        _AUTH_CACHE.pop(cache_key, None)
        response = client.post(
            f"{base}/api/v1/auth/login",
            headers={"Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        if response.status_code >= 300:
            message = f"Sub2API 登录失败: {_error_text(response)}"
            return None, {
                "ok": False,
                "retryable": _retryable(response.status_code, message),
                "status_code": response.status_code,
                "error": message,
            }
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            data = {}
        if data.get("requires_2fa"):
            return None, {
                "ok": False,
                "retryable": False,
                "error": "Sub2API 管理员启用了二次验证，请改用管理员 API Key",
            }
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            return None, {"ok": False, "retryable": False, "error": "Sub2API 登录响应缺少 access_token"}
        headers = {"Authorization": f"Bearer {access_token}"}
        _AUTH_CACHE[cache_key] = (time.monotonic() + _AUTH_CACHE_TTL_SECONDS, headers)
        return dict(headers), None


def _client(timeout: float, verify_tls: bool) -> httpx.Client:
    return httpx.Client(timeout=timeout, verify=verify_tls, follow_redirects=True)


def list_groups(**config: Any) -> dict[str, Any]:
    base = str(config.get("base_url") or "").strip().rstrip("/")
    try:
        with _client(30, bool(config.get("verify_tls", True))) as client:
            headers, error = _auth_headers(client, **_auth_config(config))
            if error:
                return error
            response = client.get(
                f"{base}/api/v1/admin/groups",
                headers=headers,
                params={"page": 1, "page_size": 1000, "sort_by": "sort_order", "sort_order": "asc"},
            )
            if response.status_code >= 300:
                message = _error_text(response)
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "retryable": _retryable(response.status_code, message),
                    "error": message,
                }
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            items = (data.get("items") or data.get("groups") or []) if isinstance(data, dict) else data
            groups = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    group_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                groups.append(
                    {
                        "id": group_id,
                        "name": str(item.get("name") or f"分组 {group_id}"),
                        "platform": str(item.get("platform") or "").lower(),
                    }
                )
            return {"ok": True, "groups": groups}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "retryable": True, "error": f"获取 Sub2API 分组失败: {exc}"}


def _auth_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_url": str(config.get("base_url") or ""),
        "api_key": str(config.get("api_key") or ""),
        "auth_mode": str(config.get("auth_mode") or "api_key"),
        "admin_email": str(config.get("admin_email") or ""),
        "admin_password": str(config.get("admin_password") or ""),
    }


def _find_account(
    client: httpx.Client,
    *,
    base: str,
    headers: dict[str, str],
    email: str,
) -> dict[str, Any] | None:
    response = client.get(
        f"{base}/api/v1/admin/accounts",
        headers=headers,
        params={"page": 1, "page_size": 20, "platform": "grok", "search": email},
    )
    if response.status_code >= 300:
        raise RuntimeError(f"导入后账号查询失败: {_error_text(response)}")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    items = data.get("items") if isinstance(data, dict) else data
    target = email.strip().lower()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        item_email = str(credentials.get("email") or item.get("name") or "").strip().lower()
        if item_email == target:
            return item
    return None


def import_record(record: dict[str, Any], *, group_id: int = 0, **config: Any) -> dict[str, Any]:
    base = str(config.get("base_url") or "").strip().rstrip("/")
    try:
        document = token_to_sub2api_record(record)
    except Exception as exc:
        return {"ok": False, "retryable": False, "error": str(exc)}
    try:
        with _client(float(config.get("timeout") or 45), bool(config.get("verify_tls", True))) as client:
            headers, error = _auth_headers(client, **_auth_config(config))
            if error:
                return error
            selected_group_id = int(group_id or 0)
            response = client.post(
                f"{base}/api/v1/admin/accounts/data",
                headers={**(headers or {}), "Content-Type": "application/json"},
                json={
                    "data": json.dumps(document, ensure_ascii=False),
                    "skip_default_group_bind": bool(selected_group_id),
                },
            )
            if response.status_code >= 300:
                message = _error_text(response)
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "retryable": _retryable(response.status_code, message),
                    "error": message,
                }
            try:
                payload = response.json()
            except Exception:
                payload = {}
            data = payload.get("data") if isinstance(payload, dict) else payload
            failed = int(data.get("account_failed") or 0) if isinstance(data, dict) else 0
            if failed:
                return {"ok": False, "retryable": False, "error": f"Sub2API 导入失败账号数: {failed}"}
            created = int(data.get("account_created") or 0) if isinstance(data, dict) else 1
            account_id = None
            if selected_group_id:
                email = str(record.get("email") or "").strip().lower()
                imported = _find_account(client, base=base, headers=headers or {}, email=email)
                if not imported or not imported.get("id"):
                    return {
                        "ok": False,
                        "retryable": True,
                        "created": created,
                        "error": "账号已导入，但回查不到账号，暂时无法绑定分组",
                    }
                account_id = imported["id"]
                bind_response = client.put(
                    f"{base}/api/v1/admin/accounts/{account_id}",
                    headers={**(headers or {}), "Content-Type": "application/json"},
                    json={"group_ids": [selected_group_id]},
                )
                if bind_response.status_code >= 300:
                    message = _error_text(bind_response)
                    return {
                        "ok": False,
                        "retryable": _retryable(bind_response.status_code, message),
                        "created": created,
                        "account_id": account_id,
                        "error": f"账号已导入，但分组绑定失败: {message}",
                    }
            return {
                "ok": True,
                "created": created,
                "account_id": account_id,
                "group_id": selected_group_id or None,
                "path": "data_import",
            }
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return {"ok": False, "retryable": True, "error": f"Sub2API 网络错误: {exc}"}


def import_sso(
    *, sso_token: str, email: str, group_id: int = 0, **config: Any
) -> dict[str, Any]:
    base = str(config.get("base_url") or "").strip().rstrip("/")
    if not str(sso_token or "").strip():
        return {"ok": False, "retryable": False, "error": "缺少可导入的 xAI SSO"}
    try:
        with _client(float(config.get("timeout") or 90), bool(config.get("verify_tls", True))) as client:
            headers, error = _auth_headers(client, **_auth_config(config))
            if error:
                return error
            selected_group_id = int(group_id or 0)
            response = client.post(
                f"{base}/api/v1/admin/grok/sso-to-oauth",
                headers={**(headers or {}), "Content-Type": "application/json"},
                json={
                    "sso_token": sso_token,
                    "name": email.strip().lower() or "Grok OAuth Account",
                    "concurrency": 3,
                    "priority": 50,
                    "group_ids": [selected_group_id] if selected_group_id else [],
                },
            )
            if response.status_code >= 300:
                message = _error_text(response)
                return {
                    "ok": False,
                    "retryable": _retryable(response.status_code, message),
                    "status_code": response.status_code,
                    "error": message,
                }
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            created = data.get("created") if isinstance(data, dict) else None
            first = created[0] if isinstance(created, list) and created else None
            account = first.get("account") if isinstance(first, dict) else None
            account_id = account.get("id") if isinstance(account, dict) else None
            if not account_id:
                return {"ok": False, "retryable": False, "error": "Sub2API SSO 导入未创建账号"}
            return {
                "ok": True,
                "account_id": account_id,
                "group_id": selected_group_id or None,
                "path": "sso_to_oauth",
            }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "retryable": True, "error": f"Sub2API 网络错误: {exc}"}


def start_oauth(**config: Any) -> dict[str, Any]:
    base = str(config.get("base_url") or "").strip().rstrip("/")
    try:
        with _client(45, bool(config.get("verify_tls", True))) as client:
            headers, error = _auth_headers(client, **_auth_config(config))
            if error:
                return error
            response = client.post(
                f"{base}/api/v1/admin/grok/oauth/auth-url",
                headers={**(headers or {}), "Content-Type": "application/json"},
                json={},
            )
            if response.status_code >= 300:
                message = _error_text(response)
                return {
                    "ok": False,
                    "retryable": _retryable(response.status_code, message),
                    "status_code": response.status_code,
                    "error": message,
                }
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(data, dict):
                data = {}
            auth_url = str(data.get("auth_url") or "")
            session_id = str(data.get("session_id") or "")
            state = str(data.get("state") or "")
            if not auth_url or not session_id or not state:
                return {"ok": False, "retryable": False, "error": "OAuth 响应缺少 auth_url/session_id/state"}
            return {"ok": True, "auth_url": auth_url, "session_id": session_id, "state": state}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "retryable": True, "error": f"Sub2API OAuth 启动失败: {exc}"}


def _callback_code(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "://" not in raw:
        return raw, ""
    query = parse_qs(urlparse(raw).query)
    return str((query.get("code") or [""])[0]), str((query.get("state") or [""])[0])


def complete_oauth(
    *,
    session_id: str,
    state: str,
    callback: str,
    email: str,
    group_id: int = 0,
    **config: Any,
) -> dict[str, Any]:
    base = str(config.get("base_url") or "").strip().rstrip("/")
    code, callback_state = _callback_code(callback)
    expected_state = str(state or "").strip()
    if callback_state and callback_state != expected_state:
        return {"ok": False, "retryable": False, "error": "OAuth callback state 不匹配"}
    if not code or not session_id or not expected_state:
        return {"ok": False, "retryable": False, "error": "OAuth callback 缺少 code/session_id/state"}
    try:
        with _client(60, bool(config.get("verify_tls", True))) as client:
            headers, error = _auth_headers(client, **_auth_config(config))
            if error:
                return error
            selected_group_id = int(group_id or 0)
            response = client.post(
                f"{base}/api/v1/admin/grok/oauth/create-from-oauth",
                headers={**(headers or {}), "Content-Type": "application/json"},
                json={
                    "session_id": session_id,
                    "code": code,
                    "state": expected_state,
                    "name": email.strip().lower() or "Grok OAuth Account",
                    "concurrency": 3,
                    "priority": 50,
                    "group_ids": [selected_group_id] if selected_group_id else [],
                },
            )
            if response.status_code >= 300:
                message = _error_text(response)
                return {
                    "ok": False,
                    "retryable": _retryable(response.status_code, message),
                    "status_code": response.status_code,
                    "error": message,
                }
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            account_id = data.get("id") if isinstance(data, dict) else None
            if not account_id:
                return {"ok": False, "retryable": False, "error": "OAuth 导入成功响应缺少账号 ID"}
            return {
                "ok": True,
                "account_id": account_id,
                "group_id": selected_group_id or None,
                "path": "oauth_callback",
            }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "retryable": True, "error": f"Sub2API OAuth 完成失败: {exc}"}

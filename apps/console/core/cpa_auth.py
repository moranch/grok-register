"""Grok SSO to complete, renewable CLIProxyAPI xAI credentials."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from curl_cffi import requests as curl_requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)
CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}


def _proxies(proxy: str) -> dict[str, str] | None:
    return {"http": proxy, "https": proxy} if proxy else None


def _decode_jwt(token: str) -> dict[str, Any]:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except Exception:
        return {}


def _iso_utc(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _json_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _request_with_retry(
    session: Any,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    **kwargs: Any,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code < 500 or attempt + 1 >= attempts:
                return response
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                raise
        time.sleep(min(1 + attempt * 2, 5))
    if last_error:
        raise last_error
    raise RuntimeError(f"请求失败: {url}")


def _inject_sso_cookies(session: Any, sso: str, sso_rw: str = "") -> None:
    for name, value in (("sso", sso), ("sso-rw", sso_rw or sso)):
        if not value:
            continue
        for domain in (".x.ai", "accounts.x.ai", "auth.x.ai", ".accounts.x.ai"):
            try:
                session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass


def exchange_sso_for_token(
    sso: str,
    *,
    sso_rw: str = "",
    proxy: str = "",
    timeout: int = 90,
    verify_tls: bool = True,
    log: Callable[[str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run x.ai device flow and require both access and refresh tokens."""
    logger = log or (lambda _: None)
    if not str(sso or "").strip():
        raise ValueError("SSO 为空")

    session = curl_requests.Session()
    session.proxies = _proxies(proxy) or {}
    session.verify = verify_tls
    _inject_sso_cookies(session, str(sso).strip(), str(sso_rw or "").strip())

    check = _request_with_retry(
        session,
        "GET",
        "https://accounts.x.ai/",
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    check_url = str(getattr(check, "url", "") or "")
    if check.status_code >= 400 or "sign-in" in check_url or "sign-up" in check_url:
        raise RuntimeError("SSO 无效或已过期")
    if cancel and cancel():
        raise RuntimeError("CPA mint 已取消")

    device = _request_with_retry(
        session,
        "POST",
        f"{OIDC_ISSUER}/oauth2/device/code",
        data={"client_id": CLIENT_ID, "scope": SCOPES},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=20,
    )
    device_data = _json_payload(device)
    if device.status_code >= 400:
        raise RuntimeError(f"申请 device code 失败: HTTP {device.status_code}")
    user_code = str(device_data.get("user_code") or "")
    device_code = str(device_data.get("device_code") or "")
    verification_uri = str(device_data.get("verification_uri_complete") or "")
    if not user_code or not device_code or not verification_uri:
        raise RuntimeError("device flow 返回数据不完整")

    _request_with_retry(
        session,
        "GET",
        verification_uri,
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    if cancel and cancel():
        raise RuntimeError("CPA mint 已取消")

    verified = _request_with_retry(
        session,
        "POST",
        f"{OIDC_ISSUER}/oauth2/device/verify",
        data={"user_code": user_code},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    verify_url = str(getattr(verified, "url", "") or "")
    verify_text = str(getattr(verified, "text", "") or "").lower()[:1000]
    if not any(marker in verify_url.lower() or marker in verify_text for marker in (
        "consent", "authorize grok build", "授权 grok build"
    )):
        raise RuntimeError(f"device flow 验证失败: HTTP {verified.status_code}")

    approved = _request_with_retry(
        session,
        "POST",
        f"{OIDC_ISSUER}/oauth2/device/approve",
        data={
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    approve_url = str(getattr(approved, "url", "") or "")
    approve_text = str(getattr(approved, "text", "") or "").lower()[:1000]
    if not any(marker in approve_url.lower() or marker in approve_text for marker in (
        "done", "device authorized", "设备已授权"
    )):
        raise RuntimeError(f"device flow 授权失败: HTTP {approved.status_code}")

    interval = max(2, int(device_data.get("interval") or 5))
    deadline = time.time() + min(int(device_data.get("expires_in") or timeout), max(timeout, 30))
    transient_errors = 0
    while time.time() < deadline:
        if cancel and cancel():
            raise RuntimeError("CPA mint 已取消")
        time.sleep(interval)
        try:
            response = session.post(
                f"{OIDC_ISSUER}/oauth2/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=20,
            )
        except Exception as exc:
            transient_errors += 1
            if transient_errors >= 8:
                raise RuntimeError(f"OAuth token 轮询网络失败: {exc}") from exc
            time.sleep(min(transient_errors * 2, 10))
            continue

        payload = _json_payload(response)
        if response.status_code < 400:
            if not payload.get("access_token"):
                raise RuntimeError("OAuth 响应缺少 access_token")
            if not payload.get("refresh_token"):
                raise RuntimeError("OAuth 响应缺少 refresh_token，凭据无法续期")
            logger("OAuth access/refresh token 获取成功")
            return payload
        if response.status_code >= 500:
            transient_errors += 1
            if transient_errors < 8:
                continue
        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise RuntimeError(f"OAuth token 获取失败: {error or response.status_code}")
    raise TimeoutError("OAuth token 获取超时")


def token_to_cpa_record(
    token: dict[str, Any],
    email: str = "",
    *,
    base_url: str = CPA_BASE_URL,
) -> dict[str, Any]:
    access_token = str(token.get("access_token") or token.get("key") or "").strip()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("access_token 为空")
    if not refresh_token:
        raise ValueError("refresh_token 为空，不能生成可续期 CPA 凭据")
    id_token = str(token.get("id_token") or "").strip()
    payload = _decode_jwt(access_token)
    id_payload = _decode_jwt(id_token) if id_token else {}
    resolved_email = email or str(id_payload.get("email") or payload.get("email") or "")
    expires_at = _iso_utc(payload.get("exp"))
    expires_in = token.get("expires_in")
    if expires_in is None and payload.get("exp") and payload.get("iat"):
        expires_in = max(int(payload["exp"]) - int(payload["iat"]), 0)
    if not expires_at and expires_in is not None:
        expires_at = _iso_utc(time.time() + int(expires_in))
    record = {
        "type": "xai",
        "auth_kind": "oauth",
        "email": resolved_email,
        "sub": str(payload.get("sub") or payload.get("principal_id") or id_payload.get("sub") or ""),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": int(expires_in or 21600),
        "expired": expires_at,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "redirect_uri": CPA_REDIRECT_URI,
        "token_endpoint": f"{OIDC_ISSUER}/oauth2/token",
        "base_url": (base_url or CPA_BASE_URL).rstrip("/"),
        "disabled": False,
        "headers": dict(CPA_HEADERS),
    }
    if id_token:
        record["id_token"] = id_token
    return record


def cpa_filename(record: dict[str, Any]) -> str:
    identity = str(record.get("email") or record.get("sub") or "unknown")
    safe = "".join(char if char.isalnum() or char in "._-@" else "-" for char in identity).strip("-")
    return f"xai-{safe or int(time.time() * 1000)}.json"


def write_cpa_record(auth_dir: str, record: dict[str, Any]) -> Path:
    directory = Path(auth_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / cpa_filename(record)
    fd, temp_name = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def probe_cpa_models(
    access_token: str,
    *,
    base_url: str = CPA_BASE_URL,
    proxy: str = "",
    timeout: int = 30,
    verify_tls: bool = True,
    headers: dict[str, str] | None = None,
    model: str = "grok-4.5",
) -> dict[str, Any]:
    """Run a minimal real response to verify account usability, not just model listing."""
    session = curl_requests.Session()
    session.proxies = _proxies(proxy) or {}
    session.verify = verify_tls
    try:
        response = _request_with_retry(
            session,
            "POST",
            base_url.rstrip("/") + "/responses",
            headers={
                **CPA_HEADERS,
                **(headers or {}),
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": "ping",
                "max_output_tokens": 2,
                "stream": False,
            },
            impersonate="chrome",
            timeout=timeout,
        )
        status = int(response.status_code)
        summary = str(getattr(response, "text", "") or "").replace("\n", " ").strip()[:500]
        lowered = summary.lower()
        banned_markers = (
            "blocked-user",
            "account suspended",
            "account_suspended",
            "account banned",
            "account_banned",
            "email-domain-rejected",
            "user banned",
        )
        token_markers = (
            "invalid-credentials",
            "bad-credentials",
            "failed to look up session id",
            "session not found",
            "token revoked",
            "token expired",
        )
        banned = status in (400, 401, 403) and any(marker in lowered for marker in banned_markers)
        token_invalid = status == 401 or (
            status in (400, 403) and any(marker in lowered for marker in token_markers)
        )
        if 200 <= status < 300:
            failure_kind = ""
            account_state = "active"
        elif banned:
            failure_kind = "banned"
            account_state = "banned"
        elif token_invalid:
            failure_kind = "token_expired"
            account_state = "token_invalid"
        elif status == 429:
            failure_kind = "rate_limited"
            account_state = "rate_limited"
        elif status == 403:
            failure_kind = "forbidden"
            account_state = "unknown"
        elif status >= 500:
            failure_kind = "transient"
            account_state = "unknown"
        else:
            failure_kind = "rejected"
            account_state = "unknown"
        ok = 200 <= status < 300
        return {
            "ok": ok,
            "status": status,
            "model_ids": [model] if ok else [],
            "has_grok_45": ok and model == "grok-4.5",
            "probe_kind": "account_response",
            "probe_version": 2,
            "account_state": account_state,
            "banned": banned,
            "failure_kind": failure_kind,
            "refresh_recommended": token_invalid,
            "error": "" if ok else (summary or f"HTTP {status}"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "model_ids": [],
            "has_grok_45": False,
            "probe_kind": "account_response",
            "probe_version": 2,
            "account_state": "unknown",
            "banned": False,
            "failure_kind": "transient",
            "refresh_recommended": False,
            "error": str(exc),
        }


def upload_cpa_record(
    endpoint: str,
    management_key: str,
    record: dict[str, Any],
    *,
    timeout: int = 30,
    verify_tls: bool = True,
) -> str:
    filename = cpa_filename(record)
    url = endpoint.rstrip("/") + "/v0/management/auth-files"
    with httpx.Client(timeout=timeout, verify=verify_tls) as client:
        response = client.post(
            url,
            params={"name": filename},
            headers={"Authorization": f"Bearer {management_key}"},
            json=record,
        )
        response.raise_for_status()
    return filename

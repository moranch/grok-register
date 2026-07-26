"""Grok SSO to complete, renewable CLIProxyAPI xAI credentials."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from curl_cffi import requests as curl_requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
SCOPES = (
    "openid profile email offline_access grok-cli:access api:access"
)
CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_USERINFO_URL = f"{OIDC_ISSUER}/oauth2/userinfo"
GROK_ACCOUNT_URL = "https://accounts.x.ai/account"
CPA_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}
IDENTITY_PROXY_BYPASS_SECONDS = 300
_identity_proxy_bypass_until = 0.0
_identity_proxy_bypass_lock = threading.Lock()
_model_proxy_bypass_until = 0.0
_model_proxy_bypass_lock = threading.Lock()


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
    try:
        denial_grace_attempts = min(
            max(
                0,
                int(
                    os.getenv(
                        "GROK_REGISTER_OAUTH_DENIAL_GRACE_ATTEMPTS",
                        "6",
                    )
                ),
            ),
            20,
        )
    except (TypeError, ValueError):
        denial_grace_attempts = 6
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
        error_description = str(payload.get("error_description") or "").strip()
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if (
            error == "invalid_grant"
            and "access denied" in error_description.casefold()
            and denial_grace_attempts > 0
            and time.time() + interval < deadline
        ):
            # The consent page can reach its final state before auth.x.ai has
            # replicated the grant to the token endpoint.  Registration-side
            # OAuth already tolerates this window; historical SSO backfill must
            # do the same or it quarantines every otherwise-valid account.
            denial_grace_attempts -= 1
            logger(
                "OAuth poll: access-denied grace retry "
                f"remaining={denial_grace_attempts}"
            )
            continue
        detail = error or str(response.status_code)
        if error_description:
            detail = f"{detail}: {error_description}"
        raise RuntimeError(f"OAuth token 获取失败: {detail}")
    raise TimeoutError("OAuth token 获取超时")


def refresh_cpa_token(
    refresh_token: str,
    *,
    proxy: str = "",
    timeout: int = 20,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Renew an x.ai OAuth credential without invoking the device flow."""
    refresh_token = str(refresh_token or "").strip()
    if not refresh_token:
        raise ValueError("refresh_token 为空")

    def request_refresh(proxy_url: str):
        session = curl_requests.Session()
        session.proxies = _proxies(proxy_url) or {}
        session.verify = verify_tls
        return _request_with_retry(
            session,
            "POST",
            f"{OIDC_ISSUER}/oauth2/token",
            attempts=1,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=timeout,
        )

    global _identity_proxy_bypass_until
    with _identity_proxy_bypass_lock:
        bypass_proxy = bool(proxy) and time.monotonic() < _identity_proxy_bypass_until
    transport = "direct_bypass" if bypass_proxy else ("proxy" if proxy else "direct")
    try:
        response = request_refresh("" if bypass_proxy else proxy)
    except Exception as proxy_error:
        if not proxy or bypass_proxy:
            raise
        with _identity_proxy_bypass_lock:
            _identity_proxy_bypass_until = time.monotonic() + IDENTITY_PROXY_BYPASS_SECONDS
        try:
            response = request_refresh("")
            transport = "direct_fallback"
        except Exception as direct_error:
            raise RuntimeError(
                f"proxy OAuth refresh failed: {proxy_error}; "
                f"direct OAuth refresh failed: {direct_error}"
            ) from direct_error

    payload = _json_payload(response)
    if int(response.status_code) >= 400:
        detail = str(
            payload.get("error_description")
            or payload.get("error")
            or getattr(response, "text", "")
            or f"HTTP {response.status_code}"
        ).strip()
        raise RuntimeError(f"OAuth refresh failed: {detail[:500]}")
    if not str(payload.get("access_token") or "").strip():
        raise RuntimeError("OAuth refresh 响应缺少 access_token")
    if not str(payload.get("refresh_token") or "").strip():
        payload["refresh_token"] = refresh_token
    payload["transport"] = transport
    return payload


def probe_grok_account_session(
    sso: str,
    *,
    sso_rw: str = "",
    proxy: str = "",
    timeout: int = 20,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Check the Grok account session without calling OAuth or a model endpoint."""
    sso = str(sso or "").strip()
    if not sso:
        return {
            "ok": False,
            "account_alive": False,
            "delivery_eligible": False,
            "status": 0,
            "error": "sso is empty",
            "failure_kind": "session_missing",
            "refresh_recommended": False,
            "banned": False,
            "probe_kind": "account_session",
            "probe_version": 4,
        }

    def request_account(proxy_url: str):
        session = curl_requests.Session()
        session.proxies = _proxies(proxy_url) or {}
        session.verify = verify_tls
        _inject_sso_cookies(session, sso, sso_rw)
        return _request_with_retry(
            session,
            "GET",
            GROK_ACCOUNT_URL,
            attempts=1,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )

    global _identity_proxy_bypass_until
    with _identity_proxy_bypass_lock:
        bypass_proxy = bool(proxy) and time.monotonic() < _identity_proxy_bypass_until
    transport = "direct_bypass" if bypass_proxy else ("proxy" if proxy else "direct")
    try:
        response = request_account("" if bypass_proxy else proxy)
    except Exception as proxy_error:
        if not proxy or bypass_proxy:
            raise
        with _identity_proxy_bypass_lock:
            _identity_proxy_bypass_until = time.monotonic() + IDENTITY_PROXY_BYPASS_SECONDS
        try:
            response = request_account("")
            transport = "direct_fallback"
        except Exception as direct_error:
            raise RuntimeError(
                f"proxy Grok session probe failed: {proxy_error}; "
                f"direct Grok session probe failed: {direct_error}"
            ) from direct_error

    status = int(response.status_code)
    final_url = str(getattr(response, "url", "") or "")
    parsed = urlparse(final_url)
    final_host = parsed.hostname or ""
    final_path = parsed.path.rstrip("/") or "/"
    alive = bool(
        status == 200
        and final_host.lower() == "accounts.x.ai"
        and (final_path == "/account" or final_path.startswith("/account/"))
    )
    return {
        "ok": alive,
        "account_alive": alive,
        "delivery_eligible": alive,
        "status": status,
        "account_state": "active" if alive else "unavailable",
        "error": "" if alive else f"Grok account session unavailable: HTTP {status}",
        "failure_kind": "" if alive else "session_invalid",
        "refresh_recommended": False,
        "banned": False,
        "probe_kind": "account_session",
        "probe_version": 4,
        "transport": transport,
        "final_path": final_path,
    }


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


def token_to_sub2api_record(record: dict[str, Any]) -> dict[str, Any]:
    """Build a one-account Sub2API import document from a CPA xAI record."""
    access_token = str(record.get("access_token") or "").strip()
    refresh_token = str(record.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise ValueError("Sub2API auth requires access_token and refresh_token")
    email = str(record.get("email") or "").strip()
    subject = str(record.get("sub") or "").strip()
    credentials: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": str(record.get("token_type") or "Bearer"),
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "base_url": str(record.get("base_url") or CPA_BASE_URL).rstrip("/"),
    }
    for source, target in (
        ("expired", "expires_at"),
        ("id_token", "id_token"),
        ("email", "email"),
        ("sub", "sub"),
    ):
        if record.get(source) not in (None, ""):
            credentials[target] = record[source]
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": [
            {
                "name": email or subject or "Grok OAuth Account",
                "platform": "grok",
                "type": "oauth",
                "credentials": credentials,
                "concurrency": 1,
                "priority": 0,
                "auto_pause_on_expired": True,
            }
        ],
    }


def grok2api_web_auth_record(email: str, sso: str) -> dict[str, Any]:
    """Build the v3 Grok2API web-account import shape."""
    if not str(sso or "").strip():
        raise ValueError("Grok2API auth requires sso")
    return {
        "version": 1,
        "accounts": [
            {
                "name": str(email or "grok-web").strip() or "grok-web",
                "sso_token": str(sso).strip(),
                "tier": "auto",
            }
        ],
    }


def _safe_identity(value: str, fallback: str = "account") -> str:
    safe = "".join(
        char if char.isalnum() or char in "._-@" else "_"
        for char in str(value or "").strip()
    ).strip("._-")
    return safe or fallback


def _write_json_record(directory: str, filename: str, document: dict[str, Any]) -> Path:
    target_dir = Path(directory).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    fd, temp_name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def write_sub2api_record(auth_dir: str, record: dict[str, Any]) -> Path:
    identity = str(record.get("email") or record.get("sub") or "account")
    return _write_json_record(
        auth_dir,
        f"SUB2API-grok-{_safe_identity(identity)}.json",
        token_to_sub2api_record(record),
    )


def write_grok2api_web_record(auth_dir: str, email: str, sso: str) -> Path:
    return _write_json_record(
        auth_dir,
        f"GROK2API-grok-{_safe_identity(email)}.json",
        grok2api_web_auth_record(email, sso),
    )


def probe_cpa_account(
    access_token: str,
    *,
    proxy: str = "",
    timeout: int = 30,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Verify the OAuth identity only; never call a model endpoint."""

    def request_identity(proxy_url: str):
        session = curl_requests.Session()
        session.proxies = _proxies(proxy_url) or {}
        session.verify = verify_tls
        return _request_with_retry(
            session,
            "GET",
            CPA_USERINFO_URL,
            attempts=1,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            impersonate="chrome",
            timeout=timeout,
        )

    global _identity_proxy_bypass_until
    with _identity_proxy_bypass_lock:
        bypass_proxy = bool(proxy) and time.monotonic() < _identity_proxy_bypass_until
    transport = "direct_bypass" if bypass_proxy else ("proxy" if proxy else "direct")
    try:
        try:
            response = request_identity("" if bypass_proxy else proxy)
        except Exception as proxy_error:
            if not proxy or bypass_proxy:
                raise
            with _identity_proxy_bypass_lock:
                _identity_proxy_bypass_until = (
                    time.monotonic() + IDENTITY_PROXY_BYPASS_SECONDS
                )
            try:
                response = request_identity("")
                transport = "direct_fallback"
            except Exception as direct_error:
                raise RuntimeError(
                    f"proxy identity check failed: {proxy_error}; "
                    f"direct identity check failed: {direct_error}"
                ) from direct_error
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
        account_alive = 200 <= status < 300
        return {
            "ok": account_alive,
            "account_alive": account_alive,
            "delivery_eligible": account_alive,
            "status": status,
            "probe_kind": "account_identity",
            "probe_version": 3,
            "transport": transport,
            "account_state": account_state,
            "banned": banned,
            "failure_kind": failure_kind,
            "refresh_recommended": token_invalid,
            "error": "" if account_alive else (summary or f"HTTP {status}"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "account_alive": False,
            "delivery_eligible": False,
            "status": 0,
            "probe_kind": "account_identity",
            "probe_version": 3,
            "transport": transport,
            "account_state": "unknown",
            "banned": False,
            "failure_kind": "transient",
            "refresh_recommended": False,
            "error": str(exc),
        }


def probe_cpa_model(
    access_token: str,
    *,
    model: str = "grok-4.5",
    base_url: str = CPA_BASE_URL,
    proxy: str = "",
    timeout: int = 30,
    verify_tls: bool = True,
) -> dict[str, Any]:
    """Run one explicit model probe without changing account-delivery liveness.

    This mirrors grokcli-2api's manual model-health request: the OAuth token is
    sent to the CPA-compatible ``/responses`` endpoint with a tiny streaming
    prompt.  The caller may persist the returned, credential-free summary, but
    must not use it to decide whether the underlying account is alive.
    """
    token = str(access_token or "").strip()
    selected_model = str(model or "grok-4.5").strip() or "grok-4.5"
    endpoint = str(base_url or CPA_BASE_URL).strip().rstrip("/") + "/responses"
    started = time.monotonic()
    if not token:
        return {
            "ok": False,
            "model_available": False,
            "model": selected_model,
            "status": 0,
            "latency_ms": 0,
            "probe_kind": "model_response",
            "transport": "none",
            "failure_kind": "credential_missing",
            "refresh_recommended": False,
            "error": "access_token is empty",
        }

    payload = {
        "model": selected_model,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            }
        ],
        "tools": [{"type": "x_search"}],
        "max_output_tokens": 8,
        "reasoning": {"effort": "low", "summary": "auto"},
        "instructions": "",
        "parallel_tool_calls": True,
    }
    headers = {
        **CPA_HEADERS,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Connection": "Keep-Alive",
    }

    def request_model(proxy_url: str):
        if proxy_url:
            session = curl_requests.Session()
            session.proxies = _proxies(proxy_url) or {}
            session.verify = verify_tls
            return _request_with_retry(
                session,
                "POST",
                endpoint,
                attempts=1,
                headers=headers,
                json=payload,
                impersonate="chrome",
                timeout=timeout,
            )
        # curl_cffi can report an OpenSSL "invalid library" handshake error
        # against cli-chat-proxy on some Linux images even though the same host
        # is reachable.  httpx provides a separate, deterministic direct path.
        with httpx.Client(timeout=timeout, verify=verify_tls) as client:
            return client.post(endpoint, headers=headers, json=payload)

    global _model_proxy_bypass_until
    with _model_proxy_bypass_lock:
        bypass_proxy = bool(proxy) and time.monotonic() < _model_proxy_bypass_until
    transport = "direct_bypass" if bypass_proxy else ("proxy" if proxy else "direct")
    try:
        try:
            response = request_model("" if bypass_proxy else proxy)
        except Exception as proxy_error:
            if not proxy or bypass_proxy:
                raise
            with _model_proxy_bypass_lock:
                _model_proxy_bypass_until = (
                    time.monotonic() + IDENTITY_PROXY_BYPASS_SECONDS
                )
            try:
                response = request_model("")
                transport = "direct_fallback"
            except Exception as direct_error:
                transport = "proxy_and_direct_failed"
                raise RuntimeError(
                    f"proxy model test failed: {proxy_error}; "
                    f"direct model test failed: {direct_error}"
                ) from direct_error
        status = int(response.status_code)
        summary = str(getattr(response, "text", "") or "").replace("\n", " ").strip()
        lowered = summary.lower()
        available = 200 <= status < 300 and bool(summary)
        if available:
            failure_kind = ""
            error = ""
        elif 200 <= status < 300:
            failure_kind = "empty_output"
            error = "empty model output"
        elif status == 401:
            failure_kind = "token_expired"
            error = summary[:500] or "HTTP 401"
        elif "personal-team-blocked:spending-limit" in lowered or any(
            marker in lowered
            for marker in ("run out of credits", "need a grok subscription")
        ):
            failure_kind = "quota_exhausted"
            error = summary[:500] or f"HTTP {status}"
        elif status == 403 and (
            "permission-denied" in lowered
            or "chat endpoint is denied" in lowered
        ):
            failure_kind = "model_denied"
            error = summary[:500] or "HTTP 403"
        elif status == 403:
            failure_kind = "forbidden"
            error = summary[:500] or "HTTP 403"
        elif status == 429:
            failure_kind = "rate_limited"
            error = summary[:500] or "HTTP 429"
        elif status >= 500:
            failure_kind = "transient"
            error = summary[:500] or f"HTTP {status}"
        else:
            failure_kind = "rejected"
            error = summary[:500] or f"HTTP {status}"
        return {
            "ok": available,
            "model_available": available,
            "model": selected_model,
            "status": status,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "probe_kind": "model_response",
            "transport": transport,
            "failure_kind": failure_kind,
            "refresh_recommended": status == 401,
            "error": error,
        }
    except Exception as exc:
        return {
            "ok": False,
            "model_available": False,
            "model": selected_model,
            "status": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "probe_kind": "model_response",
            "transport": transport,
            "failure_kind": "transient",
            "refresh_recommended": False,
            "error": str(exc)[:500],
        }


# Backward-compatible import for third-party extensions. The implementation now
# performs OAuth identity liveness only and does not contact any model endpoint.
probe_cpa_models = probe_cpa_account


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

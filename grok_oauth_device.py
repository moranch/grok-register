"""xAI OAuth device flow bound to the browser that just created the account.

The registration browser already owns the authoritative ``CreateUserAndSession``
session.  Reusing that page for device consent avoids rebuilding an account
session from a copied cookie and keeps the signup and OAuth legs on the same
browser/profile/proxy identity.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests as curl_requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
SCOPE = "openid profile email offline_access grok-cli:access api:access"


class DeviceFlowError(RuntimeError):
    """Base error for registration-side device authorization."""


class DeviceFlowEntitlementDenied(DeviceFlowError):
    """The account session is valid but xAI refused Grok CLI/API access."""


def _payload(response: Any) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _new_session(proxy: str = "", verify_tls: bool = True):
    session = curl_requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.verify = bool(verify_tls)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "grok-register/1.0",
        }
    )
    return session


def request_device_code(
    session: Any,
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    response = session.post(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        impersonate="chrome",
        timeout=timeout,
    )
    body = _payload(response)
    if int(response.status_code) >= 400:
        detail = body.get("error_description") or body.get("error") or response.text
        raise DeviceFlowError(f"device code request failed: HTTP {response.status_code}: {str(detail)[:300]}")
    required = ("device_code", "user_code", "verification_uri")
    if any(not str(body.get(key) or "").strip() for key in required):
        raise DeviceFlowError("device code response is missing required fields")
    return body


def _page_text(page: Any) -> str:
    try:
        body = page.ele("tag:body", timeout=1)
        return str(getattr(body, "text", "") or "")
    except Exception:
        try:
            return str(getattr(page, "html", "") or "")
        except Exception:
            return ""


def _click_button(page: Any, labels: tuple[str, ...]) -> str:
    normalized = {label.casefold().strip() for label in labels}
    try:
        buttons = page.eles("tag:button", timeout=1) or []
    except Exception:
        buttons = []
    for button in buttons:
        text = str(getattr(button, "text", "") or "").strip()
        if text.casefold() not in normalized:
            continue
        try:
            button.click()
            return text
        except Exception:
            try:
                button.click(by_js=True)
                return text
            except Exception:
                continue
    return ""


def approve_in_registered_browser(
    page: Any,
    verification_url: str,
    *,
    timeout: int = 90,
    log: Callable[[str], None] | None = None,
) -> None:
    logger = log or (lambda _: None)
    page.get(verification_url)
    deadline = time.monotonic() + max(20, int(timeout))
    last_url = ""
    while time.monotonic() < deadline:
        url = str(getattr(page, "url", "") or "")
        text = _page_text(page)
        lowered = text.casefold()
        if url != last_url:
            logger(f"device browser url={url[:180]}")
            last_url = url

        if "oauth2/device/done" in url or "device authorized" in lowered or "设备已授权" in text:
            logger("device browser authorized")
            return
        if "access denied" in lowered or "unable to access" in lowered or "you have been blocked" in lowered:
            raise DeviceFlowError("device browser was blocked before consent")

        clicked = _click_button(
            page,
            (
                "Allow all cookies",
                "Accept all cookies",
                "全部允许",
                "接受所有 Cookie",
            ),
        )
        if not clicked:
            clicked = _click_button(page, ("Continue", "继续"))
        if not clicked:
            clicked = _click_button(page, ("Allow", "允许", "Authorize", "授权"))
        if clicked:
            logger(f"device browser clicked={clicked}")
            time.sleep(0.8)
            continue
        time.sleep(0.5)
    raise TimeoutError("device browser consent timed out")


def poll_device_token(
    session: Any,
    device: dict[str, Any],
    *,
    timeout: int = 90,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    logger = log or (lambda _: None)
    interval = max(1, int(device.get("interval") or 5))
    deadline = time.monotonic() + min(
        max(20, int(timeout)),
        max(20, int(device.get("expires_in") or timeout)),
    )
    # Consent is already complete, so poll immediately instead of adding an
    # unconditional first sleep to every registration.
    while time.monotonic() < deadline:
        response = session.post(
            f"{OIDC_ISSUER}/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": str(device["device_code"]),
            },
            impersonate="chrome",
            timeout=20,
        )
        body = _payload(response)
        if int(response.status_code) < 400:
            if not str(body.get("access_token") or "").strip():
                raise DeviceFlowError("token response is missing access_token")
            if not str(body.get("refresh_token") or "").strip():
                raise DeviceFlowError("token response is missing refresh_token")
            return body

        code = str(body.get("error") or "")
        detail = str(body.get("error_description") or "").strip()
        if code == "authorization_pending":
            time.sleep(interval)
            continue
        if code == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        if code in {"access_denied", "authorization_denied"} or (
            code == "invalid_grant" and "access denied" in detail.casefold()
        ):
            raise DeviceFlowEntitlementDenied(
                f"OAuth entitlement denied: {code}: {detail or 'Access denied'}"
            )
        raise DeviceFlowError(
            f"device token exchange failed: {code or response.status_code}: {detail or str(body)[:300]}"
        )
    logger("device token poll timed out")
    raise TimeoutError("device token exchange timed out")


def mint_in_registered_browser(
    page: Any,
    *,
    proxy: str = "",
    timeout: int = 90,
    verify_tls: bool = True,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    logger = log or (lambda _: None)
    session = _new_session(proxy=proxy, verify_tls=verify_tls)
    try:
        device = request_device_code(session)
    except DeviceFlowError:
        raise
    except Exception as exc:
        if not proxy:
            raise
        # Browser consent still stays on the registration proxy/profile. The
        # RFC 8628 device-code and token endpoints carry no Web session cookie,
        # so a direct transport fallback is safe and avoids SOCKS DNS failures.
        logger(f"device endpoint proxy failed, retrying direct: {type(exc).__name__}")
        session = _new_session(proxy="", verify_tls=verify_tls)
        device = request_device_code(session)
    user_code = str(device["user_code"])
    logger(f"device code issued user_code={user_code}")
    verification_url = str(
        device.get("verification_uri_complete")
        or f"{device['verification_uri']}?user_code={user_code}"
    )
    approve_in_registered_browser(page, verification_url, timeout=timeout, log=logger)
    try:
        token = poll_device_token(session, device, timeout=timeout, log=logger)
    except (DeviceFlowError, TimeoutError):
        raise
    except Exception as exc:
        if not proxy:
            raise
        logger(f"token endpoint proxy failed, retrying direct: {type(exc).__name__}")
        direct_session = _new_session(proxy="", verify_tls=verify_tls)
        token = poll_device_token(direct_session, device, timeout=timeout, log=logger)
    token["user_code"] = user_code
    token["mint_method"] = "registered_session_device_flow"
    return token


def oauth_event_path(sso_output_path: str | os.PathLike[str]) -> Path:
    path = Path(sso_output_path)
    return path.with_suffix(".oauth.jsonl")


def append_oauth_event(
    sso_output_path: str | os.PathLike[str],
    *,
    email: str,
    status: str,
    attempt_id: str = "",
    token: dict[str, Any] | None = None,
    error: str = "",
    failure_kind: str = "",
) -> str:
    """Append one replay-safe registration OAuth event with mode 0600."""
    path = oauth_event_path(sso_output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attempt_id = attempt_id or uuid.uuid4().hex
    event: dict[str, Any] = {
        "attempt_id": attempt_id,
        "email": str(email or "").strip(),
        "status": str(status or "").strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if token:
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "token_type",
            "expires_in",
            "scope",
            "user_code",
            "mint_method",
        ):
            if token.get(key) not in (None, ""):
                event[key] = token[key]
    if error:
        event["error"] = str(error)[:1000]
    if failure_kind:
        event["failure_kind"] = str(failure_kind)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return attempt_id

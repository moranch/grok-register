"""xAI OAuth device flow bound to the browser that just created the account.

The registration browser already owns the authoritative ``CreateUserAndSession``
session.  Reusing that page for device consent avoids rebuilding an account
session from a copied cookie and keeps the signup and OAuth legs on the same
browser/profile/proxy identity.
"""
from __future__ import annotations

import glob
import json
import os
import random
import shutil
import struct
import tempfile
import time
import uuid
from datetime import date
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


def _new_standard_session(verify_tls: bool = True):
    """Direct requests/OpenSSL fallback for curl_cffi TLS edge cases."""
    import requests as standard_requests

    session = standard_requests.Session()
    session._grok_standard_transport = True
    session._grok_verify_tls = bool(verify_tls)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "grok-register/1.0",
        }
    )
    return session


def _post_form(session: Any, url: str, data: dict[str, str], timeout: int):
    kwargs: dict[str, Any] = {"data": data, "timeout": timeout}
    if getattr(session, "_grok_standard_transport", False):
        kwargs["verify"] = bool(getattr(session, "_grok_verify_tls", True))
    else:
        kwargs["impersonate"] = "chrome"
    return session.post(url, **kwargs)


def _registered_cookie_header(page: Any) -> str:
    """Copy only the account-session cookies needed by the setup endpoints."""
    values: dict[str, str] = {}
    try:
        cookies = page.cookies(all_domains=True, all_info=True) or []
    except Exception:
        cookies = []
    for item in cookies:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
        else:
            name = str(getattr(item, "name", "") or "").strip()
            value = str(getattr(item, "value", "") or "").strip()
        if name in {"sso", "sso-rw", "cf_clearance"} and value:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _inject_sso_browser_cookies(
    page: Any,
    browser: Any,
    *,
    sso: str,
    sso_rw: str = "",
) -> int:
    """Install a persisted xAI session into a fresh Chromium profile."""
    sso = str(sso or "").strip()
    sso_rw = str(sso_rw or sso).strip()
    if not sso:
        raise ValueError("SSO is empty")
    cookies = [
        {
            "name": name,
            "value": value,
            "domain": ".x.ai",
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }
        for name, value in (("sso", sso), ("sso-rw", sso_rw))
        if value
    ]
    targets: list[Any] = []
    for target in (page, getattr(page, "browser", None), browser):
        if target is not None and all(target is not item for item in targets):
            targets.append(target)

    last_error: Exception | None = None
    for target in targets:
        setter = getattr(getattr(target, "set", None), "cookies", None)
        if not callable(setter):
            continue
        try:
            setter(cookies)
            return len(cookies)
        except Exception as exc:
            last_error = exc
            installed = 0
            for cookie in cookies:
                try:
                    setter(cookie)
                    installed += 1
                except Exception as item_exc:
                    last_error = item_exc
            if installed == len(cookies):
                return installed
    if last_error:
        raise RuntimeError(f"failed to inject SSO cookies: {last_error}") from last_error
    raise RuntimeError("DrissionPage cookie setter is unavailable")


def _find_chromium_path() -> str:
    for candidate in (
        shutil.which("chromium-browser"),
        shutil.which("chromium"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
    ):
        if candidate:
            return str(candidate)
    matches = glob.glob(
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")
    )
    return str(matches[0]) if matches else ""


def _new_sso_oauth_browser(
    *,
    proxy: str = "",
    headless: bool = False,
) -> tuple[Any, Any, str]:
    """Create an isolated browser used only by historical OAuth backfill."""
    from DrissionPage import Chromium, ChromiumOptions

    profile_dir = tempfile.mkdtemp(prefix="grok_cpa_oauth_")
    options = ChromiumOptions()
    options.auto_port()
    options.set_user_data_path(profile_dir)
    options.set_argument("--no-sandbox")
    options.set_argument("--disable-gpu")
    options.set_argument("--disable-dev-shm-usage")
    options.set_argument("--disable-software-rasterizer")
    if headless or not os.environ.get("DISPLAY"):
        options.set_argument("--headless=new")
    proxy = str(proxy or "").strip()
    if proxy:
        if proxy.lower().startswith("socks"):
            options.set_argument("--proxy-server", proxy)
        else:
            options.set_proxy(proxy)
    browser_path = _find_chromium_path()
    if browser_path:
        options.set_browser_path(browser_path)
    extension_path = Path(__file__).resolve().parent / "turnstilePatch"
    if extension_path.is_dir():
        options.add_extension(str(extension_path))
    options.set_timeouts(base=2)

    browser = None
    try:
        browser = Chromium(options)
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        return browser, page, profile_dir
    except Exception:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


def mint_in_sso_browser(
    *,
    sso: str,
    sso_rw: str = "",
    proxy: str = "",
    timeout: int = 120,
    verify_tls: bool = True,
    headless: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Mint OAuth for a persisted live SSO using a clean browser fallback."""
    logger = log or (lambda _: None)
    browser = None
    profile_dir = ""
    try:
        browser, page, profile_dir = _new_sso_oauth_browser(
            proxy=proxy,
            headless=headless,
        )
        installed = _inject_sso_browser_cookies(
            page,
            browser,
            sso=sso,
            sso_rw=sso_rw,
        )
        logger(f"historical browser cookies injected={installed}")
        page.get("https://accounts.x.ai/")
        current_url = str(getattr(page, "url", "") or "")
        if "sign-in" in current_url or "sign-up" in current_url:
            # Some Chromium builds apply domain cookies only after the first
            # navigation creates the origin. Install once more before failing.
            _inject_sso_browser_cookies(
                page,
                browser,
                sso=sso,
                sso_rw=sso_rw,
            )
            try:
                page.refresh()
            except Exception:
                page.get("https://accounts.x.ai/")
            current_url = str(getattr(page, "url", "") or "")
        if "sign-in" in current_url or "sign-up" in current_url:
            raise DeviceFlowError("persisted SSO was rejected by the browser session")
        if "sso=" not in _registered_cookie_header(page):
            raise DeviceFlowError("browser session did not retain the SSO cookie")
        token = mint_in_registered_browser(
            page,
            proxy=proxy,
            timeout=max(30, int(timeout)),
            verify_tls=verify_tls,
            log=logger,
        )
        token["mint_method"] = "historical_sso_browser_device_flow"
        return token
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        if profile_dir:
            shutil.rmtree(profile_dir, ignore_errors=True)


def _adult_birth_date() -> str:
    today = date.today()
    rng = random.SystemRandom()
    year = today.year - rng.randint(20, 40)
    return f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T16:00:00.000Z"


def prepare_registered_account(
    page: Any,
    *,
    proxy: str = "",
    timeout: int = 20,
    verify_tls: bool = True,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Finish the account state required before requesting Grok CLI consent.

    The upstream registration flow accepts the current ToS version and records an
    adult birth date before starting device OAuth.  A freshly-created Web session
    can otherwise render the consent success page while the token endpoint rejects
    the grant with ``invalid_grant: Access denied``.
    """
    logger = log or (lambda _: None)
    session = _new_session(proxy=proxy, verify_tls=verify_tls)
    cookie_header = _registered_cookie_header(page)
    try:
        user_agent = str(page.run_js("return navigator.userAgent") or "").strip()
    except Exception:
        user_agent = ""
    common_headers: dict[str, str] = {}
    if cookie_header:
        common_headers["Cookie"] = cookie_header
    if user_agent:
        common_headers["User-Agent"] = user_agent
    if common_headers:
        session.headers.update(common_headers)

    tos_payload = bytes((0x10, 0x01))
    tos_frame = b"\x00" + struct.pack(">I", len(tos_payload)) + tos_payload
    tos_response = session.post(
        "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion",
        data=tos_frame,
        headers={
            "Content-Type": "application/grpc-web+proto",
            "X-Grpc-Web": "1",
            "X-User-Agent": "connect-es/2.1.1",
            "Origin": "https://accounts.x.ai",
            "Referer": "https://accounts.x.ai/accept-tos",
        },
        impersonate="chrome",
        timeout=timeout,
    )
    tos_ok = 200 <= int(tos_response.status_code) < 300
    logger(f"account setup tos_status={tos_response.status_code}")

    # Use a fresh connection pool when crossing from accounts.x.ai to grok.com.
    # Some curl/HTTP2 builds otherwise reuse stale TLS state across the two hosts.
    birth_session = _new_session(proxy=proxy, verify_tls=verify_tls)
    if common_headers:
        birth_session.headers.update(common_headers)
    birth_url = "https://grok.com/rest/auth/set-birth-date"
    birth_payload = {"birthDate": _adult_birth_date()}
    birth_headers = {
        "Content-Type": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
    }
    try:
        birth_response = birth_session.post(
            birth_url,
            json=birth_payload,
            headers=birth_headers,
            impersonate="chrome",
            timeout=timeout,
        )
    except Exception as exc:
        # curl_cffi occasionally reports an OpenSSL cross-host connection error
        # here even though the same endpoint works through requests/OpenSSL.
        logger(f"account setup birth transport fallback: {type(exc).__name__}")
        import requests as standard_requests

        birth_response = standard_requests.post(
            birth_url,
            json=birth_payload,
            headers={**common_headers, **birth_headers},
            timeout=timeout,
            verify=verify_tls,
        )
    birth_ok = 200 <= int(birth_response.status_code) < 300
    logger(f"account setup birth_status={birth_response.status_code}")
    return {
        "ok": tos_ok and birth_ok,
        "tos_status": int(tos_response.status_code),
        "birth_status": int(birth_response.status_code),
    }


def request_device_code(
    session: Any,
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    response = _post_form(
        session,
        f"{OIDC_ISSUER}/oauth2/device/code",
        {"client_id": CLIENT_ID, "scope": SCOPE},
        timeout,
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
    cookie_consent_attempted = False
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

        clicked = ""
        if not cookie_consent_attempted:
            clicked = _click_button(
                page,
                (
                    "Allow all cookies",
                    "Accept all cookies",
                    "全部允许",
                    "接受所有 Cookie",
                ),
            )
            if clicked:
                # Some consent managers leave a stale/hidden button in the DOM.
                # Retrying it forever prevents us from ever reaching Continue.
                cookie_consent_attempted = True
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
    try:
        denial_grace_attempts = min(
            max(
                0,
                int(
                    os.getenv(
                        "GROK_REGISTER_OAUTH_DENIAL_GRACE_ATTEMPTS", "6"
                    )
                ),
            ),
            12,
        )
    except (TypeError, ValueError):
        denial_grace_attempts = 6
    deadline = time.monotonic() + min(
        max(20, int(timeout)),
        max(20, int(device.get("expires_in") or timeout)),
    )
    # Consent is already complete, so poll immediately instead of adding an
    # unconditional first sleep to every registration.
    while time.monotonic() < deadline:
        response = _post_form(
            session,
            f"{OIDC_ISSUER}/oauth2/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": str(device["device_code"]),
            },
            20,
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
            # accounts.x.ai can reach /device/done slightly before auth.x.ai has
            # replicated the consent.  Retry the same still-live device grant a
            # couple of times before classifying the account as ineligible.
            if denial_grace_attempts > 0 and time.monotonic() + interval < deadline:
                denial_grace_attempts -= 1
                logger(
                    "oauth poll: access-denied grace retry "
                    f"remaining={denial_grace_attempts}"
                )
                time.sleep(interval)
                continue
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
    try:
        setup = prepare_registered_account(
            page,
            proxy=proxy,
            timeout=min(max(10, int(timeout)), 30),
            verify_tls=verify_tls,
            log=logger,
        )
        if not setup.get("ok"):
            logger("account setup incomplete; continuing device OAuth")
    except Exception as exc:
        if proxy:
            try:
                logger(f"account setup proxy failed, retrying direct: {type(exc).__name__}")
                setup = prepare_registered_account(
                    page,
                    proxy="",
                    timeout=min(max(10, int(timeout)), 30),
                    verify_tls=verify_tls,
                    log=logger,
                )
                if not setup.get("ok"):
                    logger("account setup incomplete; continuing device OAuth")
            except Exception as direct_exc:
                # Preserve the registered Web account even if optional setup is
                # blocked. The token result remains the eligibility authority.
                logger(
                    "account setup failed; continuing device OAuth: "
                    f"{type(direct_exc).__name__}: {str(direct_exc)[:160]}"
                )
        else:
            logger(
                "account setup failed; continuing device OAuth: "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
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
        try:
            device = request_device_code(session)
        except DeviceFlowError:
            raise
        except Exception as direct_exc:
            logger(
                "device endpoint curl direct failed, retrying requests: "
                f"{type(direct_exc).__name__}"
            )
            session = _new_standard_session(verify_tls=verify_tls)
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
        try:
            token = poll_device_token(direct_session, device, timeout=timeout, log=logger)
        except (DeviceFlowError, TimeoutError):
            raise
        except Exception as direct_exc:
            logger(
                "token endpoint curl direct failed, retrying requests: "
                f"{type(direct_exc).__name__}"
            )
            standard_session = _new_standard_session(verify_tls=verify_tls)
            token = poll_device_token(standard_session, device, timeout=timeout, log=logger)
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

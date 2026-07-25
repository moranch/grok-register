import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_oauth_device import (
    CLIENT_ID,
    SCOPE,
    DeviceFlowEntitlementDenied,
    append_oauth_event,
    approve_in_registered_browser,
    mint_in_registered_browser,
    poll_device_token,
    prepare_registered_account,
    request_device_code,
)


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class _SetupPage:
    def cookies(self, **_kwargs):
        return [
            {"name": "sso", "value": "session"},
            {"name": "unrelated", "value": "ignored"},
        ]

    def run_js(self, _script):
        return "browser-agent"


class _Element:
    def __init__(self, text, click):
        self.text = text
        self._click = click

    def click(self, **_kwargs):
        self._click()


class _Body:
    def __init__(self, page):
        self.page = page

    @property
    def text(self):
        return self.page.text


class _Page:
    def __init__(self):
        self.url = ""
        self.text = ""

    def get(self, url):
        self.url = url
        self.text = "Enter the code shown in your terminal. Continue"

    def ele(self, _selector, timeout=1):
        return _Body(self)

    def eles(self, _selector, timeout=1):
        if "Continue" in self.text:
            return [_Element("Continue", self._consent)]
        if "Authorize" in self.text:
            return [_Element("Allow", self._done)]
        return []

    def _consent(self):
        self.url = "https://accounts.x.ai/oauth2/device/consent"
        self.text = "Authorize Grok Build Allow"

    def _done(self):
        self.url = "https://accounts.x.ai/oauth2/device/done"
        self.text = "Device Authorized"


class _StickyCookiePage(_Page):
    def __init__(self):
        super().__init__()
        self.cookie_clicks = 0

    def eles(self, _selector, timeout=1):
        buttons = [_Element("Accept All Cookies", self._cookie)]
        if "Continue" in self.text:
            buttons.append(_Element("Continue", self._consent))
        elif "Authorize" in self.text:
            buttons.append(_Element("Allow", self._done))
        return buttons

    def _cookie(self):
        self.cookie_clicks += 1


class RegistrationDeviceFlowTests(unittest.TestCase):
    def test_device_request_uses_public_grok_cli_scope(self):
        session = _Session(
            [
                _Response(
                    200,
                    {
                        "device_code": "device",
                        "user_code": "ABCD-1234",
                        "verification_uri": "https://accounts.x.ai/oauth2/device",
                    },
                )
            ]
        )
        result = request_device_code(session)
        self.assertEqual(result["user_code"], "ABCD-1234")
        data = session.calls[0][1]["data"]
        self.assertEqual(data["client_id"], CLIENT_ID)
        self.assertEqual(data["scope"], SCOPE)
        self.assertNotIn("conversations:read", data["scope"])

    def test_registered_browser_clicks_continue_and_allow(self):
        page = _Page()
        approve_in_registered_browser(
            page,
            "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
            timeout=20,
        )
        self.assertIn("/done", page.url)

    def test_stale_cookie_button_is_only_clicked_once(self):
        page = _StickyCookiePage()
        with patch("grok_oauth_device.time.sleep"):
            approve_in_registered_browser(
                page,
                "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                timeout=20,
            )
        self.assertEqual(page.cookie_clicks, 1)
        self.assertIn("/done", page.url)

    def test_access_denied_is_classified_as_entitlement(self):
        session = _Session(
            [
                _Response(
                    400,
                    {"error": "invalid_grant", "error_description": "Access denied"},
                )
                for _ in range(7)
            ]
        )
        with patch("grok_oauth_device.time.sleep"):
            with self.assertRaises(DeviceFlowEntitlementDenied):
                poll_device_token(
                    session,
                    {"device_code": "device", "expires_in": 30, "interval": 1},
                    timeout=20,
                )

    def test_access_denied_race_can_recover_during_grace(self):
        session = _Session(
            [
                _Response(
                    400,
                    {"error": "invalid_grant", "error_description": "Access denied"},
                ),
                _Response(200, {"access_token": "access", "refresh_token": "refresh"}),
            ]
        )
        with patch("grok_oauth_device.time.sleep"):
            token = poll_device_token(
                session,
                {"device_code": "device", "expires_in": 30, "interval": 1},
                timeout=20,
            )
        self.assertEqual(token["refresh_token"], "refresh")

    def test_access_denied_grace_attempts_are_configurable(self):
        session = _Session(
            [
                _Response(
                    400,
                    {"error": "invalid_grant", "error_description": "Access denied"},
                ),
                _Response(200, {"access_token": "access", "refresh_token": "refresh"}),
            ]
        )
        with (
            patch.dict(
                "os.environ",
                {"GROK_REGISTER_OAUTH_DENIAL_GRACE_ATTEMPTS": "1"},
            ),
            patch("grok_oauth_device.time.sleep"),
        ):
            token = poll_device_token(
                session,
                {"device_code": "device", "expires_in": 30, "interval": 1},
                timeout=20,
            )
        self.assertEqual(token["access_token"], "access")

    def test_registered_account_setup_accepts_tos_and_birth_date(self):
        session = _Session([_Response(200, {}), _Response(204, {})])
        with patch("grok_oauth_device._new_session", return_value=session):
            result = prepare_registered_account(_SetupPage())
        self.assertTrue(result["ok"])
        self.assertIn("SetTosAcceptedVersion", session.calls[0][0])
        self.assertEqual(session.calls[1][0], "https://grok.com/rest/auth/set-birth-date")
        self.assertIn("sso=session", session.headers["Cookie"])
        self.assertNotIn("unrelated", session.headers["Cookie"])

    def test_device_endpoint_falls_back_to_direct_transport(self):
        proxy_session = object()
        direct_session = object()
        device = {
            "device_code": "device",
            "user_code": "ABCD-1234",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
        }
        with (
            patch(
                "grok_oauth_device._new_session",
                side_effect=[proxy_session, direct_session],
            ),
            patch(
                "grok_oauth_device.request_device_code",
                side_effect=[RuntimeError("SOCKS failure"), device],
            ),
            patch("grok_oauth_device.prepare_registered_account"),
            patch("grok_oauth_device.approve_in_registered_browser"),
            patch(
                "grok_oauth_device.poll_device_token",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ) as poll,
        ):
            result = mint_in_registered_browser(object(), proxy="socks5://warp:1080")
        self.assertEqual(result["access_token"], "access")
        poll.assert_called_once()
        self.assertIs(poll.call_args.args[0], direct_session)

    def test_device_endpoint_falls_back_to_requests_after_direct_tls_error(self):
        proxy_session = object()
        direct_session = object()
        standard_session = object()
        device = {
            "device_code": "device",
            "user_code": "ABCD-1234",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
        }
        with (
            patch("grok_oauth_device.prepare_registered_account"),
            patch(
                "grok_oauth_device._new_session",
                side_effect=[proxy_session, direct_session],
            ),
            patch(
                "grok_oauth_device._new_standard_session",
                return_value=standard_session,
            ),
            patch(
                "grok_oauth_device.request_device_code",
                side_effect=[RuntimeError("SOCKS failure"), RuntimeError("TLS failure"), device],
            ),
            patch("grok_oauth_device.approve_in_registered_browser"),
            patch(
                "grok_oauth_device.poll_device_token",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ) as poll,
        ):
            result = mint_in_registered_browser(object(), proxy="socks5://warp:1080")
        self.assertEqual(result["access_token"], "access")
        self.assertIs(poll.call_args.args[0], standard_session)

    def test_oauth_sidecar_keeps_attempt_id_across_final_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            sso_path = Path(tmp) / "task_1.txt"
            attempt = append_oauth_event(
                sso_path,
                email="a@example.com",
                status="pending",
            )
            append_oauth_event(
                sso_path,
                email="a@example.com",
                status="success",
                attempt_id=attempt,
                token={"access_token": "access", "refresh_token": "refresh"},
            )
            events = [
                json.loads(line)
                for line in sso_path.with_suffix(".oauth.jsonl").read_text().splitlines()
            ]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["attempt_id"], events[1]["attempt_id"])
        self.assertEqual(events[1]["access_token"], "access")


if __name__ == "__main__":
    unittest.main()

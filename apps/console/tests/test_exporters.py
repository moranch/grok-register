from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from core.base_exporter import ExporterConfig
from core.cpa_auth import (
    CPA_HEADERS,
    CPA_REDIRECT_URI,
    CPA_USERINFO_URL,
    probe_cpa_account,
    refresh_cpa_token,
    token_to_cpa_record,
)
from exporters.cpa import CpaExporter
from exporters.grok2api import Grok2APIExporter


class ExporterTests(unittest.TestCase):
    @patch("exporters.grok2api.httpx.Client")
    def test_grok2api_upgrades_legacy_endpoint_to_v3_import(self, client_cls):
        client = client_cls.return_value.__enter__.return_value
        login = Mock(status_code=200)
        login.raise_for_status.return_value = None
        login.json.return_value = {"data": {"tokens": {"accessToken": "jwt-value"}}}
        imported = Mock(status_code=200, text='event: complete\ndata: {"created":1}\n\n')
        imported.raise_for_status.return_value = None
        listed = Mock(status_code=200)
        listed.raise_for_status.return_value = None
        listed.json.return_value = {
            "data": {"items": [{"id": "7", "name": "a@example.com"}]}
        }
        synced = Mock(status_code=200, text='event: complete\ndata: {"created":1}\n\n')
        synced.raise_for_status.return_value = None
        client.post.side_effect = [login, imported, synced]
        client.get.return_value = listed
        exporter = Grok2APIExporter()
        config = ExporterConfig(
            exporter_id="grok2api",
            enabled=True,
            endpoint="http://grok2api:8000/admin/api/tokens/add",
            extra={"auth_token": "grok-admin-legacy", "admin_password": "password"},
        )

        result = exporter.push({"email": "a@example.com", "sso": "sso-value"}, config)

        self.assertTrue(result.success)
        self.assertEqual(client.post.call_count, 3)
        login_args, login_kwargs = client.post.call_args_list[0]
        self.assertEqual(login_args[0], "http://grok2api:8000/api/admin/v1/auth/login")
        self.assertEqual(login_kwargs["json"]["username"], "admin")
        import_args, import_kwargs = client.post.call_args_list[1]
        self.assertEqual(import_args[0], "http://grok2api:8000/api/admin/v1/accounts/web/import")
        self.assertEqual(import_kwargs["headers"]["Authorization"], "Bearer jwt-value")
        self.assertIn('"sso_token": "sso-value"', import_kwargs["files"]["file"][1])
        sync_args, sync_kwargs = client.post.call_args_list[2]
        self.assertEqual(sync_args[0], "http://grok2api:8000/api/admin/v1/accounts/web/sync-to-console")
        self.assertEqual(sync_kwargs["json"]["ids"], ["7"])
        self.assertTrue(result.data["console_synced"])

    @patch("exporters.cpa.upload_cpa_record", return_value="xai-a@example.com.json")
    @patch("exporters.cpa.token_to_cpa_record", return_value={"type": "xai"})
    @patch("exporters.cpa.exchange_sso_for_token", return_value={"access_token": "access"})
    def test_cpa_remote_export(self, exchange, convert, upload):
        exporter = CpaExporter()
        config = ExporterConfig(
            exporter_id="cpa",
            enabled=True,
            endpoint="http://cpa:8317",
            extra={"management_key": "secret", "probe": False},
        )

        result = exporter.push({"email": "a@example.com", "sso": "sso-value"}, config)

        self.assertTrue(result.success)
        exchange.assert_called_once()
        convert.assert_called_once()
        upload.assert_called_once()
        self.assertNotIn("access", str(result.data))

    def test_cpa_requires_destination(self):
        result = CpaExporter().push(
            {"sso": "value"},
            ExporterConfig(exporter_id="cpa", enabled=True),
        )
        self.assertFalse(result.success)
        self.assertIn("endpoint", result.message)

    def test_cpa_record_requires_refresh_token(self):
        with self.assertRaisesRegex(ValueError, "refresh_token"):
            token_to_cpa_record({"access_token": "header.payload.signature"}, "a@example.com")

    def test_cpa_record_has_complete_renewal_metadata(self):
        record = token_to_cpa_record(
            {
                "access_token": "header.payload.signature",
                "refresh_token": "refresh",
                "expires_in": 21600,
            },
            "a@example.com",
        )
        self.assertEqual(record["redirect_uri"], CPA_REDIRECT_URI)
        self.assertEqual(record["refresh_token"], "refresh")
        self.assertEqual(record["headers"], CPA_HEADERS)
        self.assertIn("x-authenticateresponse", record["headers"])

    def test_cpa_refresh_uses_standard_refresh_grant(self):
        response = Mock(status_code=200, text='{"access_token":"new-access"}')
        response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 21600,
        }
        session = Mock()
        session.request.return_value = response

        with patch("core.cpa_auth.curl_requests.Session", return_value=session):
            token = refresh_cpa_token("old-refresh", timeout=10)

        self.assertEqual(token["access_token"], "new-access")
        self.assertEqual(token["refresh_token"], "rotated-refresh")
        self.assertEqual(token["transport"], "direct")
        args, kwargs = session.request.call_args
        self.assertEqual(args[:2], ("POST", "https://auth.x.ai/oauth2/token"))
        self.assertEqual(
            kwargs["data"],
            {
                "grant_type": "refresh_token",
                "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                "refresh_token": "old-refresh",
            },
        )

    def test_cpa_refresh_preserves_non_rotated_refresh_token(self):
        response = Mock(status_code=200, text='{"access_token":"new-access"}')
        response.json.return_value = {"access_token": "new-access", "expires_in": 21600}
        session = Mock()
        session.request.return_value = response

        with patch("core.cpa_auth.curl_requests.Session", return_value=session):
            token = refresh_cpa_token("existing-refresh")

        self.assertEqual(token["refresh_token"], "existing-refresh")

    def test_cpa_refresh_falls_back_to_direct_when_proxy_times_out(self):
        proxy_session = Mock()
        proxy_session.request.side_effect = TimeoutError("proxy timed out")
        direct_response = Mock(status_code=200, text='{"access_token":"new-access"}')
        direct_response.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        direct_session = Mock()
        direct_session.request.return_value = direct_response

        with (
            patch("core.cpa_auth._identity_proxy_bypass_until", 0.0),
            patch(
                "core.cpa_auth.curl_requests.Session",
                side_effect=[proxy_session, direct_session],
            ),
        ):
            token = refresh_cpa_token(
                "existing-refresh", proxy="socks5://warp:1080", timeout=5
            )

        self.assertEqual(token["transport"], "direct_fallback")
        self.assertEqual(
            proxy_session.proxies,
            {"http": "socks5://warp:1080", "https": "socks5://warp:1080"},
        )
        self.assertEqual(direct_session.proxies, {})

    def _probe_response(self, status: int, body: str):
        response = Mock(status_code=status, text=body)
        session = Mock()
        session.request.return_value = response
        with patch("core.cpa_auth.curl_requests.Session", return_value=session):
            result = probe_cpa_account("access", timeout=10)
        self.last_probe_request = session.request.call_args
        return result

    def test_cpa_probe_confirms_oauth_identity_without_calling_a_model(self):
        result = self._probe_response(200, '{"sub":"account-1"}')
        self.assertTrue(result["ok"])
        self.assertEqual(result["account_state"], "active")
        self.assertEqual(result["probe_kind"], "account_identity")
        args, kwargs = self.last_probe_request
        self.assertEqual(args[:2], ("GET", CPA_USERINFO_URL))
        self.assertNotIn("json", kwargs)
        self.assertNotIn("model", str(kwargs).lower())

    def test_cpa_identity_probe_falls_back_to_direct_when_proxy_times_out(self):
        proxy_session = Mock()
        proxy_session.request.side_effect = TimeoutError("proxy timed out")
        direct_response = Mock(status_code=200, text='{"sub":"account-1"}')
        direct_session = Mock()
        direct_session.request.return_value = direct_response
        bypass_session = Mock()
        bypass_session.request.return_value = direct_response

        with (
            patch("core.cpa_auth._identity_proxy_bypass_until", 0.0),
            patch(
                "core.cpa_auth.curl_requests.Session",
                side_effect=[proxy_session, direct_session, bypass_session],
            ),
        ):
            result = probe_cpa_account(
                "access", proxy="socks5://warp:1080", timeout=5
            )
            bypass_result = probe_cpa_account(
                "access", proxy="socks5://warp:1080", timeout=5
            )

        self.assertTrue(result["account_alive"])
        self.assertEqual(result["transport"], "direct_fallback")
        self.assertEqual(bypass_result["transport"], "direct_bypass")
        self.assertEqual(proxy_session.proxies, {"http": "socks5://warp:1080", "https": "socks5://warp:1080"})
        self.assertEqual(direct_session.proxies, {})
        self.assertEqual(bypass_session.proxies, {})

    def test_cpa_probe_marks_explicit_suspension_as_banned(self):
        result = self._probe_response(403, '{"error":"account suspended"}')
        self.assertFalse(result["ok"])
        self.assertTrue(result["banned"])
        self.assertEqual(result["failure_kind"], "banned")
        self.assertFalse(result["refresh_recommended"])

    def test_cpa_probe_refreshes_unauthorized_token(self):
        result = self._probe_response(401, '{"error":"token expired"}')
        self.assertFalse(result["banned"])
        self.assertEqual(result["failure_kind"], "token_expired")
        self.assertTrue(result["refresh_recommended"])

    def test_cpa_probe_does_not_treat_rate_limit_or_generic_403_as_banned(self):
        limited = self._probe_response(429, '{"error":"rate limit"}')
        forbidden = self._probe_response(403, '{"error":"permission-denied"}')
        self.assertEqual(limited["failure_kind"], "rate_limited")
        self.assertFalse(limited["banned"])
        self.assertEqual(forbidden["failure_kind"], "forbidden")
        self.assertFalse(forbidden["banned"])

    def test_cpa_identity_probe_does_not_infer_liveness_from_model_errors(self):
        limited = self._probe_response(
            403,
            '{"code":"personal-team-blocked:spending-limit","error":"run out of credits"}',
        )
        denied = self._probe_response(
            403,
            '{"code":"permission-denied","error":"Access to the chat endpoint is denied"}',
        )

        for result in (limited, denied):
            self.assertFalse(result["ok"])
            self.assertFalse(result["account_alive"])
            self.assertFalse(result["delivery_eligible"])


if __name__ == "__main__":
    unittest.main()

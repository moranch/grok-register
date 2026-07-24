import gc
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import _shared
from api._cpa_runtime import CpaMintRuntime


class CpaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db_path = _shared.DB_PATH
        _shared.DB_PATH = Path(self.temp.name) / "console.db"
        _shared.init_db()
        self.runtime = CpaMintRuntime()
        self.config = {
            "enabled": True,
            "endpoint": "",
            "extra": {
                "auth_dir": self.temp.name,
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "prevalidate_enabled": True,
                "prevalidate_ttl_minutes": 60,
                "prevalidate_batch_size": 10,
                "prevalidate_scan_seconds": 30,
            },
        }

    def tearDown(self):
        _shared.DB_PATH = self.old_db_path
        gc.collect()
        self.temp.cleanup()

    def add_account(self) -> int:
        return _shared.execute(
            """
            INSERT INTO accounts
                (platform, email, sso, extra_json, status, lifecycle_status,
                 validity_status, created_at)
            VALUES ('grok', 'prevalidate@example.com', 'sso', ?, 'active',
                    'registered', 'valid', ?)
            """,
            (
                json.dumps(
                    {
                        "access_token": "existing-access",
                        "refresh_token": "existing-refresh",
                        "cpa": {
                            "status": "ready",
                            "probe": {"ok": True, "has_grok_45": True},
                            "updated_at": "2020-01-01 00:00:00",
                        },
                    }
                ),
                _shared.now_iso(),
            ),
        )

    def test_existing_valid_token_is_probed_without_mint(self):
        account_id = self.add_account()
        probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_identity",
            "probe_version": 3,
        }
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch("api._cpa_runtime.probe_cpa_account", return_value=probe) as live_probe,
            patch("api._cpa_runtime.exchange_sso_for_token") as mint,
        ):
            ok, error = self.runtime._mint_account(account_id, force=False)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        live_probe.assert_called_once()
        mint.assert_not_called()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        cpa = json.loads(row["extra_json"])["cpa"]
        self.assertTrue(cpa["probe"]["account_alive"])
        self.assertEqual(cpa["probe"]["probe_kind"], "account_identity")
        self.assertTrue(cpa["probe_checked_at"])

    def test_expired_access_token_is_renewed_with_refresh_token(self):
        account_id = self.add_account()
        expired_probe = {
            "ok": False,
            "account_alive": False,
            "status": 401,
            "error": "invalid_token",
            "failure_kind": "token_expired",
            "refresh_recommended": True,
            "banned": False,
            "probe_kind": "account_identity",
        }
        alive_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_identity",
        }
        refreshed = {
            "access_token": "renewed-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 21600,
        }

        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch(
                "api._cpa_runtime.probe_cpa_account",
                side_effect=[expired_probe, alive_probe],
            ) as live_probe,
            patch(
                "api._cpa_runtime.refresh_cpa_token", return_value=refreshed
            ) as refresh,
            patch("api._cpa_runtime.exchange_sso_for_token") as device_flow,
        ):
            ok, error = self.runtime._mint_account(account_id, force=False)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(live_probe.call_count, 2)
        refresh.assert_called_once_with(
            "existing-refresh", proxy="", timeout=30, verify_tls=True
        )
        device_flow.assert_not_called()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["access_token"], "renewed-access")
        self.assertEqual(extra["refresh_token"], "rotated-refresh")
        self.assertEqual(extra["cpa"]["mint_method"], "refresh_token")
        self.assertTrue(extra["cpa"]["probe"]["account_alive"])

    def test_refresh_failure_falls_back_to_device_flow(self):
        account_id = self.add_account()
        device_token = {
            "access_token": "device-access",
            "refresh_token": "device-refresh",
            "expires_in": 21600,
        }
        alive_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_identity",
        }

        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch(
                "api._cpa_runtime.refresh_cpa_token",
                side_effect=RuntimeError("invalid_grant"),
            ) as refresh,
            patch(
                "api._cpa_runtime.exchange_sso_for_token", return_value=device_token
            ) as device_flow,
            patch("api._cpa_runtime.probe_cpa_account", return_value=alive_probe),
        ):
            ok, error = self.runtime._mint_account(account_id, force=True)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        refresh.assert_called_once()
        device_flow.assert_called_once()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["access_token"], "device-access")
        self.assertEqual(extra["refresh_token"], "device-refresh")
        self.assertEqual(extra["cpa"]["mint_method"], "device_flow")

    def test_scheduler_enqueues_stale_undelivered_account(self):
        account_id = self.add_account()
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch.object(self.runtime, "enqueue", wraps=self.runtime.enqueue) as enqueue,
        ):
            self.runtime._schedule_prevalidation()

        enqueue.assert_called_once_with(account_id, force=False)

    def test_init_db_seeds_enabled_local_cpa_exporter(self):
        row = _shared.fetch_one("SELECT value FROM settings WHERE key='exporter_cpa'")
        self.assertIsNotNone(row)
        config = json.loads(row["value"])
        self.assertTrue(config["enabled"])
        self.assertTrue(config["extra"]["auto_mint"])
        self.assertTrue(config["extra"]["probe_required"])
        self.assertEqual(config["extra"]["auth_dir"], str(_shared.CPA_AUTH_DIR))

    def test_runtime_uses_environment_proxy_when_saved_proxy_is_empty(self):
        row = _shared.fetch_one("SELECT value FROM settings WHERE key='exporter_cpa'")
        config = json.loads(row["value"])
        config["extra"]["proxy"] = ""
        _shared.execute_no_return(
            "UPDATE settings SET value=? WHERE key='exporter_cpa'",
            (json.dumps(config),),
        )

        with patch.dict(os.environ, {"GROK_REGISTER_CPA_PROXY": "socks5://warp:1080"}):
            effective = self.runtime.config()

        self.assertEqual(effective["extra"]["proxy"], "socks5://warp:1080")

    def test_failed_prevalidation_does_not_generate_cpa_file(self):
        account_id = self.add_account()
        _shared.execute_no_return(
            "UPDATE accounts SET extra_json='{}' WHERE id=?",
            (account_id,),
        )
        failed_probe = {
            "ok": False,
            "account_alive": False,
            "error": "OAuth identity unavailable",
        }
        record = {
            "access_token": "access",
            "refresh_token": "refresh",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch("api._cpa_runtime.exchange_sso_for_token", return_value={"access_token": "access"}),
            patch("api._cpa_runtime.token_to_cpa_record", return_value=record),
            patch("api._cpa_runtime.probe_cpa_account", return_value=failed_probe),
            patch("api._cpa_runtime.write_cpa_record") as write_cpa,
        ):
            ok, error = self.runtime._mint_account(account_id, force=True)

        self.assertFalse(ok)
        self.assertIn("探测失败", error)
        write_cpa.assert_not_called()

    def test_init_db_preserves_saved_cpa_exporter_config(self):
        saved = {
            "enabled": False,
            "endpoint": "http://custom-cpa:8317",
            "extra": {"auto_mint": False, "management_key": "secret"},
        }
        _shared.execute_no_return(
            "UPDATE settings SET value=? WHERE key='exporter_cpa'",
            (json.dumps(saved),),
        )

        _shared.init_db()

        row = _shared.fetch_one("SELECT value FROM settings WHERE key='exporter_cpa'")
        self.assertEqual(json.loads(row["value"]), saved)


if __name__ == "__main__":
    unittest.main()

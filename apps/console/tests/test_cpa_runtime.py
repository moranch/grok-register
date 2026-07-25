import gc
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from api import _shared
from api._cpa_runtime import CpaMintRuntime, _permanent_credential_failure


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
                "sub_auth_dir": str(Path(self.temp.name) / "sub-auth"),
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "prevalidate_enabled": True,
                "prevalidate_ttl_minutes": 60,
                "prevalidate_batch_size": 10,
                "prevalidate_scan_seconds": 30,
                "browser_fallback": False,
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
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value={
                    "ok": False,
                    "account_alive": False,
                    "error": "session unavailable",
                    "probe_kind": "account_session",
                },
            ),
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
        session_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_session",
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
                side_effect=AssertionError("OAuth identity probe should not run"),
            ) as live_probe,
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value=session_probe,
            ) as sso_probe,
            patch(
                "api._cpa_runtime.refresh_cpa_token", return_value=refreshed
            ) as refresh,
            patch("api._cpa_runtime.exchange_sso_for_token") as device_flow,
        ):
            ok, error = self.runtime._mint_account(account_id, force=False)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        live_probe.assert_not_called()
        sso_probe.assert_called_once()
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
        self.assertEqual(extra["cpa"]["probe"]["probe_kind"], "account_session")

    def test_refresh_failure_reissues_credentials_with_device_flow(self):
        account_id = self.add_account()
        session_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_session",
        }
        device_token = {
            "access_token": "device-access",
            "refresh_token": "device-refresh",
            "expires_in": 21600,
        }

        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch(
                "api._cpa_runtime.probe_cpa_account",
                side_effect=AssertionError("OAuth identity probe should not run"),
            ),
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value=session_probe,
            ),
            patch(
                "api._cpa_runtime.refresh_cpa_token",
                side_effect=RuntimeError("invalid_grant"),
            ) as refresh,
            patch(
                "api._cpa_runtime.exchange_sso_for_token",
                return_value=device_token,
            ) as device_flow,
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
        self.assertEqual(extra["cpa"]["status"], "ready")
        self.assertTrue(extra["cpa"]["credential_ready"])
        self.assertEqual(extra["cpa"]["mint_method"], "device_flow")
        self.assertEqual(extra["cpa"]["probe"]["probe_kind"], "account_session")

    def test_protocol_denial_falls_back_to_historical_sso_browser(self):
        account_id = self.add_account()
        self.config["extra"]["browser_fallback"] = True
        session_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_session",
        }
        browser_token = {
            "access_token": "browser-access",
            "refresh_token": "browser-refresh",
            "expires_in": 21600,
            "mint_method": "historical_sso_browser_device_flow",
        }

        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value=session_probe,
            ),
            patch(
                "api._cpa_runtime.refresh_cpa_token",
                side_effect=RuntimeError("OAuth refresh failed: Access denied"),
            ),
            patch(
                "api._cpa_runtime.exchange_sso_for_token",
                side_effect=RuntimeError("OAuth token 获取失败: invalid_grant: Access denied"),
            ),
            patch(
                "grok_oauth_device.mint_in_sso_browser",
                return_value=browser_token,
            ) as browser_mint,
        ):
            ok, error = self.runtime._mint_account(account_id, force=True)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        browser_mint.assert_called_once()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["access_token"], "browser-access")
        self.assertEqual(extra["refresh_token"], "browser-refresh")
        self.assertEqual(
            extra["cpa"]["mint_method"],
            "historical_sso_browser_device_flow",
        )
        self.assertTrue(extra["cpa"]["credential_ready"])

    def test_double_oauth_rejection_marks_entitlement_without_invalidating_sso(self):
        account_id = self.add_account()
        session_probe = {
            "ok": True,
            "account_alive": True,
            "status": 200,
            "probe_kind": "account_session",
        }
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value=session_probe,
            ),
            patch(
                "api._cpa_runtime.refresh_cpa_token",
                side_effect=RuntimeError("OAuth refresh failed: Access denied"),
            ),
            patch(
                "api._cpa_runtime.exchange_sso_for_token",
                side_effect=RuntimeError("OAuth token failed: invalid_grant"),
            ),
        ):
            ok, error = self.runtime._mint_account(account_id, force=True)

        self.assertFalse(ok)
        self.assertTrue(_permanent_credential_failure(error))
        row = _shared.fetch_one(
            "SELECT validity_status, last_error, extra_json FROM accounts WHERE id=?",
            (account_id,),
        )
        extra = json.loads(row["extra_json"])
        self.assertEqual(row["validity_status"], "valid")
        self.assertIn("invalid_grant", row["last_error"])
        self.assertEqual(extra["cpa"]["failure_kind"], "oauth_entitlement_denied")
        self.assertFalse(extra["cpa"]["credential_ready"])

    def test_transient_reauthorization_failure_is_not_quarantined(self):
        error = (
            "refresh_token failed: OAuth refresh failed: Access denied; "
            "SSO reauthorization failed: connection timed out"
        )
        self.assertFalse(_permanent_credential_failure(error))

    def test_registered_session_token_fans_out_cpa_and_sub_auth(self):
        account_id = self.add_account()
        event = {
            "attempt_id": "attempt-1",
            "status": "success",
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "expires_in": 21600,
        }
        record = {
            "type": "xai",
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "email": "prevalidate@example.com",
            "sub": "principal",
        }
        probe = {
            "ok": True,
            "account_alive": True,
            "probe_kind": "account_identity",
        }
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch("api._cpa_runtime.token_to_cpa_record", return_value=record),
            patch("api._cpa_runtime.probe_cpa_account", return_value=probe),
            patch("api._cpa_runtime.write_cpa_record") as write_cpa,
            patch("api._cpa_runtime.write_sub2api_record") as write_sub,
        ):
            write_cpa.return_value = Path("xai-prevalidate@example.com.json")
            write_sub.return_value = Path("SUB2API-grok-prevalidate@example.com.json")
            ok, error = self.runtime.import_registered_oauth_token(account_id, event)
            second_ok, second_error = self.runtime.import_registered_oauth_token(account_id, event)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertTrue(second_ok)
        self.assertEqual(second_error, "")
        write_cpa.assert_called_once()
        write_sub.assert_called_once()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["access_token"], "fresh-access")
        self.assertEqual(extra["cpa"]["mint_method"], "registered_session_device_flow")
        self.assertEqual(extra["cpa"]["registration_attempt_id"], "attempt-1")
        self.assertTrue(extra["cpa"]["credential_ready"])

    def test_registration_oauth_denial_preserves_live_account(self):
        account_id = self.add_account()
        self.runtime.record_registered_oauth_denied(
            account_id,
            {
                "attempt_id": "denied-1",
                "error": "OAuth entitlement denied: invalid_grant: Access denied",
            },
        )
        row = _shared.fetch_one(
            "SELECT validity_status, lifecycle_status, extra_json FROM accounts WHERE id=?",
            (account_id,),
        )
        self.assertEqual(row["validity_status"], "valid")
        self.assertEqual(row["lifecycle_status"], "registered")
        cpa = json.loads(row["extra_json"])["cpa"]
        self.assertEqual(cpa["status"], "denied")
        self.assertEqual(cpa["failure_kind"], "oauth_entitlement_denied")

    def test_initial_invalid_grant_is_a_terminal_oauth_failure(self):
        self.assertTrue(
            _permanent_credential_failure(
                "OAuth token failed: invalid_grant: Access denied"
            )
        )

    def test_startup_quarantines_persisted_double_rejection(self):
        account_id = self.add_account()
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"])
        extra["cpa"]["error"] = (
            "refresh_token failed: OAuth refresh failed: Access denied; "
            "SSO reauthorization failed: invalid_grant"
        )
        _shared.execute_no_return(
            "UPDATE accounts SET extra_json=? WHERE id=?",
            (json.dumps(extra), account_id),
        )

        self.assertEqual(self.runtime._quarantine_known_permanent_failures(), 1)
        row = _shared.fetch_one(
            "SELECT validity_status, extra_json FROM accounts WHERE id=?",
            (account_id,),
        )
        self.assertEqual(row["validity_status"], "valid")
        self.assertEqual(
            json.loads(row["extra_json"])["cpa"]["failure_kind"],
            "oauth_entitlement_denied",
        )

    def test_scheduler_enqueues_stale_undelivered_account(self):
        account_id = self.add_account()
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch.object(self.runtime, "enqueue", wraps=self.runtime.enqueue) as enqueue,
        ):
            self.runtime._schedule_prevalidation()

        enqueue.assert_called_once_with(account_id, force=False)

    def test_scheduler_skips_oauth_entitlement_denied_account(self):
        account_id = self.add_account()
        self.runtime.record_registered_oauth_denied(
            account_id,
            {
                "attempt_id": "denied-scheduler",
                "error": "OAuth entitlement denied: invalid_grant: Access denied",
            },
        )
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch.object(self.runtime, "enqueue") as enqueue,
        ):
            self.runtime._schedule_prevalidation()
        enqueue.assert_not_called()

    def test_forced_backfill_prioritizes_deliverable_inventory(self):
        historical_id = _shared.execute(
            """
            INSERT INTO accounts
                (platform, email, sso, extra_json, status, lifecycle_status,
                 validity_status, created_at)
            VALUES ('grok', 'historical@example.com', 'old-sso', '{}', 'active',
                    'expired', 'valid', ?)
            """,
            (_shared.now_iso(),),
        )
        candidate_id = self.add_account()

        with patch.object(self.runtime, "enqueue", return_value=True) as enqueue:
            job = self.runtime.enqueue_backfill(limit=1, force=True)

        self.assertNotEqual(historical_id, candidate_id)
        enqueue.assert_called_once_with(candidate_id, force=True, job_id=job["id"])
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["queued"], 1)

    def test_init_db_seeds_enabled_local_cpa_exporter(self):
        row = _shared.fetch_one("SELECT value FROM settings WHERE key='exporter_cpa'")
        self.assertIsNotNone(row)
        config = json.loads(row["value"])
        self.assertTrue(config["enabled"])
        self.assertTrue(config["extra"]["auto_mint"])
        self.assertTrue(config["extra"]["probe_required"])
        self.assertEqual(config["extra"]["auth_dir"], str(_shared.CPA_AUTH_DIR))
        self.assertEqual(config["extra"]["sub_auth_dir"], str(_shared.SUB_AUTH_DIR))

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

    def test_worker_count_uses_environment_and_clamps_range(self):
        with patch.dict(os.environ, {"GROK_REGISTER_CPA_WORKERS": "8"}):
            self.assertEqual(self.runtime.worker_count(), 8)
        with patch.dict(os.environ, {"GROK_REGISTER_CPA_WORKERS": "999"}):
            self.assertEqual(self.runtime.worker_count(), 16)
        with patch.dict(os.environ, {"GROK_REGISTER_CPA_WORKERS": "0"}):
            self.assertEqual(self.runtime.worker_count(), 1)
        with patch.dict(os.environ, {"GROK_REGISTER_CPA_WORKERS": "invalid"}):
            self.assertEqual(self.runtime.worker_count(), 6)

    def test_runtime_processes_validation_queue_with_multiple_workers(self):
        release = threading.Event()
        all_started = threading.Event()
        state_lock = threading.Lock()
        state = {"active": 0, "max_active": 0, "started": 0}

        def mint_account(_account_id, *, force):
            self.assertFalse(force)
            with state_lock:
                state["active"] += 1
                state["started"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["started"] >= 3:
                    all_started.set()
            release.wait(5)
            with state_lock:
                state["active"] -= 1
            return True, ""

        for account_id in (101, 102, 103):
            self.assertTrue(self.runtime.enqueue(account_id))

        started_concurrently = False
        try:
            with (
                patch.dict(os.environ, {"GROK_REGISTER_CPA_WORKERS": "3"}),
                patch.object(self.runtime, "_schedule_prevalidation"),
                patch.object(self.runtime, "_mint_account", side_effect=mint_account),
            ):
                self.runtime.start()
                started_concurrently = all_started.wait(3)
                self.assertEqual(
                    [thread.name for thread in self.runtime._threads],
                    ["cpa-mint-1", "cpa-mint-2", "cpa-mint-3"],
                )
                release.set()
                deadline = time.monotonic() + 3
                while self.runtime._pending and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(self.runtime._pending)
        finally:
            release.set()
            self.runtime.stop()

        self.assertTrue(started_concurrently)
        self.assertEqual(state["max_active"], 3)

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
            patch(
                "api._cpa_runtime.probe_grok_account_session",
                return_value={
                    "ok": False,
                    "account_alive": False,
                    "error": "session invalid",
                    "probe_kind": "account_session",
                },
            ),
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

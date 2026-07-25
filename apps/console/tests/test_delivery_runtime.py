import gc
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from api import _delivery_runtime
from api import _shared


class DeliveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db_path = _shared.DB_PATH
        self.old_manifest_path = _delivery_runtime.MANIFEST_PATH
        _shared.DB_PATH = Path(self.temp.name) / "console.db"
        _delivery_runtime.MANIFEST_PATH = Path(self.temp.name) / "manifest.json"
        _shared.init_db()

    def tearDown(self):
        _shared.DB_PATH = self.old_db_path
        _delivery_runtime.MANIFEST_PATH = self.old_manifest_path
        gc.collect()
        self.temp.cleanup()

    def add_account(self, email: str, sub: str, platform: str = "grok") -> int:
        return _shared.execute(
            """
            INSERT INTO accounts
                (platform, email, password, sso, extra_json, status,
                 lifecycle_status, validity_status, created_at)
            VALUES (?, ?, 'password', 'sso', ?, 'active',
                    'registered', 'valid', ?)
            """,
            (
                platform,
                email,
                json.dumps(
                    {
                        "sub": sub,
                        "access_token": f"access-{sub}",
                        "refresh_token": f"refresh-{sub}",
                    }
                ),
                _shared.now_iso(),
            ),
        )

    def test_dynamic_cards_select_only_the_bound_platform(self):
        grok_id = self.add_account("grok@example.com", "grok-sub")
        kiro_id = self.add_account("kiro@example.com", "kiro-sub", platform="kiro")

        reservation = _delivery_runtime.reserve(
            "KIRO-CARD",
            platform="kiro",
        )

        lease = _shared.fetch_one(
            "SELECT account_id, probe_json FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        order = _shared.fetch_one(
            "SELECT platform, required_model FROM delivery_orders WHERE id=?",
            (reservation["order_id"],),
        )
        probe = json.loads(lease["probe_json"])
        self.assertNotEqual(int(lease["account_id"]), grok_id)
        self.assertEqual(int(lease["account_id"]), kiro_id)
        self.assertEqual(order["platform"], "kiro")
        self.assertEqual(order["required_model"], "")
        self.assertEqual(probe["probe_kind"], "platform_inventory")
        self.assertEqual(reservation["platform"], "kiro")
        with self.assertRaisesRegex(
            _delivery_runtime.DeliveryConflict,
            "another platform",
        ):
            _delivery_runtime.reserve("KIRO-CARD", platform="grok")

    def test_manual_delivery_rejects_mixed_platform_accounts(self):
        grok_id = self.add_account("manual-grok@example.com", "manual-grok")
        kiro_id = self.add_account(
            "manual-kiro@example.com", "manual-kiro", platform="kiro"
        )
        with self.assertRaisesRegex(
            _delivery_runtime.DeliveryConflict,
            "cannot mix",
        ):
            _delivery_runtime.prepare_selected_request([grok_id, kiro_id], "mixed")

    def set_prevalidation(self, account_id: int, checked_at: str) -> None:
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"] or "{}")
        extra["cpa"] = {
            "status": "ready",
            "probe": {
                "ok": True,
                "account_alive": True,
                "delivery_eligible": True,
                "status": 200,
                "probe_kind": "account_identity",
                "probe_version": 3,
            },
            "probe_checked_at": checked_at,
            "updated_at": checked_at,
        }
        _shared.execute_no_return(
            "UPDATE accounts SET extra_json=? WHERE id=?",
            (json.dumps(extra), account_id),
        )

    def test_recent_prevalidation_skips_live_probe(self):
        account_id = self.add_account("cached@example.com", "cached-sub")
        self.set_prevalidation(account_id, _shared.now_iso())

        reservation = _delivery_runtime.reserve("CACHED-CARD")

        lease = _shared.fetch_one(
            "SELECT account_id, probe_json FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        probe = json.loads(lease["probe_json"])
        self.assertEqual(int(lease["account_id"]), account_id)
        self.assertTrue(probe["cache_hit"])

    def test_sso_session_requires_fresh_delivery_credentials(self):
        account_id = self.add_account("session@example.com", "session-sub")
        row = _shared.fetch_one("SELECT extra_json FROM accounts WHERE id=?", (account_id,))
        extra = json.loads(row["extra_json"] or "{}")
        extra["cpa"] = {
            "credential_ready": False,
            "probe_checked_at": _shared.now_iso(),
            "probe": {
                "ok": True,
                "account_alive": True,
                "probe_kind": "account_session",
            },
        }
        _shared.execute_no_return(
            "UPDATE accounts SET extra_json=? WHERE id=?",
            (json.dumps(extra), account_id),
        )

        with self.assertRaisesRegex(
            _delivery_runtime.DeliveryUnavailable,
            "no recently verified account",
        ):
            _delivery_runtime.reserve("SESSION-NOT-READY")

        extra["cpa"]["credential_ready"] = True
        _shared.execute_no_return(
            "UPDATE accounts SET extra_json=? WHERE id=?",
            (json.dumps(extra), account_id),
        )
        reservation = _delivery_runtime.reserve("SESSION-READY")
        self.assertEqual(reservation["state"], "ready")

    def test_recent_prevalidation_is_prioritized(self):
        stale_id = self.add_account("stale@example.com", "stale-sub")
        recent_id = self.add_account("recent@example.com", "recent-sub")
        self.set_prevalidation(
            stale_id,
            (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.set_prevalidation(recent_id, _shared.now_iso())

        reservation = _delivery_runtime.reserve("PRIORITY-CARD")

        lease = _shared.fetch_one(
            "SELECT account_id FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        self.assertEqual(int(lease["account_id"]), recent_id)

    def test_expired_prevalidation_is_rejected_without_live_probe(self):
        account_id = self.add_account("expired-cache@example.com", "expired-cache-sub")
        self.set_prevalidation(
            account_id,
            (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        with patch("api._cpa_runtime.cpa_mint_runtime._mint_account") as mint:
            with self.assertRaisesRegex(
                _delivery_runtime.DeliveryUnavailable,
                "no recently verified account",
            ):
                _delivery_runtime.reserve("EXPIRED-CACHE-CARD")

        mint.assert_not_called()
        self.assertIsNone(
            _shared.fetch_one(
                "SELECT id FROM account_delivery_leases WHERE account_id=?", (account_id,)
            )
        )

    def test_alive_account_can_be_delivered_without_model_validation(self):
        account_id = self.add_account("alive@example.com", "alive-sub")
        self.set_prevalidation(account_id, _shared.now_iso())
        reservation = _delivery_runtime.reserve("ALIVE-CARD")

        lease = _shared.fetch_one(
            "SELECT account_id, state, probe_json FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        self.assertEqual(int(lease["account_id"]), account_id)
        self.assertEqual(lease["state"], "ready")
        self.assertTrue(json.loads(lease["probe_json"])["account_alive"])

    def test_unverified_account_is_rejected_without_mint(self):
        account_id = self.add_account("unverified@example.com", "unverified-sub")
        with patch("api._cpa_runtime.cpa_mint_runtime._mint_account") as mint:
            with self.assertRaisesRegex(
                _delivery_runtime.DeliveryUnavailable,
                "no recently verified account",
            ):
                _delivery_runtime.reserve("UNVERIFIED-CARD")

        mint.assert_not_called()
        account = _shared.fetch_one(
            "SELECT validity_status, lifecycle_status FROM accounts WHERE id=?",
            (account_id,),
        )
        self.assertEqual(account["validity_status"], "valid")
        self.assertEqual(account["lifecycle_status"], "registered")

    def test_zero_verified_stock_returns_without_probing_any_account(self):
        self.add_account("first-transient@example.com", "first-transient")
        self.add_account("second-transient@example.com", "second-transient")
        with patch.object(_delivery_runtime, "_probe_account") as probe:
            with self.assertRaisesRegex(
                _delivery_runtime.DeliveryUnavailable,
                "no recently verified account",
            ):
                _delivery_runtime.reserve("TRANSIENT-CARD")

        probe.assert_not_called()
        self.assertEqual(
            _shared.fetch_one("SELECT COUNT(*) count FROM account_delivery_leases")["count"],
            0,
        )

    def test_stale_probing_lease_is_recovered_after_two_minutes(self):
        account_id = self.add_account("stale-lease@example.com", "stale-lease")
        self.set_prevalidation(account_id, _shared.now_iso())
        old_time = (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
        future_expiry = (datetime.now() + timedelta(minutes=7)).strftime("%Y-%m-%d %H:%M:%S")
        order_id = _shared.execute(
            """
            INSERT INTO delivery_orders
                (card_key, platform, required_model, state, source, created_at, updated_at)
            VALUES ('STALE-LEASE-CARD', 'grok', '', 'pending', 'dynamic', ?, ?)
            """,
            (old_time, old_time),
        )
        _shared.execute_no_return(
            """
            INSERT INTO account_delivery_leases
                (id, order_id, account_id, lease_token, state, created_at, updated_at, expires_at)
            VALUES ('stale-lease', ?, ?, 'old-token', 'probing', ?, ?, ?)
            """,
            (order_id, account_id, old_time, old_time, future_expiry),
        )

        reservation = _delivery_runtime.reserve("STALE-LEASE-CARD")

        self.assertNotEqual(reservation["lease_id"], "stale-lease")
        old_lease = _shared.fetch_one(
            "SELECT state, last_error FROM account_delivery_leases WHERE id='stale-lease'"
        )
        self.assertEqual(old_lease["state"], "failed")
        self.assertEqual(old_lease["last_error"], "probe lease expired")

    def test_commit_is_idempotent_and_account_is_never_reused(self):
        first_id = self.add_account("first@example.com", "first-sub")
        second_id = self.add_account("second@example.com", "second-sub")
        self.set_prevalidation(first_id, _shared.now_iso())
        self.set_prevalidation(second_id, _shared.now_iso())
        first = _delivery_runtime.reserve("CARD-1")
        self.assertNotIn("document", first)
        committed = _delivery_runtime.commit(
            "CARD-1", first["lease_id"], first["lease_token"], "bundle-1"
        )
        repeated = _delivery_runtime.commit(
            "CARD-1", first["lease_id"], first["lease_token"], "bundle-1"
        )
        recovered = _delivery_runtime.reserve("CARD-1")
        second = _delivery_runtime.reserve("CARD-2")

        self.assertEqual(committed["account_id"], first_id)
        self.assertEqual(repeated["document"], committed["document"])
        self.assertEqual(recovered["document"], committed["document"])
        self.assertEqual(recovered["lease_id"], first["lease_id"])
        self.assertEqual(recovered["lease_token"], first["lease_token"])
        leased = _shared.fetch_one(
            "SELECT account_id FROM account_delivery_leases WHERE id=?",
            (second["lease_id"],),
        )
        self.assertEqual(int(leased["account_id"]), second_id)
        self.assertEqual(_delivery_runtime.by_card("CARD-1")["document"], committed["document"])

    def test_concurrent_reservations_get_different_accounts(self):
        ids = {
            self.add_account("one@example.com", "one-sub"),
            self.add_account("two@example.com", "two-sub"),
        }
        for account_id in ids:
            self.set_prevalidation(account_id, _shared.now_iso())
        barrier = threading.Barrier(2)

        def probe(_account_id, _required_model):
            barrier.wait(timeout=5)
            return {"ok": True, "model_ids": ["grok-4.5"]}

        with patch.object(_delivery_runtime, "_probe_account", side_effect=probe):
            with ThreadPoolExecutor(max_workers=2) as pool:
                reservations = list(pool.map(_delivery_runtime.reserve, ("CARD-A", "CARD-B")))

        leased_ids = {
            int(
                _shared.fetch_one(
                    "SELECT account_id FROM account_delivery_leases WHERE id=?",
                    (reservation["lease_id"],),
                )["account_id"]
            )
            for reservation in reservations
        }
        self.assertEqual(leased_ids, ids)

    def test_history_manifest_excludes_previously_bundled_account(self):
        delivered_id = self.add_account("old@example.com", "old-sub")
        fresh_id = self.add_account("fresh@example.com", "fresh-sub")
        self.set_prevalidation(fresh_id, _shared.now_iso())
        _delivery_runtime.MANIFEST_PATH.write_text(
            json.dumps(
                {
                    "bundles": {
                        "old-bundle": {
                            "id": "old-bundle",
                            "key": "OLD-CARD",
                            "identities": [
                                {"email": "old@example.com", "account_id": "old-sub"}
                            ],
                            "files": [f"grok-account-{delivered_id}-old_example.com.json"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        reservation = _delivery_runtime.reserve("NEW-CARD")

        consumed = _shared.fetch_one(
            "SELECT card_key FROM account_delivery_consumptions WHERE account_id=?",
            (delivered_id,),
        )
        leased = _shared.fetch_one(
            "SELECT account_id FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        self.assertEqual(consumed["card_key"], "OLD-CARD")
        self.assertEqual(int(leased["account_id"]), fresh_id)

    def test_history_import_scans_new_bundles_and_reads_zip_json(self):
        first_id = self.add_account("first-history@example.com", "first-history-sub")
        second_id = self.add_account("second-history@example.com", "second-history-sub")
        manifest = {
            "bundles": {
                "bundle-one": {
                    "id": "bundle-one",
                    "key": "HISTORY-ONE",
                    "identities": [{"email": "first-history@example.com"}],
                }
            }
        }
        _delivery_runtime.MANIFEST_PATH.write_text(json.dumps(manifest), encoding="utf-8")
        first = _delivery_runtime.import_download_gate_history()

        zip_dir = _delivery_runtime.MANIFEST_PATH.parent / "zips"
        zip_dir.mkdir()
        with zipfile.ZipFile(zip_dir / "bundle-two.zip", "w") as archive:
            archive.writestr(
                "opaque.json",
                json.dumps({"email": "second-history@example.com", "sub": "second-history-sub"}),
            )
        manifest["bundles"]["bundle-two"] = {
            "id": "bundle-two",
            "key": "HISTORY-TWO",
            "files": ["opaque.json"],
        }
        _delivery_runtime.MANIFEST_PATH.write_text(json.dumps(manifest), encoding="utf-8")
        second = _delivery_runtime.import_download_gate_history()
        repeated = _delivery_runtime.import_download_gate_history()

        self.assertEqual(first, {"bundles": 1, "accounts": 1})
        self.assertEqual(second, {"bundles": 1, "accounts": 1})
        self.assertEqual(repeated, {"bundles": 0, "accounts": 0})
        rows = _shared.fetch_all(
            "SELECT account_id, card_key FROM account_delivery_consumptions ORDER BY account_id"
        )
        self.assertEqual(
            [(int(row["account_id"]), row["card_key"]) for row in rows],
            [(first_id, "HISTORY-ONE"), (second_id, "HISTORY-TWO")],
        )

    def test_expired_probe_result_cannot_mark_lease_ready(self):
        expired_id = self.add_account("expired-probe@example.com", "expired-probe-sub")
        fresh_id = self.add_account("fresh-probe@example.com", "fresh-probe-sub")
        self.set_prevalidation(expired_id, _shared.now_iso())
        self.set_prevalidation(fresh_id, _shared.now_iso())
        probed: list[int] = []

        def probe(account_id, _required_model):
            probed.append(account_id)
            if account_id == expired_id:
                _shared.execute_no_return(
                    "UPDATE account_delivery_leases SET state='failed', last_error='expired', "
                    "updated_at=? WHERE account_id=? AND state='probing'",
                    (_shared.now_iso(), account_id),
                )
            return {"ok": True, "model_ids": ["grok-4.5"]}

        with patch.object(_delivery_runtime, "_probe_account", side_effect=probe):
            reservation = _delivery_runtime.reserve("CAS-CARD")

        leases = _shared.fetch_all(
            "SELECT account_id, state FROM account_delivery_leases ORDER BY created_at, account_id"
        )
        states = {int(row["account_id"]): row["state"] for row in leases}
        self.assertEqual(probed, [expired_id, fresh_id])
        self.assertEqual(states[expired_id], "failed")
        self.assertEqual(states[fresh_id], "ready")
        selected = _shared.fetch_one(
            "SELECT account_id FROM account_delivery_leases WHERE id=?",
            (reservation["lease_id"],),
        )
        self.assertEqual(int(selected["account_id"]), fresh_id)

    def test_manual_delivery_is_idempotent_after_consumption(self):
        account_id = self.add_account("manual@example.com", "manual-sub")
        order_id = _delivery_runtime.prepare_selected([account_id], "manual")
        account = _shared.account_list_by_ids([account_id])[0]
        document = _shared.account_delivery_document(account)
        _delivery_runtime.commit_selected(
            order_id,
            card_key="MANUAL-CARD",
            bundle_id="manual-bundle",
            documents={account_id: document},
        )
        repeated = _delivery_runtime.prepare_selected_request([account_id], "again")
        self.assertEqual(repeated["order_id"], order_id)
        self.assertEqual(repeated["state"], "consumed")
        self.assertTrue(repeated["reused"])

    def test_uncertain_manual_delivery_remains_reserved(self):
        account_id = self.add_account("uncertain@example.com", "uncertain-sub")
        prepared = _delivery_runtime.prepare_selected_request([account_id], "manual")
        _delivery_runtime.abort_selected(prepared["order_id"], "response lost")
        repeated = _delivery_runtime.prepare_selected_request([account_id], "manual")

        self.assertEqual(repeated["order_id"], prepared["order_id"])
        self.assertEqual(repeated["state"], "packing")
        lease = _shared.fetch_one(
            "SELECT state FROM account_delivery_leases WHERE order_id=?",
            (prepared["order_id"],),
        )
        self.assertEqual(lease["state"], "packing")

    def test_manifest_import_recovers_manual_order_after_process_restart(self):
        account_id = self.add_account("restart@example.com", "restart-sub")
        prepared = _delivery_runtime.prepare_selected_request([account_id], "restart")
        _delivery_runtime.MANIFEST_PATH.write_text(
            json.dumps(
                {
                    "bundles": {
                        "restart-bundle": {
                            "id": "restart-bundle",
                            "key": prepared["card_key"],
                            "title": "restart",
                            "identities": [{"email": "restart@example.com"}],
                            "files": ["restart.json"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        repeated = _delivery_runtime.prepare_selected_request([account_id], "restart")
        recovered = _delivery_runtime.recover_selected(
            repeated["order_id"],
            {
                account_id: _shared.account_delivery_document(
                    _shared.account_list_by_ids([account_id])[0]
                )
            },
        )

        self.assertEqual(repeated["order_id"], prepared["order_id"])
        self.assertEqual(repeated["state"], "consumed")
        self.assertEqual(recovered["bundle_id"], "restart-bundle")
        consumption = _shared.fetch_one(
            "SELECT lease_id, bundle_id FROM account_delivery_consumptions WHERE account_id=?",
            (account_id,),
        )
        self.assertTrue(consumption["lease_id"])
        self.assertEqual(consumption["bundle_id"], "restart-bundle")


if __name__ == "__main__":
    unittest.main()

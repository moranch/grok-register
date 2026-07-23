import gc
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from api import _delivery_runtime
from api import _inventory_runtime
from api import _shared
from api import _task_runtime


class AutoReplenishRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.old_db_path = _shared.DB_PATH
        self.old_manifest_path = _delivery_runtime.MANIFEST_PATH
        self.old_task_dir = _task_runtime.TASKS_DIR
        self.old_source_project = _task_runtime.SOURCE_PROJECT
        self.old_source_python = _task_runtime.SOURCE_VENV_PYTHON
        _shared.DB_PATH = self.root / "console.db"
        _delivery_runtime.MANIFEST_PATH = self.root / "manifest.json"
        _task_runtime.TASKS_DIR = self.root / "tasks"
        _task_runtime.SOURCE_PROJECT = self.root
        _task_runtime.SOURCE_VENV_PYTHON = Path(sys.executable)
        _shared.init_db()
        self.runtime = _inventory_runtime.AutoReplenishRuntime()
        self.runtime.save_config(
            {
                "enabled": True,
                "threshold": 100,
                "replenish_count": 100,
                "check_interval_seconds": 60,
                "cooldown_seconds": 300,
            }
        )

    def tearDown(self):
        self.runtime.stop()
        _shared.DB_PATH = self.old_db_path
        _delivery_runtime.MANIFEST_PATH = self.old_manifest_path
        _task_runtime.TASKS_DIR = self.old_task_dir
        _task_runtime.SOURCE_PROJECT = self.old_source_project
        _task_runtime.SOURCE_VENV_PYTHON = self.old_source_python
        gc.collect()
        self.temp.cleanup()

    def add_account(
        self,
        index: int,
        *,
        platform: str = "grok",
        status: str = "active",
        lifecycle_status: str = "registered",
        validity_status: str = "valid",
        extra: dict | None = None,
    ) -> int:
        return _shared.execute(
            """
            INSERT INTO accounts
                (platform, email, password, sso, extra_json, status,
                 lifecycle_status, validity_status, created_at)
            VALUES (?, ?, 'password', ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                f"account-{index}@example.com",
                f"sso-{index}",
                json.dumps(extra or {}),
                status,
                lifecycle_status,
                validity_status,
                _shared.now_iso(),
            ),
        )

    def add_accounts(self, count: int) -> None:
        for index in range(count):
            self.add_account(index)

    def add_order(self, card_key: str) -> int:
        now = _shared.now_iso()
        return _shared.execute(
            """
            INSERT INTO delivery_orders
                (card_key, required_model, state, source, created_at, updated_at)
            VALUES (?, 'grok-4.5', 'pending', 'dynamic', ?, ?)
            """,
            (card_key, now, now),
        )

    def test_stock_count_excludes_consumed_leased_and_invalid_accounts(self):
        consumed_id = self.add_account(1)
        leased_id = self.add_account(2)
        self.add_account(3)
        self.add_account(4, validity_status="invalid")
        self.add_account(5, platform="chatgpt")

        consumed_order = self.add_order("DG-CONSUMED")
        _shared.execute(
            """
            INSERT INTO account_delivery_consumptions
                (order_id, account_id, card_key, bundle_id, document_json, consumed_at)
            VALUES (?, ?, 'DG-CONSUMED', 'bundle-1', '{}', ?)
            """,
            (consumed_order, consumed_id, _shared.now_iso()),
        )
        leased_order = self.add_order("DG-LEASED")
        now = _shared.now_iso()
        _shared.execute_no_return(
            """
            INSERT INTO account_delivery_leases
                (id, order_id, account_id, lease_token, state, created_at, updated_at)
            VALUES ('lease-1', ?, ?, 'token-1', 'ready', ?, ?)
            """,
            (leased_order, leased_id, now, now),
        )

        self.assertEqual(_delivery_runtime.available_delivery_stock_count(), 1)

    def test_stock_snapshot_separates_recent_verified_accounts(self):
        recent = _shared.now_iso()
        stale = (datetime.now() - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")

        def cpa_probe(
            *,
            checked_at: str,
            ok: bool = True,
            alive: bool | None = None,
        ):
            return {
                "cpa": {
                    "probe_checked_at": checked_at,
                    "probe": {
                        "ok": ok,
                        "account_alive": ok if alive is None else alive,
                        "probe_kind": "account_identity",
                    },
                }
            }

        self.add_account(1, extra=cpa_probe(checked_at=recent))
        self.add_account(2, extra=cpa_probe(checked_at=stale))
        self.add_account(3, extra=cpa_probe(checked_at=recent, ok=False))
        self.add_account(4, extra=cpa_probe(checked_at=recent))
        self.add_account(5, extra=cpa_probe(checked_at=recent, ok=False, alive=True))

        snapshot = _delivery_runtime.delivery_stock_snapshot()

        self.assertEqual(snapshot["candidate_stock"], 5)
        self.assertEqual(snapshot["verified_stock"], 3)
        self.assertEqual(snapshot["unverified_stock"], 2)
        self.assertEqual(snapshot["replenishment_metric"], "candidate_stock")

    def test_stock_99_queues_one_task_for_100_and_deduplicates(self):
        self.add_accounts(99)

        first = self.runtime.check_now()
        second = self.runtime.check_now()

        self.assertTrue(first["triggered"])
        self.assertEqual(first["available_stock"], 99)
        self.assertEqual(first["task"]["target_count"], 100)
        self.assertFalse(second["triggered"])
        self.assertEqual(second["reason"], "active_task")
        rows = _shared.fetch_all("SELECT * FROM tasks ORDER BY id")
        self.assertEqual(len(rows), 1)
        params = json.loads(rows[0]["params_json"])
        self.assertEqual(params["extra"]["source"], "auto_replenish")
        self.assertEqual(params["extra"]["trigger_stock"], 99)

    def test_stock_100_does_not_trigger(self):
        self.add_accounts(100)

        result = self.runtime.check_now()

        self.assertFalse(result["triggered"])
        self.assertEqual(result["reason"], "stock_sufficient")
        self.assertEqual(_shared.fetch_one("SELECT COUNT(*) AS total FROM tasks")["total"], 0)

    def test_disabled_monitor_does_not_trigger(self):
        self.add_accounts(10)
        self.runtime.save_config({"enabled": False})

        result = self.runtime.check_now()

        self.assertFalse(result["triggered"])
        self.assertEqual(result["reason"], "disabled")

    def test_completed_auto_task_obeys_cooldown(self):
        self.add_accounts(99)
        first = self.runtime.check_now()
        task_id = int(first["task"]["id"])
        _shared.execute_no_return(
            "UPDATE tasks SET status='failed', finished_at=? WHERE id=?",
            (_shared.now_iso(), task_id),
        )

        cooled = self.runtime.check_now()
        forced = self.runtime.check_now(force=True)

        self.assertFalse(cooled["triggered"])
        self.assertEqual(cooled["reason"], "cooldown")
        self.assertTrue(forced["triggered"])
        self.assertEqual(_shared.fetch_one("SELECT COUNT(*) AS total FROM tasks")["total"], 2)


if __name__ == "__main__":
    unittest.main()

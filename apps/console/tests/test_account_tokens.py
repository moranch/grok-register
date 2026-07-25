import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import _shared
from core.base_exporter import PushResult


class AccountTokenTests(unittest.TestCase):
    def _row(self, *, platform="grok", sso="grok-sso", extra=None):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE accounts (
                id INTEGER, email TEXT, sso TEXT, password TEXT, task_id INTEGER,
                proxy_url TEXT, status TEXT, platform TEXT, lifecycle_status TEXT,
                plan_state TEXT, validity_status TEXT, last_error TEXT,
                last_checked_at TEXT, notes TEXT, extra_json TEXT,
                exporter_status_json TEXT, created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO accounts VALUES (1, 'a@example.com', ?, '', NULL, '', 'active', ?, "
            "'registered', 'unknown', 'valid', '', '', '', ?, '{}', '')",
            (sso, platform, json.dumps(extra or {})),
        )
        return conn.execute("SELECT * FROM accounts").fetchone()

    def test_grok_sso_is_not_mislabeled_as_access_token(self):
        account = _shared._account_row_to_dict(self._row())
        self.assertEqual(account["sso"], "grok-sso")
        self.assertEqual(account["tokens"]["session_token"], "")
        self.assertEqual(account["tokens"]["access_token"], "")

    def test_cpa_snake_case_tokens_are_exposed(self):
        account = _shared._account_row_to_dict(
            self._row(
                extra={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "id_token": "id",
                }
            )
        )
        self.assertEqual(
            account["tokens"],
            {
                "session_token": "",
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
            },
        )

    def test_account_update_persists_token_fields(self):
        old_db_path = _shared.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _shared.DB_PATH = Path(tmp) / "console.db"
            try:
                _shared.init_db()
                account_id = _shared.execute(
                    "INSERT INTO accounts (email, sso, created_at) VALUES (?, ?, ?)",
                    ("a@example.com", "grok-sso", _shared.now_iso()),
                )
                account = _shared.account_update(
                    account_id,
                    access_token="access",
                    refresh_token="refresh",
                    id_token="id",
                )
                self.assertEqual(account["tokens"]["access_token"], "access")
                self.assertEqual(account["tokens"]["refresh_token"], "refresh")
                self.assertEqual(account["tokens"]["id_token"], "id")
            finally:
                _shared.DB_PATH = old_db_path

    def test_harvest_preserves_generated_password(self):
        old_db_path = _shared.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _shared.DB_PATH = root / "console.db"
            try:
                _shared.init_db()
                _shared.log_register_event(task_id=7, ok=True, email="a@example.com")
                task_dir = root / "task_7"
                (task_dir / "sso").mkdir(parents=True)
                (task_dir / "sso" / "task_7.txt").write_text("grok-sso\n", encoding="utf-8")
                (task_dir / "console.log").write_text(
                    "注册成功 | email=a@example.com | password=StrongPass! | given=Neo | family=Lin\n",
                    encoding="utf-8",
                )
                with patch("core.cpa_auth.write_grok2api_web_record"):
                    _shared._harvest_task_accounts(7, task_dir)
                row = _shared.fetch_one("SELECT password FROM accounts WHERE email = ?", ("a@example.com",))
                self.assertIsNotNone(row)
                self.assertEqual(row["password"], "StrongPass!")
            finally:
                _shared.DB_PATH = old_db_path

    def test_harvest_pushes_each_new_account_once(self):
        old_db_path = _shared.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _shared.DB_PATH = root / "console.db"
            try:
                _shared.init_db()
                _shared.execute_no_return(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        "exporter_grok2api",
                        json.dumps(
                            {
                                "enabled": True,
                                "endpoint": "http://grok2api:8000/admin/api/tokens/add",
                                "extra": {},
                            }
                        ),
                        _shared.now_iso(),
                    ),
                )
                _shared.log_register_event(task_id=8, ok=True, email="b@example.com")
                task_dir = root / "task_8"
                (task_dir / "sso").mkdir(parents=True)
                (task_dir / "sso" / "task_8.txt").write_text("new-sso\n", encoding="utf-8")
                (task_dir / "console.log").write_text("", encoding="utf-8")
                result = PushResult(
                    success=True,
                    exporter_id="grok2api",
                    message="pushed",
                    data={"api_version": 3},
                )
                with (
                    patch("exporters.grok2api.Grok2APIExporter.push", return_value=result) as push,
                    patch("core.cpa_auth.write_grok2api_web_record"),
                ):
                    _shared._harvest_task_accounts(8, task_dir)
                    _shared._harvest_task_accounts(8, task_dir)
                self.assertEqual(push.call_count, 1)
                row = _shared.fetch_one(
                    "SELECT exporter_status_json FROM accounts WHERE email = ?",
                    ("b@example.com",),
                )
                status = json.loads(row["exporter_status_json"])
                self.assertEqual(status["grok2api"]["status"], "pushed")
            finally:
                _shared.DB_PATH = old_db_path

    def test_harvest_imports_registration_side_oauth_once(self):
        old_db_path = _shared.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _shared.DB_PATH = root / "console.db"
            try:
                _shared.init_db()
                _shared.log_register_event(task_id=9, ok=True, email="oauth@example.com")
                task_dir = root / "task_9"
                (task_dir / "sso").mkdir(parents=True)
                (task_dir / "sso" / "task_9.txt").write_text("oauth-sso\n", encoding="utf-8")
                (task_dir / "sso" / "task_9.oauth.jsonl").write_text(
                    json.dumps(
                        {
                            "attempt_id": "attempt-9",
                            "email": "oauth@example.com",
                            "status": "success",
                            "access_token": "access",
                            "refresh_token": "refresh",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (task_dir / "console.log").write_text("", encoding="utf-8")
                with (
                    patch("core.cpa_auth.write_grok2api_web_record"),
                    patch("api._shared.push_account_to_grok2api"),
                    patch(
                        "api._cpa_runtime.cpa_mint_runtime.import_registered_oauth_token",
                        return_value=(True, ""),
                    ) as imported,
                    patch("api._cpa_runtime.cpa_mint_runtime.enqueue") as enqueue,
                ):
                    _shared._harvest_task_accounts(9, task_dir)

                imported.assert_called_once()
                event = imported.call_args.args[1]
                self.assertEqual(event["attempt_id"], "attempt-9")
                enqueue.assert_not_called()
            finally:
                _shared.DB_PATH = old_db_path


if __name__ == "__main__":
    unittest.main()

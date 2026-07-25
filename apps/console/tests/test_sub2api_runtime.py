from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from api import _shared
from api._sub2api_runtime import Sub2ApiImportRuntime
from core import sub2api_client


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, url, **kwargs):
        return self.handler("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self.handler("POST", url, kwargs)

    def put(self, url, **kwargs):
        return self.handler("PUT", url, kwargs)


class Sub2ApiClientTests(unittest.TestCase):
    def test_lists_groups_with_api_key(self):
        def handler(method, url, kwargs):
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/api/v1/admin/groups"))
            self.assertEqual(kwargs["headers"], {"x-api-key": "secret"})
            return FakeResponse(
                payload={"data": {"items": [{"id": 7, "name": "Grok", "platform": "grok"}]}}
            )

        with patch.object(sub2api_client, "_client", return_value=FakeClient(handler)):
            result = sub2api_client.list_groups(
                base_url="http://sub2api", api_key="secret", auth_mode="api_key"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["groups"], [{"id": 7, "name": "Grok", "platform": "grok"}])

    def test_data_import_binds_selected_group(self):
        calls = []

        def handler(method, url, kwargs):
            calls.append((method, url, kwargs))
            if method == "POST" and url.endswith("/accounts/data"):
                document = json.loads(kwargs["json"]["data"])
                self.assertEqual(document["accounts"][0]["platform"], "grok")
                self.assertTrue(kwargs["json"]["skip_default_group_bind"])
                return FakeResponse(payload={"data": {"account_created": 1, "account_failed": 0}})
            if method == "GET" and url.endswith("/admin/accounts"):
                return FakeResponse(
                    payload={
                        "data": {
                            "items": [
                                {
                                    "id": 99,
                                    "name": "user@example.com",
                                    "credentials": {"email": "user@example.com"},
                                }
                            ]
                        }
                    }
                )
            if method == "PUT" and url.endswith("/admin/accounts/99"):
                self.assertEqual(kwargs["json"], {"group_ids": [7]})
                return FakeResponse(payload={"ok": True})
            raise AssertionError((method, url))

        record = {
            "access_token": "access",
            "refresh_token": "refresh",
            "email": "user@example.com",
        }
        with patch.object(sub2api_client, "_client", return_value=FakeClient(handler)):
            result = sub2api_client.import_record(
                record,
                group_id=7,
                base_url="http://sub2api",
                api_key="secret",
                auth_mode="api_key",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], 99)
        self.assertEqual(result["group_id"], 7)
        self.assertEqual([call[0] for call in calls], ["POST", "GET", "PUT"])

    def test_oauth_callback_rejects_wrong_state_before_network(self):
        result = sub2api_client.complete_oauth(
            session_id="session",
            state="expected",
            callback="http://127.0.0.1/callback?code=abc&state=wrong",
            email="user@example.com",
            base_url="http://sub2api",
            api_key="secret",
        )
        self.assertFalse(result["ok"])
        self.assertIn("state", result["error"])


class Sub2ApiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db_path = _shared.DB_PATH
        _shared.DB_PATH = Path(self.temp.name) / "console.db"
        _shared.init_db()
        self.runtime = Sub2ApiImportRuntime()
        self.config = {
            "enabled": True,
            "endpoint": "http://sub2api",
            "extra": {
                "base_url": "http://sub2api",
                "api_key": "secret",
                "auth_mode": "api_key",
                "group_id": 7,
                "retries": 2,
                "verify_tls": True,
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
                 validity_status, exporter_status_json, created_at)
            VALUES ('grok', 'user@example.com', 'sso', ?, 'active',
                    'registered', 'valid', '{}', ?)
            """,
            (
                json.dumps({"access_token": "access", "refresh_token": "refresh"}),
                _shared.now_iso(),
            ),
        )

    def test_import_retries_transient_failure_and_persists_stage(self):
        account_id = self.add_account()
        outcomes = [
            {"ok": False, "retryable": True, "error": "temporarily unavailable"},
            {"ok": True, "account_id": 88, "group_id": 7, "path": "data_import"},
        ]
        with (
            patch.object(self.runtime, "config", return_value=self.config),
            patch("api._sub2api_runtime.import_record", side_effect=outcomes) as importer,
            patch.object(self.runtime._stop, "wait", return_value=False),
        ):
            ok, error = self.runtime._import_account(account_id, group_id=7)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(importer.call_count, 2)
        row = _shared.fetch_one(
            "SELECT extra_json, exporter_status_json FROM accounts WHERE id=?", (account_id,)
        )
        extra = json.loads(row["extra_json"])
        stage = extra["post_process"]["imports"]["sub2api"]
        self.assertEqual(stage["status"], "completed")
        self.assertEqual(stage["attempts"], 2)
        self.assertEqual(stage["result"]["account_id"], 88)
        status = json.loads(row["exporter_status_json"])["sub2api"]
        self.assertTrue(status["ok"])

    def test_init_db_seeds_disabled_sub2api_integration(self):
        row = _shared.fetch_one("SELECT value FROM settings WHERE key='exporter_sub2api'")
        self.assertIsNotNone(row)
        config = json.loads(row["value"])
        self.assertFalse(config["enabled"])
        self.assertEqual(config["extra"]["auth_mode"], "api_key")
        self.assertEqual(config["extra"]["retries"], 2)


if __name__ == "__main__":
    unittest.main()

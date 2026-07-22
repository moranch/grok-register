from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

CONSOLE_DIR = Path(__file__).resolve().parents[1]
if str(CONSOLE_DIR) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR))

from api import _shared


class AccountExportTests(unittest.TestCase):
    def test_json_export_is_compact_credentials_only(self):
        row = {
            "id": 9,
            "email": "a@example.com",
            "password": "password",
            "sso": "sso-value",
            "platform": "grok",
            "tokens": {"access_token": "access", "refresh_token": "", "id_token": None},
            "created_at": "2026-07-16 23:00:00",
            "extra_json": "x" * 10000,
            "exporter_status_json": "{}",
            "proxy_url": "socks5://proxy",
            "status": "active",
        }
        with patch.object(_shared, "account_list", return_value=[row]):
            content, media_type, filename = _shared.export_accounts("json")

        document = json.loads(content)
        self.assertEqual(len(document), 1)
        self.assertEqual(
            set(document[0]),
            {"email", "password", "sso", "platform", "tokens", "created_at"},
        )
        self.assertEqual(document[0]["tokens"], {"access_token": "access"})
        self.assertNotIn("extra_json", content)
        self.assertEqual(media_type, "application/json; charset=utf-8")
        self.assertTrue(filename.endswith(".json"))

    def test_backup_export_keeps_internal_fields(self):
        row = {
            "id": 9,
            "email": "a@example.com",
            "extra_json": '{"quota":1}',
            "exporter_status_json": '{"cpa":{"status":"ready"}}',
        }
        with patch.object(_shared, "account_list", return_value=[row]):
            content, _, filename = _shared.export_accounts("backup")

        document = json.loads(content)
        self.assertEqual(document[0]["id"], 9)
        self.assertIn("extra_json", document[0])
        self.assertIn("exporter_status_json", document[0])
        self.assertIn("backup", filename)


if __name__ == "__main__":
    unittest.main()

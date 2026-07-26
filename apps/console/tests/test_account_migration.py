from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from api import _shared
from api._account_migration import build_migration_document, import_migration_document


class AccountMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db_path = _shared.DB_PATH
        self.old_runtime_dir = _shared.RUNTIME_DIR
        _shared.RUNTIME_DIR = Path(self.temp.name) / "runtime"
        _shared.DB_PATH = _shared.RUNTIME_DIR / "source.db"
        _shared.init_db()

    def tearDown(self):
        _shared.DB_PATH = self.old_db_path
        _shared.RUNTIME_DIR = self.old_runtime_dir
        gc.collect()
        self.temp.cleanup()

    def _insert(self, email: str, sso: str, *, sub: str, password: str = "pw") -> int:
        return _shared.execute(
            """
            INSERT INTO accounts
                (email,sso,password,proxy_url,status,lifecycle_status,plan_state,
                 validity_status,last_error,last_checked_at,notes,created_at,
                 platform,extra_json,exporter_status_json)
            VALUES (?,?,?,'','active','registered','free','valid','','','','2026-07-26 01:00:00',
                    'grok',?, '{}')
            """,
            (
                email,
                sso,
                password,
                json.dumps(
                    {
                        "sub": sub,
                        "access_token": f"access-{sub}",
                        "refresh_token": f"refresh-{sub}",
                    }
                ),
            ),
        )

    def test_export_deduplicates_same_principal_and_keeps_credentials(self):
        preferred = self._insert("user@example.com", "sso-new", sub="principal-1")
        duplicate = self._insert("", "sso-old", sub="principal-1")
        second = self._insert("second@example.com", "sso-second", sub="principal-2")

        document = build_migration_document(
            [preferred, duplicate, second],
            selection={"status": "ready"},
        )

        self.assertEqual(document["schema"], "grok-register.account-migration.v1")
        self.assertEqual(document["source_count"], 3)
        self.assertEqual(document["count"], 2)
        self.assertEqual(document["duplicates_removed"], 1)
        first = next(item for item in document["accounts"] if item["email"] == "user@example.com")
        self.assertEqual(first["sso"], "sso-new")
        self.assertIn("access-principal-1", first["extra_json"])
        self.assertEqual(document["selection"], {"status": "ready"})

    def test_import_preview_backup_idempotency_and_update(self):
        first = self._insert("user@example.com", "sso-new", sub="principal-1")
        second = self._insert("second@example.com", "sso-second", sub="principal-2")
        document = build_migration_document([first, second])

        _shared.DB_PATH = _shared.RUNTIME_DIR / "target.db"
        _shared.init_db()
        preview = import_migration_document(document, dry_run=True)
        self.assertEqual(preview["inserted"], 2)
        self.assertFalse(preview["backup"])
        self.assertEqual(_shared.fetch_one("SELECT COUNT(*) n FROM accounts")["n"], 0)

        imported = import_migration_document(document)
        self.assertEqual(imported["inserted"], 2)
        self.assertEqual(imported["updated"], 0)
        self.assertTrue(imported["backup"])
        self.assertTrue((_shared.RUNTIME_DIR / "backups" / imported["backup"]).exists())

        again = import_migration_document(document)
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(again["updated"], 0)
        self.assertEqual(again["unchanged"], 2)
        self.assertFalse(again["backup"])

        document["accounts"][0]["password"] = "rotated-password"
        updated = import_migration_document(document)
        self.assertEqual(updated["updated"], 1)
        row = _shared.fetch_one(
            "SELECT password,created_at FROM accounts WHERE LOWER(email)=LOWER(?)",
            (document["accounts"][0]["email"],),
        )
        self.assertEqual(row["password"], "rotated-password")
        self.assertEqual(row["created_at"], "2026-07-26 01:00:00")

    def test_import_rejects_wrong_schema_without_mutation(self):
        with self.assertRaises(HTTPException) as invalid:
            import_migration_document(
                {"schema": "unknown", "accounts": [{"email": "x@example.com"}]}
            )
        self.assertEqual(invalid.exception.status_code, 422)
        self.assertEqual(_shared.fetch_one("SELECT COUNT(*) n FROM accounts")["n"], 0)

    def test_import_merge_does_not_erase_existing_credentials(self):
        self._insert("kept@example.com", "existing-sso", sub="principal-keep", password="kept-pw")
        document = {
            "schema": "grok-register.account-migration.v1",
            "accounts": [
                {
                    "email": "",
                    "sso": "rotated-sso",
                    "password": "",
                    "platform": "grok",
                    "extra_json": json.dumps({"sub": "principal-keep", "new_field": 1}),
                    "exporter_status_json": "{}",
                }
            ],
        }

        result = import_migration_document(document)

        self.assertEqual(result["updated"], 1)
        row = _shared.fetch_one("SELECT * FROM accounts WHERE email='kept@example.com'")
        self.assertEqual(row["password"], "kept-pw")
        self.assertEqual(row["sso"], "rotated-sso")
        extra = json.loads(row["extra_json"])
        self.assertEqual(extra["access_token"], "access-principal-keep")
        self.assertEqual(extra["new_field"], 1)


if __name__ == "__main__":
    unittest.main()

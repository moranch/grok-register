import json
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi import HTTPException
from starlette.requests import Request

from api import _delivery_runtime
from api import _shared
from api import accounts as accounts_api


class _Response:
    ok = True
    text = ""

    def __init__(self, key: str):
        self.key = key

    def json(self):
        return {
            "ok": True,
            "bundle_id": "bundle-1",
            "key": self.key,
            "file_count": 1,
            "title": "Selected accounts",
        }


class AccountDeliveryTests(unittest.TestCase):
    def test_delivery_document_keeps_credentials_and_cpa_metadata(self):
        document = _shared.account_delivery_document(
            {
                "id": 12,
                "platform": "grok",
                "email": "user@example.com",
                "password": "password",
                "sso": "sso-value",
                "tokens": {
                    "session_token": "",
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "id_token": "id",
                },
                "task_id": 4,
                "proxy_url": "",
                "status": "active",
                "lifecycle_status": "registered",
                "plan_state": "free",
                "validity_status": "valid",
                "last_error": "",
                "last_checked_at": "",
                "notes": "",
                "created_at": "2026-07-13 12:00:00",
                "extra_json": json.dumps(
                    {
                        "sub": "xai-principal",
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "base_url": "https://api.x.ai",
                        "token_endpoint": "https://example/token",
                        "headers": {"User-Agent": "test"},
                        "cpa": {"status": "ready"},
                    }
                ),
                "exporter_status_json": json.dumps({"grok2api": {"ok": True}}),
            }
        )
        self.assertEqual(document["schema"], "grok-register.account-delivery.v1")
        self.assertEqual(document["account_id"], "xai-principal")
        self.assertEqual(document["credentials"]["password"], "password")
        self.assertEqual(document["credentials"]["sso"], "sso-value")
        self.assertEqual(document["credentials"]["refresh_token"], "refresh")
        self.assertEqual(document["cpa_auth"]["type"], "xai")
        self.assertEqual(document["cpa_auth"]["auth_kind"], "oauth")
        self.assertFalse(document["cpa_auth"]["disabled"])
        self.assertEqual(document["extra"]["cpa"]["status"], "ready")
        self.assertTrue(document["exporter_status"]["grok2api"]["ok"])

    def test_selected_accounts_are_sent_to_download_gate(self):
        old_db_path = _shared.DB_PATH
        old_manifest_path = _delivery_runtime.MANIFEST_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _shared.DB_PATH = Path(tmp) / "console.db"
            _delivery_runtime.MANIFEST_PATH = Path(tmp) / "manifest.json"
            try:
                _shared.init_db()
                account_id = _shared.execute(
                    """
                    INSERT INTO accounts
                        (platform, email, password, sso, extra_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "grok",
                        "user@example.com",
                        "password",
                        "sso-value",
                        json.dumps({"access_token": "access", "refresh_token": "refresh"}),
                        _shared.now_iso(),
                    ),
                )
                request = Request({"type": "http", "headers": [], "query_string": b""})
                payload = _shared.AccountDeliveryCreate(
                    account_ids=[account_id], title="Selected accounts"
                )
                with (
                    patch.object(accounts_api, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-token"),
                    patch.object(accounts_api, "DOWNLOAD_GATE_PUBLIC_URL", "http://localhost:8787"),
                    patch.object(
                        accounts_api.requests,
                        "post",
                        side_effect=lambda *args, **kwargs: _Response(kwargs["json"]["key"]),
                    ) as post,
                ):
                    result = accounts_api.api_accounts_delivery(request, payload)
                sent = post.call_args.kwargs["json"]
                self.assertEqual(len(sent["files"]), 1)
                self.assertEqual(sent["files"][0]["data"]["email"], "user@example.com")
                self.assertEqual(sent["files"][0]["data"]["access_token"], "access")
                self.assertEqual(result["key"], sent["key"])
                self.assertEqual(result["claim_url"], f"http://localhost:8787/?key={sent['key']}")
            finally:
                _shared.DB_PATH = old_db_path
                _delivery_runtime.MANIFEST_PATH = old_manifest_path
                gc.collect()

    def test_lost_gate_response_recovers_same_bundle_without_releasing_account(self):
        old_db_path = _shared.DB_PATH
        old_manifest_path = _delivery_runtime.MANIFEST_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _shared.DB_PATH = Path(tmp) / "console.db"
            _delivery_runtime.MANIFEST_PATH = Path(tmp) / "manifest.json"
            try:
                _shared.init_db()
                account_id = _shared.execute(
                    """
                    INSERT INTO accounts
                        (platform, email, password, sso, extra_json, status,
                         lifecycle_status, validity_status, created_at)
                    VALUES ('grok', 'lost@example.com', 'password', 'sso', '{}', 'active',
                            'registered', 'valid', ?)
                    """,
                    (_shared.now_iso(),),
                )
                request = Request({"type": "http", "headers": [], "query_string": b""})
                payload = _shared.AccountDeliveryCreate(account_ids=[account_id], title="Lost response")

                def create_then_timeout(*_args, **kwargs):
                    key = kwargs["json"]["key"]
                    _delivery_runtime.MANIFEST_PATH.write_text(
                        json.dumps(
                            {
                                "bundles": {
                                    "recovered-bundle": {
                                        "id": "recovered-bundle",
                                        "key": key,
                                        "title": "Lost response",
                                        "file_count": 1,
                                        "files": ["lost.json"],
                                    }
                                }
                            }
                        ),
                        encoding="utf-8",
                    )
                    raise requests.Timeout("response lost")

                with (
                    patch.object(accounts_api, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-token"),
                    patch.object(accounts_api, "DOWNLOAD_GATE_PUBLIC_URL", "http://localhost:8787"),
                    patch.object(accounts_api.requests, "post", side_effect=create_then_timeout),
                ):
                    first = accounts_api.api_accounts_delivery(request, payload)
                with (
                    patch.object(accounts_api, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-token"),
                    patch.object(accounts_api, "DOWNLOAD_GATE_PUBLIC_URL", "http://localhost:8787"),
                    patch.object(accounts_api.requests, "post") as repeated_post,
                ):
                    repeated = accounts_api.api_accounts_delivery(request, payload)

                self.assertEqual(first["bundle_id"], "recovered-bundle")
                self.assertEqual(repeated["key"], first["key"])
                repeated_post.assert_not_called()
                consumption = _shared.fetch_one(
                    "SELECT card_key, bundle_id FROM account_delivery_consumptions WHERE account_id=?",
                    (account_id,),
                )
                self.assertEqual(consumption["card_key"], first["key"])
                self.assertEqual(consumption["bundle_id"], "recovered-bundle")
            finally:
                _shared.DB_PATH = old_db_path
                _delivery_runtime.MANIFEST_PATH = old_manifest_path
                gc.collect()

    def test_uncertain_gate_response_is_explicit_and_keeps_account_reserved(self):
        old_db_path = _shared.DB_PATH
        old_manifest_path = _delivery_runtime.MANIFEST_PATH
        with tempfile.TemporaryDirectory() as tmp:
            _shared.DB_PATH = Path(tmp) / "console.db"
            _delivery_runtime.MANIFEST_PATH = Path(tmp) / "manifest.json"
            try:
                _shared.init_db()
                account_id = _shared.execute(
                    """
                    INSERT INTO accounts
                        (platform, email, password, sso, extra_json, status,
                         lifecycle_status, validity_status, created_at)
                    VALUES ('grok', 'uncertain@example.com', 'password', 'sso', '{}', 'active',
                            'registered', 'valid', ?)
                    """,
                    (_shared.now_iso(),),
                )
                request = Request({"type": "http", "headers": [], "query_string": b""})
                payload = _shared.AccountDeliveryCreate(account_ids=[account_id], title="Uncertain")
                with (
                    patch.object(accounts_api, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-token"),
                    patch.object(accounts_api.requests, "post", side_effect=requests.Timeout("lost")),
                ):
                    with self.assertRaises(HTTPException) as first_error:
                        accounts_api.api_accounts_delivery(request, payload)
                with (
                    patch.object(accounts_api, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-token"),
                    patch.object(accounts_api.requests, "post") as repeated_post,
                ):
                    with self.assertRaises(HTTPException) as repeated_error:
                        accounts_api.api_accounts_delivery(request, payload)

                self.assertEqual(first_error.exception.status_code, 502)
                self.assertIn("账号已安全保留且不会再次分配", str(first_error.exception.detail))
                self.assertEqual(repeated_error.exception.status_code, 409)
                self.assertIn("恢复原卡密", str(repeated_error.exception.detail))
                repeated_post.assert_not_called()
                lease = _shared.fetch_one(
                    "SELECT state FROM account_delivery_leases WHERE account_id=?",
                    (account_id,),
                )
                self.assertEqual(lease["state"], "packing")
            finally:
                _shared.DB_PATH = old_db_path
                _delivery_runtime.MANIFEST_PATH = old_manifest_path
                gc.collect()


if __name__ == "__main__":
    unittest.main()

import gc
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from api import _shared
from api import _delivery_runtime
from api import accounts as accounts_api


def _request(authorization: str = "") -> Request:
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request({"type": "http", "headers": headers, "query_string": b""})


class InternalAccountInventoryTests(unittest.TestCase):
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

    def _add_account(
        self,
        email: str,
        *,
        status: str = "active",
        lifecycle_status: str = "registered",
        validity_status: str = "valid",
        extra: dict | str | None = None,
        platform: str = "grok",
    ) -> int:
        if isinstance(extra, str):
            extra_json = extra
        else:
            extra_json = json.dumps(extra or {})
        return _shared.execute(
            """
            INSERT INTO accounts
                (email, sso, password, status, lifecycle_status, validity_status,
                 plan_state, platform, extra_json, last_error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'free', ?, ?, ?, ?)
            """,
            (
                email,
                f"sso-secret-{email}",
                f"password-secret-{email}",
                status,
                lifecycle_status,
                validity_status,
                platform,
                extra_json,
                "",
                _shared.now_iso(),
            ),
        )

    def _seed_inventory(self) -> dict[str, int]:
        checked_at = _shared.now_iso()
        ready_id = self._add_account(
            "ready@example.com",
            extra={
                "access_token": "access-super-secret",
                "refresh_token": "refresh-super-secret",
                "id_token": "id-super-secret",
                "password": "nested-password-secret",
                "cpa": {
                    "status": "ready",
                    # account_response probes are delivery-ready even when the
                    # legacy credential_ready flag was not written.
                    "credential_ready": False,
                    "probe_checked_at": checked_at,
                    "probe": {
                        "account_alive": True,
                        "probe_kind": "account_response",
                    },
                },
            },
        )
        unverified_id = self._add_account("unverified@example.com", extra="not-json")
        invalid_id = self._add_account(
            "invalid@example.com",
            validity_status="invalid",
            extra={"cpa": {"status": "failed", "failure_kind": "token_expired"}},
        )
        delivered_id = self._add_account("delivered@example.com")
        leased_id = self._add_account("leased@example.com")

        delivered_order_id = _shared.execute(
            """
            INSERT INTO delivery_orders
                (card_key, platform, state, created_at, updated_at)
            VALUES ('DELIVERED-CARD', 'grok', 'consumed', ?, ?)
            """,
            (checked_at, checked_at),
        )
        _shared.execute(
            """
            INSERT INTO account_delivery_consumptions
                (order_id, account_id, card_key, document_json, consumed_at)
            VALUES (?, ?, 'DELIVERED-CARD', ?, ?)
            """,
            (
                delivered_order_id,
                delivered_id,
                json.dumps({"access_token": "document-access-secret"}),
                checked_at,
            ),
        )

        leased_order_id = _shared.execute(
            """
            INSERT INTO delivery_orders
                (card_key, platform, state, created_at, updated_at)
            VALUES ('LEASED-CARD', 'grok', 'pending', ?, ?)
            """,
            (checked_at, checked_at),
        )
        _shared.execute(
            """
            INSERT INTO account_delivery_leases
                (id, order_id, account_id, lease_token, state, created_at, updated_at)
            VALUES ('lease-1', ?, ?, 'lease-token-secret', 'ready', ?, ?)
            """,
            (leased_order_id, leased_id, checked_at, checked_at),
        )
        return {
            "ready": ready_id,
            "unverified": unverified_id,
            "invalid": invalid_id,
            "delivered": delivered_id,
            "leased": leased_id,
        }

    def test_internal_route_requires_download_gate_bearer(self):
        with patch.object(_shared, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-secret"):
            with self.assertRaises(HTTPException) as denied:
                accounts_api.api_internal_accounts(
                    _request("Bearer wrong"),
                    search="",
                    status="all",
                    platform="grok",
                    page=1,
                    page_size=50,
                )
            self.assertEqual(denied.exception.status_code, 401)

        with patch.object(_shared, "DOWNLOAD_GATE_INTERNAL_TOKEN", ""):
            with self.assertRaises(HTTPException) as unavailable:
                accounts_api.api_internal_accounts(
                    _request(),
                    search="",
                    status="all",
                    platform="grok",
                    page=1,
                    page_size=50,
                )
            self.assertEqual(unavailable.exception.status_code, 503)

    def test_inventory_summary_filters_and_pagination(self):
        ids = self._seed_inventory()
        result = accounts_api._internal_account_list(page=1, page_size=2)

        self.assertEqual(
            result["summary"],
            {
                "total": 5,
                "ready": 1,
                "unverified": 1,
                "invalid": 1,
                "delivered": 1,
                "leased": 1,
            },
        )
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["pages"], 3)
        self.assertEqual(len(result["items"]), 2)

        ready = accounts_api._internal_account_list(status="ready")
        self.assertEqual(ready["total"], 1)
        self.assertEqual(ready["items"][0]["id"], ids["ready"])
        self.assertTrue(ready["items"][0]["recently_verified"])
        self.assertFalse(ready["items"][0]["credential_ready"])
        self.assertTrue(ready["items"][0]["account_alive"])
        self.assertEqual(ready["items"][0]["probe_kind"], "account_response")

        by_email = accounts_api._internal_account_list(search="READY@EXAMPLE")
        self.assertEqual(by_email["total"], 1)
        self.assertEqual(by_email["items"][0]["id"], ids["ready"])
        by_id = accounts_api._internal_account_list(search=str(ids["invalid"]))
        self.assertEqual(by_id["total"], 1)
        self.assertEqual(by_id["items"][0]["id"], ids["invalid"])
        literal_wildcard = accounts_api._internal_account_list(search="%")
        self.assertEqual(literal_wildcard["total"], 0)

    def test_response_never_contains_credentials_or_raw_extra_json(self):
        self._seed_inventory()
        with patch.object(_shared, "DOWNLOAD_GATE_INTERNAL_TOKEN", "internal-secret"):
            result = accounts_api.api_internal_accounts(
                _request("Bearer internal-secret"),
                search="",
                status="all",
                platform="grok",
                page=1,
                page_size=50,
            )

        forbidden_keys = {
            "password",
            "sso",
            "access_token",
            "refresh_token",
            "id_token",
            "extra_json",
            "document_json",
            "lease_token",
        }

        def assert_safe(value):
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value.keys()))
                for nested in value.values():
                    assert_safe(nested)
            elif isinstance(value, list):
                for nested in value:
                    assert_safe(nested)

        assert_safe(result)
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in (
            "access-super-secret",
            "refresh-super-secret",
            "id-super-secret",
            "nested-password-secret",
            "document-access-secret",
            "lease-token-secret",
            "sso-secret-",
            "password-secret-",
        ):
            self.assertNotIn(secret, serialized)

    def test_ready_rules_match_delivery_stock_snapshot(self):
        now = _shared.now_iso()
        stale = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

        def probe(kind: str, credential_ready: bool, checked_at: str) -> dict:
            return {
                "access_token": f"access-{kind}-{credential_ready}-{checked_at}",
                "refresh_token": f"refresh-{kind}-{credential_ready}-{checked_at}",
                "cpa": {
                    "status": "ready",
                    "credential_ready": credential_ready,
                    "probe_checked_at": checked_at,
                    "probe": {"account_alive": True, "probe_kind": kind},
                },
            }

        self._add_account(
            "response@example.com",
            extra=probe("account_response", False, now),
        )
        self._add_account(
            "session-ready@example.com",
            extra=probe("account_session", True, now),
        )
        self._add_account(
            "session-not-ready@example.com",
            extra=probe("account_session", False, now),
        )
        self._add_account(
            "stale@example.com",
            extra=probe("account_identity", False, stale),
        )

        delivery_stock = _delivery_runtime.delivery_stock_snapshot("grok")
        inventory = accounts_api._internal_account_list()
        self.assertEqual(delivery_stock["candidate_stock"], 4)
        self.assertEqual(delivery_stock["verified_stock"], 2)
        self.assertEqual(inventory["summary"]["ready"], 2)
        self.assertEqual(inventory["summary"]["unverified"], 2)
        self.assertEqual(
            {item["email"] for item in inventory["items"] if item["recently_verified"]},
            {"response@example.com", "session-ready@example.com"},
        )


if __name__ == "__main__":
    unittest.main()

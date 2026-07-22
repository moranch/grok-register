import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("download_gate_server.py")
SPEC = importlib.util.spec_from_file_location("download_gate_server_test", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(gate)


class DynamicCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp.name)
        self.patches = [
            patch.object(gate, "DATA_DIR", data_dir),
            patch.object(gate, "ZIP_DIR", data_dir / "zips"),
            patch.object(gate, "JSON_DIR", data_dir / "jsons"),
            patch.object(gate, "BACKUP_DIR", data_dir / "backups"),
            patch.object(gate, "MANIFEST_PATH", data_dir / "manifest.json"),
            patch.object(gate, "ANNOUNCEMENT_PATH", data_dir / "announcement.json"),
            patch.object(gate, "ADMIN_PASSWORD_PATH", data_dir / "admin_password.txt"),
        ]
        for item in self.patches:
            item.start()
        gate.ensure_dirs()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_historical_keys_are_migrated_to_permanent_cards(self):
        manifest = {
            "bundles": {
                "old-bundle": {
                    "id": "old-bundle",
                    "key": "OLD-CARD",
                    "created_at": "2026-07-01 00:00:00",
                    "bound_client": "fingerprint",
                    "bound_at": "2026-07-02 00:00:00",
                }
            },
            "keys": {"OLD-CARD": "old-bundle"},
        }
        gate.save_manifest(manifest)
        migrated = gate.load_manifest()
        self.assertEqual(migrated["cards"]["OLD-CARD"]["status"], "claimed")
        self.assertEqual(migrated["cards"]["OLD-CARD"]["bundle_id"], "old-bundle")

    def test_issue_cards_creates_empty_ledger_entries(self):
        manifest = gate.load_manifest()
        keys = gate.issue_cards(manifest, 3, "batch-a")
        gate.save_manifest(manifest)
        self.assertEqual(len(set(keys)), 3)
        for key in keys:
            self.assertEqual(manifest["cards"][key]["status"], "issued")
            self.assertEqual(manifest["cards"][key]["bundle_id"], "")
            self.assertNotIn(key, manifest["keys"])

    def test_cards_are_bound_to_one_target_platform(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "kiro-batch", platform="kiro")[0]
        gate.save_manifest(manifest)
        self.assertEqual(manifest["cards"][key]["platform"], "kiro")
        self.assertEqual(manifest["cards"][key]["required_model"], "")

        responses = [
            {
                "order_id": "order-kiro",
                "lease_id": "lease-kiro",
                "lease_token": "token-kiro",
                "state": "ready",
                "platform": "kiro",
            },
            {
                "order_id": "order-kiro",
                "account_id": "kiro-account",
                "document": {
                    "platform": "kiro",
                    "account_id": "kiro-account",
                    "email": "kiro@example.com",
                    "password": "secret",
                },
            },
        ]
        with patch.object(gate, "console_json_post", side_effect=responses) as post:
            _bundle_id, bundle = gate.provision_card_bundle(manifest, key)

        reserve_payload = post.call_args_list[0].args[1]
        self.assertEqual(reserve_payload["platform"], "kiro")
        self.assertEqual(reserve_payload["required_model"], "")
        self.assertEqual(bundle["platform"], "kiro")
        self.assertEqual(manifest["cards"][key]["platform"], "kiro")

    def test_auto_replenish_panel_distinguishes_verified_and_candidate_stock(self):
        status = {
            "config": {"enabled": True, "threshold": 100, "replenish_count": 100},
            "candidate_stock": 185,
            "verified_stock": 17,
            "unverified_stock": 168,
            "prevalidate_ttl_minutes": 60,
            "last_reason": "stock_sufficient",
        }

        with patch.object(gate, "load_auto_replenish_status", return_value=(status, "")):
            panel = gate.admin_auto_replenish_panel()

        self.assertIn("近期验活可交付（60 分钟）", panel)
        self.assertIn(">17</strong>", panel)
        self.assertIn("未通过近期验活", panel)
        self.assertIn(">168</strong>", panel)
        self.assertIn("候选库存（补货口径）", panel)
        self.assertIn(">185</strong>", panel)

    def test_public_pool_summary_uses_recent_verified_stock(self):
        status = {
            "candidate_stock": 185,
            "verified_stock": 17,
            "unverified_stock": 168,
            "prevalidate_ttl_minutes": 60,
            "required_model": "grok-4.5",
        }

        with patch.object(gate, "load_auto_replenish_status", return_value=(status, "")):
            summary = gate.public_pool_summary()

        self.assertTrue(summary["pool"]["availableKnown"])
        self.assertEqual(summary["pool"]["availableCount"], 17)
        self.assertEqual(summary["pool"]["candidateCount"], 185)
        self.assertEqual(summary["pool"]["unverifiedCount"], 168)
        self.assertEqual(summary["pool"]["verificationModel"], "grok-4.5")

    def test_pickup_page_contains_live_pool_component(self):
        page = gate.user_page().decode("utf-8")

        self.assertIn('id="livePoolAvailable"', page)
        self.assertIn("/api/pool-summary", page)
        self.assertIn("兑换时再次现场验活", page)

    def test_generated_card_keys_use_readable_groups(self):
        manifest = gate.load_manifest()
        keys = gate.issue_cards(manifest, 25, "format-check")

        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertRegex(key, r"^DG-(?:[A-HJ-NP-Z2-9]{4}-){2}[A-HJ-NP-Z2-9]{4}$")
            self.assertNotRegex(key, r"[01IO]")

    def test_legacy_ungrouped_card_key_remains_compatible(self):
        legacy_key = "DG-ABCDEFGHJKLMNP"
        manifest = gate.load_manifest()
        manifest["cards"][legacy_key] = {"key": legacy_key, "status": "issued", "bundle_id": ""}

        self.assertEqual(gate.normalize_key(legacy_key.lower()), legacy_key)
        self.assertFalse(gate.is_card_key_available(manifest, legacy_key))

    def test_unknown_card_is_rejected_before_console_call(self):
        manifest = gate.load_manifest()
        with patch.object(gate, "console_json_post") as post:
            with self.assertRaises(KeyError):
                gate.provision_card_bundle(manifest, "DG-UNKNOWN")
        post.assert_not_called()

    def test_first_claim_commits_once_and_reuses_same_bundle(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        document = {
            "account_id": "acct-1",
            "email": "user@example.com",
            "sso": "sso-secret",
            "credentials": {"sso": "sso-secret"},
            "cpa_auth": {
                "type": "xai",
                "auth_kind": "oauth",
                "email": "user@example.com",
                "sub": "acct-1",
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "id_token": "id-secret",
                "token_type": "Bearer",
                "expires_in": 21600,
                "expired": "2026-07-14T06:00:00Z",
                "last_refresh": "2026-07-14T00:00:00Z",
                "redirect_uri": "http://127.0.0.1:56121/callback",
                "token_endpoint": "https://auth.x.ai/oauth2/token",
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "disabled": False,
                "headers": {"x-grok-client-version": "0.2.93"},
            },
        }
        responses = [
            {"order_id": "order-1", "lease_id": "lease-1", "lease_token": "token-1", "state": "ready"},
            {"order_id": "order-1", "account_id": "acct-1", "document": document},
        ]
        with patch.object(gate, "console_json_post", side_effect=responses) as post:
            first_id, first_bundle = gate.provision_card_bundle(manifest, key)
            second_id, second_bundle = gate.provision_card_bundle(manifest, key)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(first_id, second_id)
        self.assertEqual(first_bundle["account_id"], "acct-1")
        self.assertEqual(second_bundle["id"], first_id)
        self.assertEqual(manifest["cards"][key]["status"], "claimed")
        self.assertEqual(manifest["keys"][key], first_id)
        json_path = gate.JSON_DIR / f"{first_id}.json"
        self.assertTrue(json_path.exists())
        self.assertFalse((gate.ZIP_DIR / f"{first_id}.zip").exists())
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(first_bundle["download_format"], "json")
        self.assertEqual(first_bundle["json_name"], "xai-user@example.com.json")
        self.assertEqual(gate.bundle_download_path(first_id, first_bundle), f"/download/{first_id}.json")
        self.assertEqual(gate.bundle_download_name(first_id, first_bundle), "xai-user_example.com.json")
        self.assertEqual(payload["type"], "xai")
        self.assertEqual(payload["sub"], "acct-1")
        self.assertEqual(payload["sso"], "sso-secret")
        self.assertNotIn("account_id", payload)
        self.assertNotIn("credentials", payload)

    def test_existing_delivery_zip_is_migrated_to_flat_cpa_json(self):
        manifest = gate.load_manifest()
        bundle_id = "legacy-wrapper"
        document = {
            "schema": "grok-register.account-delivery.v1",
            "account_id": "acct-legacy",
            "email": "legacy@example.com",
            "sso": "legacy-sso",
            "account_state": {"status": "active"},
            "cpa_auth": {
                "type": "xai",
                "auth_kind": "oauth",
                "email": "legacy@example.com",
                "sub": "acct-legacy",
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
                "disabled": False,
                "headers": {},
            },
        }
        zip_path = gate.ZIP_DIR / f"{bundle_id}.zip"
        with gate.zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("legacy.json", json.dumps(document))
        manifest["bundles"][bundle_id] = {
            "id": bundle_id,
            "files": ["legacy.json"],
            "file_count": 1,
            "size": zip_path.stat().st_size,
        }

        self.assertEqual(gate.migrate_existing_bundle_json(manifest), 1)

        with gate.zipfile.ZipFile(zip_path) as archive:
            self.assertEqual(archive.namelist(), ["xai-legacy@example.com.json"])
            payload = json.loads(archive.read(archive.namelist()[0]))
        self.assertEqual(set(payload), {
            "type", "auth_kind", "email", "sub", "access_token", "refresh_token",
            "id_token", "token_type", "disabled", "headers", "sso",
        })
        self.assertEqual(manifest["bundles"][bundle_id]["format"], "cpa-flat-v1")

    def test_existing_dynamic_zip_is_converted_to_direct_json(self):
        manifest = gate.load_manifest()
        bundle_id = "legacy-dynamic"
        zip_path = gate.ZIP_DIR / f"{bundle_id}.zip"
        with gate.zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("legacy@example.com.json", json.dumps({"email": "legacy@example.com"}))
        manifest["bundles"][bundle_id] = {
            "id": bundle_id,
            "key": "LEGACY-CARD",
            "dynamic": True,
            "files": ["legacy@example.com.json"],
            "file_count": 1,
            "size": zip_path.stat().st_size,
        }

        self.assertEqual(gate.migrate_existing_bundle_json(manifest), 1)

        json_path = gate.JSON_DIR / f"{bundle_id}.json"
        self.assertFalse(zip_path.exists())
        self.assertTrue(json_path.exists())
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["email"], "legacy@example.com")
        self.assertEqual(manifest["bundles"][bundle_id]["download_format"], "json")

    def test_failure_returns_card_to_issued_and_retry_reuses_lease(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        first_responses = [
            {"order_id": "order-1", "lease_id": "lease-1", "lease_token": "token-1", "state": "ready"},
            RuntimeError("commit failed"),
        ]
        with patch.object(gate, "console_json_post", side_effect=first_responses):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                gate.provision_card_bundle(manifest, key)
        self.assertEqual(manifest["cards"][key]["status"], "issued")
        self.assertEqual(manifest["cards"][key]["lease_id"], "lease-1")

        committed = {"account_id": "acct-1", "document": {"account_id": "acct-1", "email": "user@example.com"}}
        with patch.object(gate, "console_json_post", return_value=committed) as post:
            gate.provision_card_bundle(manifest, key)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/commit"))
        self.assertEqual(manifest["cards"][key]["status"], "claimed")

    def test_consumed_reserve_rebuilds_missing_bundle_with_stable_id(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        document = {"account_id": "acct-1", "email": "recovered@example.com"}
        consumed = {
            "state": "consumed",
            "order_id": "order-1",
            "account_id": "acct-1",
            "document": document,
        }
        with patch.object(gate, "console_json_post", return_value=consumed) as post, patch.object(
            gate, "console_json_get"
        ) as get:
            bundle_id, bundle = gate.provision_card_bundle(manifest, key)

        self.assertEqual(bundle_id, gate.deterministic_bundle_id(key))
        self.assertEqual(bundle["account_id"], "acct-1")
        self.assertEqual(post.call_count, 1)
        get.assert_not_called()
        payload = json.loads((gate.JSON_DIR / f"{bundle_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["email"], "recovered@example.com")

    def test_batch_download_wraps_direct_json_files(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        committed = {
            "account_id": "acct-1",
            "document": {"account_id": "acct-1", "email": "batch@example.com"},
        }
        with patch.object(
            gate,
            "console_json_post",
            side_effect=[
                {"state": "ready", "lease_id": "lease-1", "lease_token": "token-1"},
                committed,
            ],
        ):
            bundle_id, bundle = gate.provision_card_bundle(manifest, key)
        batch = {"items": [{"key": key, "bundle_id": bundle_id}]}
        data = gate.build_batch_zip_bytes(manifest, batch)
        with gate.zipfile.ZipFile(gate.io.BytesIO(data)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].endswith(gate.bundle_download_name(bundle_id, bundle)))
            payload = json.loads(archive.read(names[0]))
        self.assertEqual(payload["email"], "batch@example.com")

    def test_consumed_reserve_without_document_recovers_by_card(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        recovered = {
            "state": "consumed",
            "order_id": "order-1",
            "account_id": "acct-1",
            "document": {"account_id": "acct-1", "email": "lookup@example.com"},
        }
        with patch.object(gate, "console_json_post", return_value={"state": "consumed"}), patch.object(
            gate, "console_json_get", return_value=recovered
        ) as get:
            bundle_id, _bundle = gate.provision_card_bundle(manifest, key)
        self.assertEqual(bundle_id, gate.deterministic_bundle_id(key))
        get.assert_called_once()

    def test_same_card_concurrent_provision_only_consumes_once(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "batch-a")[0]
        gate.save_manifest(manifest)
        calls: list[str] = []
        calls_lock = threading.Lock()

        def post(path, payload):
            with calls_lock:
                calls.append(path)
            if path.endswith("/reserve"):
                time.sleep(0.05)
                return {
                    "state": "ready",
                    "order_id": "order-1",
                    "lease_id": "lease-1",
                    "lease_token": "token-1",
                }
            return {
                "state": "consumed",
                "order_id": "order-1",
                "account_id": "acct-1",
                "document": {"account_id": "acct-1", "email": "once@example.com"},
            }

        results: list[str] = []
        failures: list[Exception] = []

        def worker():
            try:
                results.append(gate.provision_card_bundle({}, key)[0])
            except Exception as exc:
                failures.append(exc)

        with patch.object(gate, "console_json_post", side_effect=post):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(calls), 2)

    def test_batch_keeps_successful_item_when_later_card_fails(self):
        manifest = gate.load_manifest()
        first, second = gate.issue_cards(manifest, 2, "batch-a")
        gate.save_manifest(manifest)

        def post(path, payload):
            key = payload["card_key"]
            if key == second:
                raise RuntimeError("probe failed")
            if path.endswith("/reserve"):
                return {
                    "state": "ready",
                    "order_id": "order-1",
                    "lease_id": "lease-1",
                    "lease_token": "token-1",
                }
            return {
                "state": "consumed",
                "order_id": "order-1",
                "account_id": "acct-1",
                "document": {"account_id": "acct-1", "email": "partial@example.com"},
            }

        handler = SimpleNamespace(headers={"User-Agent": "test"}, client_address=("127.0.0.1", 1234))
        with patch.object(gate, "console_json_post", side_effect=post):
            items, errors, changed = gate.prepare_claim_items(
                handler,
                manifest,
                [first, second],
                "client-id",
            )

        self.assertTrue(changed)
        self.assertEqual([item["key"] for item in items], [first])
        self.assertEqual(len(errors), 1)
        self.assertIn(second, errors[0]["message"])
        saved = gate.load_manifest()
        bundle_id = saved["keys"][first]
        self.assertTrue(saved["bundles"][bundle_id]["bound_client"])
        self.assertEqual(saved["cards"][second]["status"], "issued")


if __name__ == "__main__":
    unittest.main()

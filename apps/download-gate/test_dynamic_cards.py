import importlib.util
import base64
import json
import os
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

    def test_legacy_utc_card_times_are_migrated_once_for_shanghai(self):
        manifest = {
            "bundles": {
                "old-bundle": {
                    "id": "old-bundle",
                    "key": "OLD-CARD",
                    "created_at": "2026-07-23 14:53:43",
                    "bound_at": "2026-07-23 15:09:32",
                    "bound_client": "fingerprint",
                    "batch": "batch-20260723-145343",
                }
            },
            "keys": {"OLD-CARD": "old-bundle"},
            "cards": {},
        }
        gate.save_manifest(manifest)

        with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
            migrated = gate.load_manifest()
            migrated_again = gate.load_manifest()

        bundle = migrated_again["bundles"]["old-bundle"]
        self.assertEqual(bundle["created_at"], "2026-07-23 22:53:43")
        self.assertEqual(bundle["bound_at"], "2026-07-23 23:09:32")
        self.assertEqual(bundle["batch"], "batch-20260723-225343")
        self.assertEqual(
            migrated_again["_metadata"]["timestamp_timezone"],
            "Asia/Shanghai",
        )

    def test_v1_timezone_migration_only_updates_legacy_batch_label(self):
        manifest = {
            "_metadata": {
                "timestamp_timezone": "Asia/Shanghai",
                "timestamp_migration": "legacy-utc-to-cst-v1",
            },
            "bundles": {},
            "keys": {},
            "cards": {
                "OLD-CARD": {
                    "key": "OLD-CARD",
                    "status": "issued",
                    "created_at": "2026-07-23 22:53:43",
                    "batch": "batch-20260723-145343",
                }
            },
        }
        gate.save_manifest(manifest)

        with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
            migrated = gate.load_manifest()

        card = migrated["cards"]["OLD-CARD"]
        self.assertEqual(card["created_at"], "2026-07-23 22:53:43")
        self.assertEqual(card["batch"], "batch-20260723-225343")

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
        self.assertIn("cockpit_download_url", page)
        self.assertIn("sub_download_url", page)
        self.assertIn('download="auth.json"', page)
        self.assertIn("下载 CPA JSON", page)
        self.assertIn("下载 Sub2API JSON", page)
        self.assertIn("下载 Cockpit auth.json", page)
        self.assertIn("CPA、Sub2API 与 Cockpit 是三种独立格式", page)
        self.assertIn('id="claimSubBtn"', page)
        self.assertIn('id="claimCockpitBtn"', page)
        self.assertIn("兑换时再次现场验活", page)

    def test_card_status_labels_are_localized(self):
        self.assertEqual(gate.card_status_view({"status": "issued"}), ("issued", "待验活"))
        self.assertEqual(
            gate.card_status_view({"status": "provisioning"}),
            ("provisioning", "正在验活"),
        )
        self.assertEqual(
            gate.card_status_view({"status": "claimed"}),
            ("claimed", "验活成功 · 已领取"),
        )
        self.assertEqual(
            gate.card_status_view({"status": "issued", "last_error": "timed out"}),
            ("failed", "验活失败"),
        )
        self.assertEqual(gate.CARD_STATUS_LABELS["void"], "已作废")

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
        self.assertEqual(first_bundle["json_name"], "CPA-xai-user@example.com.json")
        self.assertEqual(gate.bundle_download_path(first_id, first_bundle), f"/download/{first_id}.json")
        self.assertEqual(gate.bundle_download_name(first_id, first_bundle), "CPA-xai-user_example.com.json")
        self.assertEqual(payload["type"], "xai")
        self.assertEqual(payload["sub"], "acct-1")
        self.assertEqual(set(payload), {
            "type", "access_token", "refresh_token", "token_type", "expires_in",
            "expired", "last_refresh", "email", "sub", "base_url", "redirect_uri",
            "token_endpoint", "auth_kind", "id_token",
        })
        self.assertNotIn("sso", payload)
        self.assertNotIn("disabled", payload)
        self.assertNotIn("headers", payload)
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
            self.assertEqual(archive.namelist(), ["CPA-xai-legacy@example.com.json"])
            payload = json.loads(archive.read(archive.namelist()[0]))
        self.assertEqual(set(payload), {
            "type", "access_token", "refresh_token", "token_type", "expires_in",
            "expired", "last_refresh", "email", "sub", "base_url", "redirect_uri",
            "token_endpoint", "auth_kind", "id_token",
        })
        self.assertEqual(manifest["bundles"][bundle_id]["format"], "cpa-flat-v1")

    def test_cpa_pickup_matches_aaron_identity_precedence(self):
        def jwt(payload):
            encode = lambda value: base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).rstrip(b"=").decode()
            return f"{encode({'alg': 'none'})}.{encode(payload)}.x"

        access = jwt({"sub": "access-sub", "iat": 1784690000, "exp": 1784700000})
        id_token = jwt({
            "sub": "id-sub",
            "email": "identity@example.com",
            "iat": 1784690000,
            "exp": 1784710000,
        })
        payload = gate.cpa_import_payload({
            "type": "xai",
            "access_token": access,
            "refresh_token": "refresh",
            "id_token": id_token,
            "expires_in": 20000,
            "disabled": False,
            "headers": {"X-Test": "remove-me"},
            "sso": "remove-me",
        })

        self.assertEqual(payload["sub"], "id-sub")
        self.assertEqual(payload["email"], "identity@example.com")
        self.assertEqual(payload["expired"], gate._utc_text(1784710000))
        self.assertNotIn("disabled", payload)
        self.assertNotIn("headers", payload)
        self.assertNotIn("sso", payload)

    def test_existing_direct_cpa_json_is_rewritten_to_aaron_shape(self):
        manifest = gate.load_manifest()
        bundle_id = "legacy-direct-cpa"
        path = gate.JSON_DIR / f"{bundle_id}.json"
        path.write_text(json.dumps({
            "type": "xai",
            "email": "legacy@example.com",
            "sub": "legacy-sub",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
            "token_type": "Bearer",
            "disabled": False,
            "headers": {"X-Test": "remove-me"},
            "sso": "remove-me",
        }), encoding="utf-8")
        manifest["bundles"][bundle_id] = {
            "id": bundle_id,
            "platform": "grok",
            "dynamic": True,
            "json_name": "xai-legacy@example.com.json",
            "download_format": "json",
            "files": ["xai-legacy@example.com.json"],
            "format": "cpa-flat-v1",
        }

        self.assertEqual(gate.migrate_existing_bundle_json(manifest), 1)

        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("disabled", payload)
        self.assertNotIn("headers", payload)
        self.assertNotIn("sso", payload)
        self.assertEqual(manifest["bundles"][bundle_id]["format"], "cpa-aaron-v1")

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
        source_document = {
            "type": "xai",
            "account_id": "acct-1",
            "email": "batch@example.com",
            "sub": "acct-1",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
        committed = {
            "account_id": "acct-1",
            "document": source_document,
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
        source_path = gate.JSON_DIR / f"{bundle_id}.json"
        source_before = source_path.read_bytes()
        batch = {"items": [{"key": key, "bundle_id": bundle_id}]}
        with patch.object(gate, "console_json_post") as post:
            data = gate.build_batch_zip_bytes(manifest, batch)
            post.assert_not_called()
        self.assertEqual(source_path.read_bytes(), source_before)
        with gate.zipfile.ZipFile(gate.io.BytesIO(data)) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), 3)
            cpa_name = next(name for name in names if "/cpa/" in name)
            sub_name = next(name for name in names if "/sub2api/" in name)
            cockpit_name = next(name for name in names if name.endswith("/cockpit/auth.json"))
            self.assertTrue(cpa_name.endswith(gate.bundle_download_name(bundle_id, bundle)))
            self.assertTrue(sub_name.endswith("/sub2api/SUB2API-grok-batch_example.com.json"))
            cpa = json.loads(archive.read(cpa_name))
            sub = json.loads(archive.read(sub_name))
            cockpit = json.loads(archive.read(cockpit_name))
        self.assertEqual(cpa["email"], "batch@example.com")
        self.assertEqual(sub["type"], gate.SUB2API_DATA_TYPE)
        self.assertEqual(sub["accounts"][0]["credentials"]["access_token"], cpa["access_token"])
        self.assertEqual(sub["accounts"][0]["credentials"]["sub"], cpa["sub"])
        entry = cockpit[gate.GROK_AUTH_REGISTRY_KEY]
        self.assertEqual(entry["key"], cpa["access_token"])
        self.assertEqual(entry["refresh_token"], cpa["refresh_token"])
        self.assertEqual(entry["user_id"], cpa["sub"])
        self.assertEqual(entry["principal_id"], cpa["sub"])

    def test_cockpit_auth_payload_is_single_official_registry_account(self):
        source = {
            "type": "xai",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "email": "cockpit@example.com",
            "sub": "subject-1",
            "expired": "2026-07-23T00:00:00Z",
            "last_refresh": "2026-07-22T18:00:00Z",
        }
        original = json.loads(json.dumps(source))

        payload = gate.cockpit_auth_payload(source)

        self.assertEqual(source, original)
        self.assertEqual(set(payload), {gate.GROK_AUTH_REGISTRY_KEY})
        self.assertNotIn("type", payload)
        self.assertNotIn("access_token", payload)
        self.assertNotIn("sub", payload)
        entry = payload[gate.GROK_AUTH_REGISTRY_KEY]
        self.assertEqual(entry["key"], "access-token")
        self.assertEqual(entry["auth_mode"], "oidc")
        self.assertEqual(entry["email"], "cockpit@example.com")
        self.assertEqual(entry["refresh_token"], "refresh-token")
        self.assertEqual(entry["user_id"], "subject-1")
        self.assertEqual(entry["principal_id"], "subject-1")
        self.assertEqual(entry["principal_type"], "User")
        self.assertEqual(entry["expires_at"], "2026-07-23T00:00:00Z")
        self.assertEqual(entry["oidc_issuer"], gate.GROK_OIDC_ISSUER)
        self.assertEqual(entry["oidc_client_id"], gate.GROK_OIDC_CLIENT_ID)

    def test_sub2api_payload_is_single_grok_data_account(self):
        source = {
            "type": "xai",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "token_type": "Bearer",
            "expired": "2026-07-23T00:00:00Z",
            "email": "sub@example.com",
            "sub": "subject-1",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
            "id_token": "id-token",
        }
        original = json.loads(json.dumps(source))

        payload = gate.sub2api_payload(source)

        self.assertEqual(source, original)
        self.assertEqual(set(payload), {"type", "version", "exported_at", "proxies", "accounts"})
        self.assertEqual(payload["type"], gate.SUB2API_DATA_TYPE)
        self.assertEqual(payload["version"], gate.SUB2API_DATA_VERSION)
        self.assertEqual(payload["proxies"], [])
        self.assertEqual(len(payload["accounts"]), 1)
        account = payload["accounts"][0]
        self.assertEqual(account["name"], "sub@example.com")
        self.assertEqual(account["platform"], "grok")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["concurrency"], 1)
        self.assertEqual(account["priority"], 0)
        self.assertTrue(account["auto_pause_on_expired"])
        credentials = account["credentials"]
        self.assertEqual(credentials["access_token"], "access-token")
        self.assertEqual(credentials["refresh_token"], "refresh-token")
        self.assertEqual(credentials["expires_at"], "2026-07-23T00:00:00Z")
        self.assertEqual(credentials["email"], "sub@example.com")
        self.assertEqual(credentials["sub"], "subject-1")
        self.assertEqual(credentials["client_id"], gate.GROK_OIDC_CLIENT_ID)
        self.assertEqual(credentials["scope"], gate.GROK_OIDC_SCOPE)
        self.assertEqual(gate.sub2api_filename(source), "SUB2API-grok-sub_example.com.json")

    def test_batch_keeps_non_grok_json_layout_unchanged(self):
        manifest = gate.load_manifest()
        bundle_id = "kiro-json"
        source_path = gate.JSON_DIR / f"{bundle_id}.json"
        source_path.write_text(json.dumps({"email": "kiro@example.com"}), encoding="utf-8")
        manifest["bundles"][bundle_id] = {
            "id": bundle_id,
            "platform": "kiro",
            "json_name": "kiro-account.json",
            "download_format": "json",
            "files": ["kiro-account.json"],
        }
        batch = {"items": [{"key": "KIRO-KEY", "bundle_id": bundle_id}]}

        data = gate.build_batch_zip_bytes(manifest, batch)

        with gate.zipfile.ZipFile(gate.io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), ["01-KIRO-KEY/kiro-account.json"])

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

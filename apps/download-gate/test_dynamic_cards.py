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
from unittest.mock import Mock, patch


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
        self.assertEqual(migrated["bundles"]["old-bundle"]["bound_client"], "")

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

    def test_grokcli_variant_is_kept_with_bundle_and_removed_with_bundle(self):
        bundle_id = "bundle-with-grokcli"
        manifest = gate.load_manifest()
        manifest["bundles"][bundle_id] = {
            "id": bundle_id,
            "variants": {gate.GROKCLI2API_VARIANT: {}},
        }
        primary = gate.bundle_json_path(bundle_id)
        variant = gate.bundle_variant_json_path(bundle_id, gate.GROKCLI2API_VARIANT)
        orphan = gate.JSON_DIR / "orphan.grokcli2api.json"
        primary.write_text("{}", encoding="utf-8")
        variant.write_text("{}", encoding="utf-8")
        orphan.write_text("{}", encoding="utf-8")

        self.assertEqual(gate.clear_orphan_zips(manifest), 1)
        self.assertTrue(primary.exists())
        self.assertTrue(variant.exists())
        self.assertFalse(orphan.exists())

        self.assertEqual(gate.delete_bundle_from_manifest(manifest, bundle_id), 1)
        self.assertFalse(primary.exists())
        self.assertFalse(variant.exists())

    def test_bulk_card_parser_accepts_keys_links_and_deduplicates(self):
        parsed = gate.parse_card_keys_input(
            "DG-AAAA-BBBB-CCCC\n"
            "https://example.test/?key=DG-DDDD-EEEE-FFFF\n"
            "DG-AAAA-BBBB-CCCC, DG-GGGG-HHHH-JJJJ"
        )
        self.assertEqual(
            parsed,
            [
                "DG-AAAA-BBBB-CCCC",
                "DG-DDDD-EEEE-FFFF",
                "DG-GGGG-HHHH-JJJJ",
            ],
        )

    def test_batch_revoke_and_delete_unused_cards(self):
        manifest = gate.load_manifest()
        revoke_key, delete_key = gate.issue_cards(manifest, 2, "batch-a")

        revoked = gate.batch_manage_cards(
            manifest,
            [revoke_key, "DG-NOT-FOUND"],
            mode="revoke",
        )
        deleted = gate.batch_manage_cards(manifest, [delete_key], mode="delete")

        self.assertEqual(revoked["revoked"], 1)
        self.assertEqual(revoked["missing"], 1)
        self.assertEqual(manifest["cards"][revoke_key]["status"], "void")
        self.assertFalse(manifest["cards"][revoke_key].get("deleted", False))
        self.assertEqual(deleted["deleted"], 1)
        self.assertTrue(manifest["cards"][delete_key]["deleted"])
        self.assertEqual(manifest["cards"][delete_key]["status"], "void")
        self.assertIn(delete_key, gate.existing_card_keys(manifest))

    def test_delete_claimed_card_revokes_but_preserves_delivery_audit(self):
        key = "DG-CLAIMED-CARD"
        manifest = {
            "bundles": {
                "bundle-1": {
                    "id": "bundle-1",
                    "key": key,
                    "bound_at": "2026-07-25 01:02:03",
                }
            },
            "keys": {key: "bundle-1"},
            "cards": {
                key: {
                    "key": key,
                    "status": "claimed",
                    "bundle_id": "bundle-1",
                    "claimed_at": "2026-07-25 01:02:03",
                }
            },
        }

        result = gate.batch_manage_cards(manifest, [key], mode="delete")
        gate.save_manifest(manifest)
        reloaded = gate.load_manifest()

        self.assertEqual(result["claimed_preserved"], 1)
        self.assertIn("bundle-1", reloaded["bundles"])
        self.assertEqual(reloaded["cards"][key]["status"], "void")
        self.assertFalse(reloaded["cards"][key].get("deleted", False))
        self.assertNotIn(key, reloaded["keys"])

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

        self.assertIn("近期账号存活可交付（60 分钟）", panel)
        self.assertIn(">17</strong>", panel)
        self.assertIn("未通过账号存活验证", panel)
        self.assertIn(">168</strong>", panel)
        self.assertIn("候选库存（补货口径）", panel)
        self.assertIn(">185</strong>", panel)

    def test_public_pool_summary_uses_recent_verified_stock(self):
        status = {
            "candidate_stock": 185,
            "verified_stock": 17,
            "unverified_stock": 168,
            "prevalidate_ttl_minutes": 60,
        }

        with patch.object(gate, "load_auto_replenish_status", return_value=(status, "")):
            summary = gate.public_pool_summary()

        self.assertTrue(summary["pool"]["availableKnown"])
        self.assertEqual(summary["pool"]["availableCount"], 17)
        self.assertEqual(summary["pool"]["candidateCount"], 185)
        self.assertEqual(summary["pool"]["unverifiedCount"], 168)
        self.assertNotIn("verificationModel", summary["pool"])

    def test_pickup_page_contains_live_pool_component(self):
        page = gate.user_page().decode("utf-8")

        self.assertIn('id="livePoolAvailable"', page)
        self.assertIn("/api/pool-summary", page)
        self.assertIn("cockpit_download_url", page)
        self.assertIn("sub_download_url", page)
        self.assertIn("grokcli_download_url", page)
        self.assertIn('download="auth.json"', page)
        self.assertIn("下载 CPA JSON", page)
        self.assertIn("下载 Sub2API JSON", page)
        self.assertIn("下载 Cockpit auth.json", page)
        self.assertIn("下载 GrokCLI-2API JSON", page)
        self.assertIn("CPA、Sub2API、Cockpit 与 GrokCLI-2API 是四种独立格式", page)
        self.assertIn('id="claimSubBtn"', page)
        self.assertIn('id="claimCockpitBtn"', page)
        self.assertIn('id="claimGrokCliBtn"', page)
        self.assertNotIn('name="required_model"', page)
        self.assertIn("账号存活验证通过", page)
        self.assertIn("领取仅使用近期已验证库存", page)
        self.assertIn("暂无近期验活库存，新卡暂不可领取", page)
        self.assertNotIn("兑换时将现场尝试验活", page)
        self.assertEqual(page.count("activeButton.textContent = '正在分配'"), 1)

    def test_admin_account_proxy_forwards_filters_and_enforces_allowlist(self):
        console_payload = {
            "items": [
                {
                    "id": 42,
                    "platform": "grok",
                    "email": "safe@example.com",
                    "status": "active",
                    "lifecycle_status": "active",
                    "validity_status": "valid",
                    "plan_state": "free",
                    "created_at": "2026-07-26 01:02:03",
                    "last_checked_at": "2026-07-26 02:02:03",
                    "cpa_status": "ready",
                    "credential_ready": True,
                    "account_alive": True,
                    "probe_checked_at": "2026-07-26 02:02:03",
                    "failure_kind": "",
                    "last_error": "",
                    "delivered": False,
                    "leased": False,
                    "model_test_model": "grok-4.5",
                    "model_test_ok": False,
                    "model_test_status": 403,
                    "model_test_checked_at": "2026-07-26 03:04:05",
                    "model_test_latency_ms": 123,
                    "model_test_failure_kind": "quota_exhausted",
                    "model_test_error": "credits exhausted",
                    "password": "must-not-leak",
                    "sso": "must-not-leak",
                    "access_token": "must-not-leak",
                    "refresh_token": "must-not-leak",
                    "extra_json": {"credentials": {"access_token": "must-not-leak"}},
                }
            ],
            "page": 2,
            "page_size": 50,
            "total": 81,
            "pages": 2,
            "summary": {
                "total": 81,
                "ready": 5,
                "unverified": 60,
                "invalid": 10,
                "delivered": 6,
                "leased": 1,
                "access_token": "must-not-leak",
            },
            "access_token": "must-not-leak",
        }
        query = {
            "platform": ["grok"],
            "q": ["safe@example.com"],
            "status": ["ready"],
            "page": ["2"],
            "page_size": ["50"],
        }

        with patch.object(gate, "console_json_get", return_value=console_payload) as get:
            result = gate.load_admin_accounts(query)

        requested_path = get.call_args.args[0]
        self.assertTrue(requested_path.startswith("/api/internal/accounts?"))
        self.assertIn("search=safe%40example.com", requested_path)
        self.assertIn("status=ready", requested_path)
        self.assertIn("page=2", requested_path)
        self.assertIn("page_size=50", requested_path)
        self.assertEqual(result["summary"]["ready"], 5)
        self.assertEqual(result["items"][0]["email"], "safe@example.com")
        self.assertEqual(result["items"][0]["model_test_model"], "grok-4.5")
        self.assertFalse(result["items"][0]["model_test_ok"])
        self.assertEqual(result["items"][0]["model_test_status"], 403)
        serialized = json.dumps(result)
        for secret_field in ("password", "sso", "access_token", "refresh_token", "extra_json"):
            self.assertNotIn(secret_field, serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_admin_model_test_proxy_is_explicit_and_credential_free(self):
        console_payload = {
            "ok": True,
            "test": {
                "account_id": 42,
                "ok": False,
                "model_available": False,
                "model": "grok-4.5",
                "status": 403,
                "latency_ms": 321,
                "probe_kind": "model_response",
                "failure_kind": "model_denied",
                "refresh_recommended": False,
                "error": "permission denied",
                "checked_at": "2026-07-26 04:05:06",
                "access_token": "must-not-leak",
                "sso": "must-not-leak",
            },
        }

        with patch.object(gate, "console_json_post", return_value=console_payload) as post:
            result = gate.run_admin_account_model_test(42, "grok-4.5")

        post.assert_called_once_with(
            "/api/internal/accounts/42/model-test",
            {"model": "grok-4.5"},
            timeout_seconds=65,
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["test"]["model_available"])
        self.assertEqual(result["test"]["failure_kind"], "model_denied")
        serialized = json.dumps(result)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("sso", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_admin_model_test_handler_returns_console_result(self):
        handler = SimpleNamespace(
            read_body=lambda: json.dumps({"model": "grok-4.5"}).encode(),
            send_json=Mock(),
        )
        expected = {
            "ok": True,
            "test": {"account_id": 42, "model_available": True},
        }
        with patch.object(gate, "run_admin_account_model_test", return_value=expected) as run:
            gate.DownloadGateHandler.handle_admin_account_model_test(handler, 42)

        run.assert_called_once_with(42, "grok-4.5")
        handler.send_json.assert_called_once_with(
            expected,
            extra_headers={"Cache-Control": "no-store, max-age=0"},
        )

    def test_admin_page_contains_account_list_search_filters_and_pagination(self):
        handler = SimpleNamespace(headers={"Host": "127.0.0.1:18787"}, server=SimpleNamespace(server_port=18787))
        status = {
            "config": {"enabled": False, "threshold": 100, "replenish_count": 100},
            "candidate_stock": 0,
            "verified_stock": 0,
            "unverified_stock": 0,
        }
        with patch.object(gate, "load_auto_replenish_status", return_value=(status, "")):
            page = gate.admin_page(handler).decode("utf-8")

        self.assertIn('data-admin-tab="accounts"', page)
        self.assertIn('data-admin-panel="accounts"', page)
        self.assertIn('id="accountsSearch"', page)
        self.assertIn('id="accountsStatusFilter"', page)
        self.assertIn('id="accountsPrevPage"', page)
        self.assertIn('id="accountsNextPage"', page)
        self.assertIn('id="accountsPageSize"', page)
        self.assertIn('id="accountsModelInput"', page)
        self.assertIn("data-account-model-test", page)
        self.assertIn("模型测试", page)
        self.assertIn("不会改变取件库存判定", page)
        self.assertIn(f"{gate.ADMIN_PATH}/api/accounts?", page)
        self.assertIn("CPA 凭据", page)
        self.assertIn("账号存活", page)
        self.assertIn("最近验活", page)
        self.assertIn("失败原因", page)
        self.assertIn("注册时间", page)
        self.assertIn("账户列表暂不可用", page)

    def test_admin_account_endpoint_returns_friendly_503_when_console_is_unavailable(self):
        handler = SimpleNamespace(send_json=Mock())
        parsed = gate.urlparse(f"{gate.ADMIN_PATH}/api/accounts?status=ready")

        with patch.object(gate, "load_admin_accounts", side_effect=RuntimeError("connection refused")):
            gate.DownloadGateHandler.handle_admin_accounts(handler, parsed)

        payload, status, headers = handler.send_json.call_args.args
        self.assertFalse(payload["ok"])
        self.assertIn("无法加载 Console 账户列表", payload["error"])
        self.assertIn("connection refused", payload["error"])
        self.assertEqual(status, gate.HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")

    def test_card_status_labels_are_localized(self):
        self.assertEqual(
            gate.card_status_view({"status": "issued"}), ("issued", "未使用卡密")
        )
        self.assertEqual(
            gate.card_status_view({"status": "provisioning"}),
            ("provisioning", "正在分配"),
        )
        self.assertEqual(
            gate.card_status_view({"status": "claimed"}),
            ("claimed", "领取成功"),
        )
        self.assertEqual(
            gate.card_status_view({"status": "issued", "last_error": "timed out"}),
            ("retryable", "领取超时 · 可重试"),
        )
        self.assertEqual(
            gate.card_status_view(
                {"status": "issued", "last_error": "delivery reservation is already in progress"}
            ),
            ("retryable", "领取超时 · 可重试"),
        )
        self.assertEqual(gate.CARD_STATUS_LABELS["void"], "已作废")

    def test_generated_card_keys_use_readable_groups(self):
        manifest = gate.load_manifest()
        keys = gate.issue_cards(manifest, 25, "format-check")

        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertRegex(key, r"^DG-(?:[A-HJ-NP-Z2-9]{4}-){2}[A-HJ-NP-Z2-9]{4}$")
            self.assertNotRegex(key, r"[01IO]")

    def test_duplicate_claim_does_not_wait_for_existing_card_lock(self):
        key = "DG-LOCK-TEST-CARD"
        held = threading.Event()
        release = threading.Event()

        def hold_lock():
            with gate.card_lock(key):
                held.set()
                release.wait(timeout=5)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        self.assertTrue(held.wait(timeout=2))
        try:
            started = time.monotonic()
            with gate.try_card_lock(key) as acquired:
                self.assertFalse(acquired)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(timeout=5)

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
            "password": "register-password",
            "credentials": {"sso": "sso-secret", "password": "register-password"},
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
        grokcli_path = gate.bundle_variant_json_path(first_id, gate.GROKCLI2API_VARIANT)
        self.assertIsNotNone(grokcli_path)
        self.assertTrue(grokcli_path.exists())
        grokcli = json.loads(grokcli_path.read_text(encoding="utf-8"))
        self.assertEqual(grokcli["count"], 1)
        self.assertEqual(len(grokcli["auth"]), 1)
        grokcli_entry = next(iter(grokcli["auth"].values()))
        self.assertEqual(grokcli_entry["access_token"], "access-secret")
        self.assertEqual(grokcli_entry["refresh_token"], "refresh-secret")
        self.assertEqual(grokcli_entry["sso"], "sso-secret")
        self.assertEqual(grokcli_entry["sso_cookie"], "sso-secret")
        self.assertEqual(grokcli_entry["password"], "register-password")
        self.assertEqual(grokcli_entry["register_password"], "register-password")
        self.assertEqual(
            first_bundle["variants"][gate.GROKCLI2API_VARIANT]["file_name"],
            "grokcli-2api-auth-user_example.com.json",
        )

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
            self.assertEqual(len(names), 4)
            cpa_name = next(name for name in names if "/cpa/" in name)
            sub_name = next(name for name in names if "/sub2api/" in name)
            cockpit_name = next(name for name in names if name.endswith("/cockpit/auth.json"))
            grokcli_name = next(name for name in names if "/grokcli-2api/" in name)
            self.assertTrue(cpa_name.endswith(gate.bundle_download_name(bundle_id, bundle)))
            self.assertTrue(sub_name.endswith("/sub2api/SUB2API-grok-batch_example.com.json"))
            cpa = json.loads(archive.read(cpa_name))
            sub = json.loads(archive.read(sub_name))
            cockpit = json.loads(archive.read(cockpit_name))
            grokcli = json.loads(archive.read(grokcli_name))
        self.assertEqual(cpa["email"], "batch@example.com")
        self.assertEqual(sub["type"], gate.SUB2API_DATA_TYPE)
        self.assertEqual(sub["accounts"][0]["credentials"]["access_token"], cpa["access_token"])
        self.assertEqual(sub["accounts"][0]["credentials"]["sub"], cpa["sub"])
        entry = cockpit[gate.GROK_AUTH_REGISTRY_KEY]
        self.assertEqual(entry["key"], cpa["access_token"])
        self.assertEqual(entry["refresh_token"], cpa["refresh_token"])
        self.assertEqual(entry["user_id"], cpa["sub"])
        self.assertEqual(entry["principal_id"], cpa["sub"])
        grokcli_entry = next(iter(grokcli["auth"].values()))
        self.assertEqual(grokcli["count"], 1)
        self.assertEqual(grokcli_entry["access_token"], cpa["access_token"])
        self.assertEqual(grokcli_entry["refresh_token"], cpa["refresh_token"])
        self.assertEqual(grokcli_entry["user_id"], cpa["sub"])

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

    def test_grokcli_2api_payload_preserves_recovery_credentials(self):
        source = {
            "account_id": "subject-2",
            "email": "grokcli@example.com",
            "sso": "sso=sso-cookie-value",
            "credentials": {
                "password": "registered-password",
                "sso": "sso-cookie-value",
            },
            "cpa_auth": {
                "type": "xai",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "email": "grokcli@example.com",
                "sub": "subject-2",
                "expired": "2026-07-23T00:00:00Z",
                "last_refresh": "2026-07-22T18:00:00Z",
            },
        }
        original = json.loads(json.dumps(source))

        payload = gate.grokcli_2api_payload(source)

        self.assertEqual(source, original)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(set(payload["auth"]), {"https://auth.x.ai::subject-2"})
        entry = payload["auth"]["https://auth.x.ai::subject-2"]
        self.assertEqual(entry["key"], "access-token")
        self.assertEqual(entry["access_token"], "access-token")
        self.assertEqual(entry["refresh_token"], "refresh-token")
        self.assertEqual(entry["id_token"], "id-token")
        self.assertEqual(entry["sso"], "sso-cookie-value")
        self.assertEqual(entry["sso_cookie"], "sso-cookie-value")
        self.assertEqual(entry["password"], "registered-password")
        self.assertEqual(entry["register_password"], "registered-password")
        self.assertEqual(entry["auth_mode"], "oidc")
        self.assertEqual(entry["oidc_issuer"], gate.GROK_OIDC_ISSUER)
        self.assertEqual(entry["oidc_client_id"], gate.GROK_OIDC_CLIENT_ID)
        self.assertEqual(
            gate.grokcli_2api_filename(source),
            "grokcli-2api-auth-grokcli_example.com.json",
        )

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
            )

        self.assertTrue(changed)
        self.assertEqual([item["key"] for item in items], [first])
        self.assertEqual(len(errors), 1)
        self.assertIn(second, errors[0]["message"])
        saved = gate.load_manifest()
        bundle_id = saved["keys"][first]
        self.assertTrue(saved["bundles"][bundle_id]["bound_at"])
        self.assertFalse(saved["bundles"][bundle_id]["bound_client"])
        self.assertEqual(saved["cards"][second]["status"], "issued")

    def test_claimed_card_is_not_bound_to_browser(self):
        manifest = gate.load_manifest()
        key = gate.issue_cards(manifest, 1, "no-browser-binding")[0]
        gate.save_manifest(manifest)
        responses = [
            {
                "state": "ready",
                "order_id": "order-1",
                "lease_id": "lease-1",
                "lease_token": "token-1",
            },
            {
                "state": "consumed",
                "order_id": "order-1",
                "account_id": "acct-1",
                "document": {"account_id": "acct-1", "email": "open@example.com"},
            },
        ]
        first_handler = SimpleNamespace(
            headers={"User-Agent": "browser-one"},
            client_address=("127.0.0.1", 1234),
        )
        with patch.object(gate, "console_json_post", side_effect=responses):
            items, errors, changed = gate.prepare_claim_items(first_handler, manifest, [key])

        self.assertTrue(changed)
        self.assertFalse(errors)
        self.assertEqual([item["key"] for item in items], [key])
        saved = gate.load_manifest()
        bundle = saved["bundles"][saved["keys"][key]]
        self.assertTrue(bundle["bound_at"])
        self.assertFalse(bundle["bound_client"])

        second_handler = SimpleNamespace(
            headers={"User-Agent": "browser-two"},
            client_address=("203.0.113.5", 4321),
        )
        items, errors, _changed = gate.prepare_claim_items(second_handler, saved, [key])
        self.assertFalse(errors)
        self.assertEqual([item["key"] for item in items], [key])


if __name__ == "__main__":
    unittest.main()

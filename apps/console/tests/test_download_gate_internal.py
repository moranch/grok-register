import importlib.util
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

import requests


class DownloadGateInternalApiTests(unittest.TestCase):
    def test_bearer_token_creates_bundle(self):
        source = Path(__file__).parents[2] / "download-gate" / "download_gate_server.py"
        old_data_dir = os.environ.get("DOWNLOAD_GATE_DATA_DIR")
        old_token = os.environ.get("DOWNLOAD_GATE_INTERNAL_TOKEN")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DOWNLOAD_GATE_DATA_DIR"] = tmp
            os.environ["DOWNLOAD_GATE_INTERNAL_TOKEN"] = "test-internal-token"
            spec = importlib.util.spec_from_file_location("download_gate_test_module", source)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.DownloadGateHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            payload = {
                "title": "Selected accounts",
                "pack_mode": "bundle",
                "key": "DG-RETRY-SAFE-KEY",
                "files": [
                    {
                        "filename": "account.json",
                        "data": {
                            "schema": "grok-register.account-delivery.v1",
                            "account_id": "acct-1",
                            "email": "user@example.com",
                            "sso": "sso-value",
                            "credentials": {"sso": "sso-value"},
                            "account_state": {"status": "active"},
                            "cpa_auth": {
                                "type": "xai",
                                "auth_kind": "oauth",
                                "email": "user@example.com",
                                "sub": "acct-1",
                                "access_token": "access",
                                "refresh_token": "refresh",
                                "id_token": "id",
                                "disabled": False,
                                "headers": {},
                            },
                        },
                    },
                    {
                        "filename": "notes.json",
                        "data": {"kind": "ordinary", "value": 1},
                    },
                ],
            }
            try:
                unauthorized = requests.post(
                    f"{base_url}/api/internal/bundles",
                    headers={"Authorization": "Bearer wrong"},
                    json=payload,
                    timeout=5,
                )
                self.assertEqual(unauthorized.status_code, 401)

                response = requests.post(
                    f"{base_url}/api/internal/bundles",
                    headers={"Authorization": "Bearer test-internal-token"},
                    json=payload,
                    timeout=5,
                )
                self.assertEqual(response.status_code, 201)
                result = response.json()
                self.assertTrue(result["key"])
                self.assertEqual(result["file_count"], 2)
                manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["keys"][result["key"]], result["bundle_id"])
                zip_path = Path(tmp) / "zips" / f"{result['bundle_id']}.zip"
                with zipfile.ZipFile(zip_path) as archive:
                    packed = json.loads(archive.read("xai-user@example.com.json"))
                    ordinary = json.loads(archive.read("notes.json"))
                self.assertEqual(packed["email"], "user@example.com")
                self.assertEqual(packed["sso"], "sso-value")
                self.assertNotIn("schema", packed)
                self.assertNotIn("account_state", packed)
                self.assertEqual(ordinary, {"kind": "ordinary", "value": 1})

                replay = requests.post(
                    f"{base_url}/api/internal/bundles",
                    headers={"Authorization": "Bearer test-internal-token"},
                    json=payload,
                    timeout=5,
                )
                self.assertEqual(replay.status_code, 200)
                replay_result = replay.json()
                self.assertTrue(replay_result["idempotent_replay"])
                self.assertEqual(replay_result["bundle_id"], result["bundle_id"])
                replay_manifest = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(len(replay_manifest["bundles"]), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                if old_data_dir is None:
                    os.environ.pop("DOWNLOAD_GATE_DATA_DIR", None)
                else:
                    os.environ["DOWNLOAD_GATE_DATA_DIR"] = old_data_dir
                if old_token is None:
                    os.environ.pop("DOWNLOAD_GATE_INTERNAL_TOKEN", None)
                else:
                    os.environ["DOWNLOAD_GATE_INTERNAL_TOKEN"] = old_token


if __name__ == "__main__":
    unittest.main()

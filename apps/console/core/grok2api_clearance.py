from __future__ import annotations

import os
import threading
import time

import httpx


class Grok2ApiClearanceRefresher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._quota_refreshed = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="grok2api-clearance",
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _configure_statsig_signer(
        client: httpx.Client,
        grok2api: str,
        headers: dict[str, str],
    ) -> None:
        signer_url = os.getenv(
            "GROK2API_STATSIG_SIGNER_URL",
            "http://statsig-signer:8788/sign",
        ).strip()
        if not signer_url:
            return

        response = client.get(f"{grok2api}/api/admin/v1/settings", headers=headers)
        response.raise_for_status()
        data = response.json().get("data") or {}
        config = data.get("config") or {}
        web = config.get("providerWeb") or {}
        if web.get("statsigMode") == "url" and web.get("statsigSignerURL") == signer_url:
            return

        web["statsigMode"] = "url"
        web["statsigSignerURL"] = signer_url
        web["statsigManualValue"] = ""
        config["providerWeb"] = web
        updated = client.put(
            f"{grok2api}/api/admin/v1/settings",
            headers=headers,
            json={"revision": str(data.get("revision", 0)), "config": config},
        )
        updated.raise_for_status()
        print("[OK] Grok2API 已切换到容器内 Statsig 签名器")

    def _run(self) -> None:
        interval = max(int(os.getenv("GROK2API_CF_REFRESH_INTERVAL", "600")), 120)
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                print(f"[WARN] Grok2API clearance 刷新失败: {exc}")
            self._stop.wait(interval)

    def refresh(self) -> None:
        flaresolverr = os.getenv("FLARESOLVERR_URL", "http://flaresolverr:8191").rstrip("/")
        grok2api = os.getenv("GROK2API_INTERNAL_URL", "http://grok2api:8000").rstrip("/")
        username = os.getenv("GROK2API_ADMIN_USERNAME", "admin")
        password = os.getenv("GROK2API_ADMIN_PASSWORD", "")
        if not password:
            raise RuntimeError("GROK2API_ADMIN_PASSWORD 未配置")

        with httpx.Client(timeout=150) as client:
            solved = client.post(
                f"{flaresolverr}/v1",
                json={
                    "cmd": "request.get",
                    "url": "https://grok.com/",
                    "maxTimeout": 120000,
                },
            )
            solved.raise_for_status()
            payload = solved.json()
            solution = payload.get("solution") or {}
            if payload.get("status") != "ok":
                raise RuntimeError(payload.get("message") or "FlareSolverr 未解出挑战")

            allowed = {"cf_clearance", "__cf_bm", "_cfuvid"}
            cookies = "; ".join(
                f"{item.get('name')}={item.get('value')}"
                for item in solution.get("cookies") or []
                if item.get("name") in allowed or str(item.get("name") or "").startswith("cf_chl_")
            )
            user_agent = str(solution.get("userAgent") or "").strip()
            if "cf_clearance=" not in cookies or not user_agent:
                raise RuntimeError("FlareSolverr 响应缺少 cf_clearance 或 User-Agent")

            login = client.post(
                f"{grok2api}/api/admin/v1/auth/login",
                json={"username": username, "password": password},
            )
            login.raise_for_status()
            access_token = login.json()["data"]["tokens"]["accessToken"]
            headers = {"Authorization": f"Bearer {access_token}"}
            self._configure_statsig_signer(client, grok2api, headers)
            nodes = client.get(f"{grok2api}/api/admin/v1/egress-nodes", headers=headers)
            nodes.raise_for_status()
            items = nodes.json().get("data", {}).get("items", [])
            node = next((item for item in items if item.get("scope") == "grok_web"), None)
            node_payload = {
                "name": "direct-cf-web",
                "scope": "grok_web",
                "enabled": True,
                "clearProxyURL": True,
                "userAgent": user_agent,
                "cloudflareCookies": cookies,
            }
            if node:
                updated = client.put(
                    f"{grok2api}/api/admin/v1/egress-nodes/{node['id']}",
                    headers=headers,
                    json=node_payload,
                )
            else:
                updated = client.post(
                    f"{grok2api}/api/admin/v1/egress-nodes",
                    headers=headers,
                    json=node_payload,
                )
            updated.raise_for_status()
            print("[OK] Grok2API Cloudflare clearance 已刷新")

            if not self._quota_refreshed:
                quotas = client.post(
                    f"{grok2api}/api/admin/v1/accounts/web/refresh-quotas",
                    headers=headers,
                )
                quotas.raise_for_status()
                quota_body = quotas.text
                if "event: complete" not in quota_body or "event: error" in quota_body:
                    raise RuntimeError(f"Web 额度自动刷新未完成: {quota_body[-500:]}")
                self._quota_refreshed = True
                print("[OK] Grok2API Web 账户额度已自动刷新")


grok2api_clearance_refresher = Grok2ApiClearanceRefresher()

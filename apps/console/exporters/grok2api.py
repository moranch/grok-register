# grok2api 专用 Exporter（默认禁用）
# 将账号 token 推送到 grok2api 2.x 的 /admin/api/tokens/add 端点

from __future__ import annotations

import logging
import os
import json
from typing import Any, Dict, Optional

import httpx

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class Grok2APIExporter(BaseExporter):
    """
    grok2api 专用 Exporter。

    将注册成功的账号 token 推送到 grok2api 服务，
    用于 API 代理池的 token 补充。

    默认禁用，需要在 settings 中手动启用并配置 endpoint。
    """

    name = "grok2api"
    display_name = "grok2api"
    description = "推送 Grok Web SSO 到 grok2api 2.x/3.x 管理接口"
    config_schema = {
        "endpoint": {
            "type": "string",
            "title": "grok2api 端点",
            "description": "如 http://grok2api:8000/admin/api/tokens/add",
            "required": True,
        },
        "auth_token": {
            "type": "string",
            "title": "认证 Token",
            "description": "grok2api 管理接口的认证 token（可选）",
            "default": "",
        },
        "pool": {
            "type": "string",
            "title": "账号池",
            "description": "auto 会在导入后自动识别 basic/super/heavy",
            "default": "auto",
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        POST 账号 token 到 grok2api 的 /api/tokens 端点。

        Args:
            account_data: 账号字典（需包含 token 或 cookie）。
            config: Exporter 配置。

        Returns:
            PushResult。
        """
        endpoint = config.endpoint
        if not endpoint:
            endpoint = config.extra.get("endpoint", "")

        if not endpoint:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message="未配置 grok2api endpoint",
            )

        # 提取 token
        token = (
            account_data.get("sso", "")
            or account_data.get("token", "")
            or account_data.get("cookie", "")
            or account_data.get("sso_token", "")
        )

        if not token:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message="账号数据中未找到有效 token",
            )

        # grok2api v3 使用管理员 JWT + multipart 文件导入。旧 endpoint 配置
        # 自动升级，避免升级容器后所有注册结果静默落在本地库存。
        endpoint = endpoint.rstrip("/")
        is_v3 = "/api/admin/v1/" in endpoint or endpoint.endswith("/accounts/web/import")
        if endpoint.endswith("/admin/api/tokens/add") or endpoint.endswith("/v1/admin/tokens"):
            endpoint = endpoint.split("/admin/api/tokens/add", 1)[0].split("/v1/admin/tokens", 1)[0]
            endpoint += "/api/admin/v1/accounts/web/import"
            is_v3 = True

        if is_v3:
            base_url = endpoint.split("/api/admin/v1/", 1)[0]
            auth_token = str(config.extra.get("auth_token", "") or "").strip()
            username = str(
                config.extra.get("admin_username")
                or os.getenv("GROK2API_ADMIN_USERNAME", "admin")
            ).strip()
            password = str(
                config.extra.get("admin_password")
                or os.getenv("GROK2API_ADMIN_PASSWORD", "")
            ).strip()
            try:
                with httpx.Client(timeout=60) as client:
                    if not auth_token or auth_token.startswith("grok-admin-"):
                        if not password:
                            return PushResult(
                                success=False,
                                exporter_id=self.name,
                                message="grok2api v3 未配置管理员密码",
                            )
                        login = client.post(
                            f"{base_url}/api/admin/v1/auth/login",
                            json={"username": username, "password": password},
                        )
                        login.raise_for_status()
                        auth_token = str(
                            login.json().get("data", {}).get("tokens", {}).get("accessToken", "")
                        )
                    if not auth_token:
                        raise RuntimeError("grok2api v3 登录响应缺少 accessToken")
                    import_document = json.dumps(
                        {
                            "version": 1,
                            "accounts": [
                                {
                                    "name": account_data.get("email") or "grok-web",
                                    "sso_token": token,
                                    "tier": "auto",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                    response = client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {auth_token}"},
                        files={
                            "file": (
                                f"{account_data.get('email') or 'account'}.json",
                                import_document,
                                "application/json",
                            )
                        },
                    )
                    response.raise_for_status()
                    body = response.text
                    if 'event: error' in body or '"error"' in body and 'event: complete' not in body:
                        raise RuntimeError(body[:300])
                    if "event: complete" not in body:
                        raise RuntimeError(f"grok2api v3 未返回导入完成事件: {body[:300]}")

                    console_synced = False
                    sync_message = ""
                    auto_sync = str(
                        config.extra.get("sync_console")
                        if "sync_console" in config.extra
                        else os.getenv("GROK2API_AUTO_SYNC_CONSOLE", "1")
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if auto_sync:
                        account_name = str(account_data.get("email") or "").strip()
                        listed = client.get(
                            f"{base_url}/api/admin/v1/accounts",
                            headers={"Authorization": f"Bearer {auth_token}"},
                            params={"provider": "grok_web", "search": account_name, "pageSize": 20},
                        )
                        listed.raise_for_status()
                        items = listed.json().get("data", {}).get("items", [])
                        web_account = next(
                            (item for item in items if str(item.get("name") or "") == account_name),
                            items[0] if len(items) == 1 else None,
                        )
                        if web_account:
                            synced = client.post(
                                f"{base_url}/api/admin/v1/accounts/web/sync-to-console",
                                headers={"Authorization": f"Bearer {auth_token}"},
                                json={"ids": [str(web_account["id"])], "all": False, "strategy": "missing"},
                            )
                            synced.raise_for_status()
                            sync_body = synced.text
                            console_synced = "event: complete" in sync_body and "event: error" not in sync_body
                            if not console_synced:
                                raise RuntimeError(f"Console 自动同步未完成: {sync_body[:300]}")
                            sync_message = "，Console 已同步"
                        else:
                            raise RuntimeError("导入后未定位到对应 Web 账户")
                return PushResult(
                    success=True,
                    exporter_id=self.name,
                    message=f"推送到 grok2api v3 成功{sync_message} (HTTP {response.status_code})",
                    data={
                        "status_code": response.status_code,
                        "api_version": 3,
                        "console_synced": console_synced,
                    },
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                return PushResult(
                    success=False,
                    exporter_id=self.name,
                    message=f"grok2api v3 推送失败: {exc}",
                )

        # grok2api 2.x 兼容路径。
        headers = {"Content-Type": "application/json"}
        auth_token = config.extra.get("auth_token", "")
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        payload = {
            "tokens": [token],
            "pool": str(config.extra.get("pool", "auto") or "auto"),
            "tags": ["grok-register"],
        }

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()

                return PushResult(
                    success=True,
                    exporter_id=self.name,
                    message=f"推送到 grok2api 成功 (HTTP {resp.status_code})",
                    data={"status_code": resp.status_code},
                )

        except httpx.HTTPStatusError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"grok2api 推送失败: HTTP {exc.response.status_code} - {exc.response.text[:200]}",
            )
        except httpx.HTTPError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"grok2api 网络请求失败: {exc}",
            )

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        """校验配置。"""
        endpoint = config.endpoint or config.extra.get("endpoint", "")
        if not endpoint:
            return "必须配置 grok2api endpoint"
        if not endpoint.startswith(("http://", "https://")):
            return "endpoint 必须以 http:// 或 https:// 开头"
        return None

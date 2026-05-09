# grok2api 专用 Exporter（默认禁用）
# 将账号 token 推送到 grok2api 服务的 /api/tokens 端点

from __future__ import annotations

import logging
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
    description = "推送 token 到 grok2api 服务的 /api/tokens 端点"
    config_schema = {
        "endpoint": {
            "type": "string",
            "title": "grok2api 端点",
            "description": "grok2api 服务地址（如 http://localhost:3000/api/tokens）",
            "required": True,
        },
        "auth_token": {
            "type": "string",
            "title": "认证 Token",
            "description": "grok2api 管理接口的认证 token（可选）",
            "default": "",
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
            account_data.get("token", "")
            or account_data.get("cookie", "")
            or account_data.get("sso_token", "")
        )

        if not token:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message="账号数据中未找到有效 token",
            )

        # 构建请求
        headers = {"Content-Type": "application/json"}
        auth_token = config.extra.get("auth_token", "")
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        payload = {
            "token": token,
            "email": account_data.get("email", ""),
            "status": "active",
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

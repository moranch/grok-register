# Kiro Account Manager 专用 Exporter（默认禁用）
# 将账号数据推送到 kiro-account-manager 的 /api/accounts 端点

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class KiroAccountManagerExporter(BaseExporter):
    """
    Kiro Account Manager 专用 Exporter。

    将注册成功的账号数据推送到 kiro-account-manager 服务，
    用于统一账号管理和调度。

    默认禁用，需要在 settings 中手动启用并配置 endpoint。
    """

    name = "kiro_account_manager"
    display_name = "Kiro Account Manager"
    description = "推送账号到 kiro-account-manager 的 /api/accounts 端点"
    config_schema = {
        "endpoint": {
            "type": "string",
            "title": "Account Manager 端点",
            "description": "kiro-account-manager 服务地址（如 http://localhost:8080/api/accounts）",
            "required": True,
        },
        "api_key": {
            "type": "string",
            "title": "API Key",
            "description": "kiro-account-manager 的 API 认证密钥",
            "default": "",
        },
        "platform": {
            "type": "string",
            "title": "平台标识",
            "description": "账号所属平台（如 grok / cursor）",
            "default": "grok",
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        POST 账号数据到 kiro-account-manager 的 /api/accounts 端点。

        Args:
            account_data: 账号字典。
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
                message="未配置 kiro-account-manager endpoint",
            )

        # 构建请求头
        headers = {"Content-Type": "application/json"}
        api_key = config.extra.get("api_key", "")
        if api_key:
            headers["X-API-Key"] = api_key

        # 构建请求体
        platform = config.extra.get("platform", "grok")
        payload = {
            "platform": platform,
            "email": account_data.get("email", ""),
            "password": account_data.get("password", ""),
            "token": account_data.get("token", "") or account_data.get("cookie", ""),
            "status": "active",
            "extra": account_data.get("extra", {}),
        }

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()

                return PushResult(
                    success=True,
                    exporter_id=self.name,
                    message=f"推送到 Kiro Account Manager 成功 (HTTP {resp.status_code})",
                    data={"status_code": resp.status_code},
                )

        except httpx.HTTPStatusError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"Account Manager 推送失败: HTTP {exc.response.status_code} - {exc.response.text[:200]}",
            )
        except httpx.HTTPError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"Account Manager 网络请求失败: {exc}",
            )

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        """校验配置。"""
        endpoint = config.endpoint or config.extra.get("endpoint", "")
        if not endpoint:
            return "必须配置 kiro-account-manager endpoint"
        if not endpoint.startswith(("http://", "https://")):
            return "endpoint 必须以 http:// 或 https:// 开头"
        return None

# SSO Token 文件导出器
# 仅导出 SSO token 纯文本

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class SsoFileExporter(BaseExporter):
    """
    SSO Token 纯文本导出器。

    不做网络请求，仅返回 SSO token 字符串，供前端下载或复制使用。
    """

    name = "sso"
    display_name = "SSO Token (纯文本)"
    description = "仅导出 SSO token 纯文本字符串"
    config_schema = {
        "token_field": {
            "type": "string",
            "title": "Token 字段名",
            "description": "从 account_data 中提取 token 的字段名",
            "default": "token",
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        返回 SSO token 字符串。

        不做网络请求，仅提取并返回 token 纯文本。

        Args:
            account_data: 账号字典。
            config: Exporter 配置。

        Returns:
            PushResult，data 字段包含 token 纯文本。
        """
        token_field = config.extra.get("token_field", "token")
        token = account_data.get(token_field, "")

        # 也尝试从 extra 中获取
        if not token and "extra" in account_data:
            token = account_data["extra"].get(token_field, "")

        # 尝试从 cookie / sso_token 字段获取
        if not token:
            token = (
                account_data.get("sso_token", "")
                or account_data.get("cookie", "")
                or account_data.get("auth_token", "")
            )

        if not token:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message="未找到有效的 SSO token",
            )

        return PushResult(
            success=True,
            exporter_id=self.name,
            message="SSO token 导出成功",
            data={
                "content": str(token),
                "content_type": "text/plain",
                "filename": f"{account_data.get('email', 'account')}_sso.txt",
            },
        )

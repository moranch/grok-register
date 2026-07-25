"""Sub2API account exporter backed by the independent import client."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.cpa_auth import token_to_cpa_record
from core.registry import register_exporter
from core.sub2api_client import import_record, import_sso


@register_exporter
class Sub2ApiExporter(BaseExporter):
    name = "sub2api"
    display_name = "Sub2API"
    description = "将注册凭据独立导入 Sub2API，支持分组、SSO fallback 和失败重试队列"
    config_schema = {
        "base_url": {"type": "string", "title": "Sub2API 地址", "default": ""},
        "auth_mode": {
            "type": "string",
            "title": "认证方式",
            "default": "api_key",
            "enum": ["api_key", "password"],
        },
        "api_key": {"type": "string", "title": "管理员 API Key", "default": ""},
        "admin_email": {"type": "string", "title": "管理员邮箱", "default": ""},
        "admin_password": {"type": "string", "title": "管理员密码", "default": ""},
        "group_id": {"type": "integer", "title": "默认分组 ID", "default": 0},
        "auto_import": {"type": "boolean", "title": "注册后自动导入", "default": False},
        "sso_fallback": {"type": "boolean", "title": "OAuth 缺失时使用 SSO 导入", "default": True},
        "retries": {"type": "integer", "title": "失败重试次数", "default": 2},
        "workers": {"type": "integer", "title": "导入并发数", "default": 2},
        "timeout": {"type": "integer", "title": "请求超时秒数", "default": 45},
        "verify_tls": {"type": "boolean", "title": "验证 TLS", "default": True},
    }

    @staticmethod
    def _client_config(config: ExporterConfig) -> dict[str, Any]:
        extra = dict(config.extra or {})
        extra["base_url"] = str(config.endpoint or extra.get("base_url") or "").strip()
        for key in ("group_id", "auto_import", "sso_fallback", "retries", "workers"):
            extra.pop(key, None)
        return extra

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        error = self.validate_config(config)
        if error:
            return PushResult(False, self.name, error)
        extra = account_data.get("extra") if isinstance(account_data.get("extra"), dict) else {}
        settings = config.extra or {}
        group_id = int(settings.get("group_id") or 0)
        client_config = self._client_config(config)
        if extra.get("access_token") and extra.get("refresh_token"):
            token = {
                key: extra.get(key)
                for key in (
                    "access_token",
                    "refresh_token",
                    "id_token",
                    "token_type",
                    "expires_in",
                    "expired",
                    "scope",
                )
                if extra.get(key) not in (None, "")
            }
            record = token_to_cpa_record(
                token,
                str(account_data.get("email") or ""),
                base_url=str(settings.get("oauth_base_url") or "https://cli-chat-proxy.grok.com/v1"),
            )
            result = import_record(record, group_id=group_id, **client_config)
        elif bool(settings.get("sso_fallback", True)) and account_data.get("sso"):
            result = import_sso(
                sso_token=str(account_data.get("sso") or ""),
                email=str(account_data.get("email") or ""),
                group_id=group_id,
                **client_config,
            )
        else:
            return PushResult(False, self.name, "账号缺少 OAuth token/SSO")
        return PushResult(
            bool(result.get("ok")),
            self.name,
            "Sub2API 导入成功" if result.get("ok") else str(result.get("error") or "Sub2API 导入失败"),
            result,
        )

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        extra = config.extra or {}
        base_url = str(config.endpoint or extra.get("base_url") or "").strip()
        if not base_url.startswith(("http://", "https://")):
            return "Sub2API 地址必须以 http:// 或 https:// 开头"
        mode = str(extra.get("auth_mode") or "api_key")
        if mode == "api_key" and not str(extra.get("api_key") or "").strip():
            return "Sub2API 管理员 API Key 未配置"
        if mode == "password" and not (
            str(extra.get("admin_email") or "").strip()
            and str(extra.get("admin_password") or "")
        ):
            return "Sub2API 管理员邮箱或密码未配置"
        return None

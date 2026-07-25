"""CLIProxyAPI exporter for Grok accounts."""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.cpa_auth import (
    exchange_sso_for_token,
    probe_cpa_account,
    token_to_cpa_record,
    upload_cpa_record,
    write_cpa_record,
    write_sub2api_record,
)
from core.registry import register_exporter


@register_exporter
class CpaExporter(BaseExporter):
    name = "cpa"
    display_name = "CLIProxyAPI"
    description = "将 Grok SSO 转换为 OAuth 凭据并写入或上传 CPA"
    config_schema = {
        "endpoint": {
            "type": "string",
            "title": "CPA 地址",
            "description": "远程 CPA 根地址，如 http://cpa:8317；本地写入时可留空",
            "default": "",
        },
        "management_key": {
            "type": "string",
            "title": "Management Key",
            "default": "",
        },
        "auth_dir": {
            "type": "string",
            "title": "本地 auth 目录",
            "description": "容器内路径，如 /workspace/runtime/cpa-auth",
            "default": "",
        },
        "sub_auth_dir": {
            "type": "string",
            "title": "Sub2API Auth 目录",
            "description": "OAuth 成功后同步写入 SUB2API-grok-<email>.json",
            "default": "/workspace/runtime/sub-auth",
        },
        "proxy": {"type": "string", "title": "Device Flow 代理", "default": ""},
        "base_url": {
            "type": "string",
            "title": "Grok Build 地址",
            "default": "https://cli-chat-proxy.grok.com/v1",
        },
        "timeout": {"type": "integer", "title": "超时秒数", "default": 90},
        "identity_timeout": {
            "type": "integer",
            "title": "账号存活验证超时秒数",
            "default": 12,
        },
        "verify_tls": {"type": "boolean", "title": "验证 TLS", "default": True},
        "probe": {"type": "boolean", "title": "导出后验证账号存活", "default": True},
        "probe_required": {"type": "boolean", "title": "账号验证失败视为导出失败", "default": True},
        "auto_mint": {"type": "boolean", "title": "注册成功后自动补全 OAuth", "default": True},
        "prevalidate_enabled": {
            "type": "boolean",
            "title": "后台提前验活",
            "default": True,
        },
        "prevalidate_ttl_minutes": {
            "type": "integer",
            "title": "验活缓存分钟",
            "default": 60,
        },
        "prevalidate_batch_size": {
            "type": "integer",
            "title": "每批验活账号数",
            "default": 10,
        },
        "prevalidate_scan_seconds": {
            "type": "integer",
            "title": "后台扫描秒数",
            "default": 30,
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        error = self.validate_config(config)
        if error:
            return PushResult(False, self.name, error)
        extra = config.extra or {}
        account_extra = account_data.get("extra") or {}
        sso = str(
            account_data.get("sso")
            or account_data.get("token")
            or account_data.get("cookie")
            or account_extra.get("sso")
            or ""
        ).strip()
        if not sso:
            return PushResult(False, self.name, "账号缺少 SSO")

        try:
            timeout = max(30, int(extra.get("timeout", 90)))
            verify_tls = bool(extra.get("verify_tls", True))
            token = exchange_sso_for_token(
                sso,
                sso_rw=str(account_extra.get("sso_rw") or ""),
                proxy=str(extra.get("proxy") or ""),
                timeout=timeout,
                verify_tls=verify_tls,
            )
            record = token_to_cpa_record(
                token,
                str(account_data.get("email") or ""),
                base_url=str(extra.get("base_url") or "https://cli-chat-proxy.grok.com/v1"),
            )
            probe = None
            if bool(extra.get("probe", True)):
                probe = probe_cpa_account(
                    record["access_token"],
                    proxy=str(extra.get("proxy") or ""),
                    timeout=min(max(5, int(extra.get("identity_timeout", 12))), 30),
                    verify_tls=verify_tls,
                )
                if bool(extra.get("probe_required", True)) and not bool(
                    probe.get("account_alive", probe.get("ok"))
                ):
                    return PushResult(False, self.name, "CPA 文件未生成：账号存活探测失败")

            destinations: list[str] = []
            filename = ""
            auth_dir = str(extra.get("auth_dir") or "").strip()
            if auth_dir:
                path = write_cpa_record(auth_dir, record)
                filename = path.name
                destinations.append("local")
            sub_auth_dir = str(extra.get("sub_auth_dir") or "").strip()
            sub_filename = ""
            if sub_auth_dir:
                sub_filename = write_sub2api_record(sub_auth_dir, record).name
                destinations.append("sub2api")
            endpoint = str(config.endpoint or extra.get("endpoint") or "").strip()
            if endpoint:
                filename = upload_cpa_record(
                    endpoint,
                    str(extra.get("management_key") or ""),
                    record,
                    timeout=timeout,
                    verify_tls=verify_tls,
                )
                destinations.append("remote")
            return PushResult(
                True,
                self.name,
                "CPA 导出成功",
                {
                    "filename": filename,
                    "sub_filename": sub_filename,
                    "destinations": destinations,
                    "probe": probe,
                    "oauth": {
                        "sub": record.get("sub", ""),
                        "expired": record.get("expired", ""),
                        "has_refresh_token": bool(record.get("refresh_token")),
                    },
                },
            )
        except Exception as exc:
            return PushResult(False, self.name, f"CPA 导出失败: {exc}")

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        extra = config.extra or {}
        endpoint = str(config.endpoint or extra.get("endpoint") or "").strip()
        auth_dir = str(extra.get("auth_dir") or "").strip()
        if not endpoint and not auth_dir:
            return "必须配置 CPA endpoint 或本地 auth_dir"
        if endpoint and not endpoint.startswith(("http://", "https://")):
            return "CPA endpoint 必须以 http:// 或 https:// 开头"
        if endpoint and not str(extra.get("management_key") or "").strip():
            return "远程 CPA 必须配置 management_key"
        return None

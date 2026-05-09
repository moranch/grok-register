"""
Kiro 平台插件 — 基于上游 any-auto-register vendor 实现。

Requirement 1 AC6, Requirement 14 AC1, Requirement 17 AC1。
"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.kiro.plugin import KiroPlatform as _UpstreamKiro


@register_platform
class KiroPlatform(AARAdapter):
    upstream_cls = _UpstreamKiro

    name = "kiro"
    display_name = "Kiro"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_oauth=True,
        supports_refresh=True,
        supports_trial_info=True,
        supports_switch=True,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="refresh_token",
            display_name="Refresh Token 模式",
            description="产出 Access Token + Refresh Token，支持自动续期",
            is_recommended=True,
        ),
        EngineSpec(
            id="access_token",
            display_name="Access Token 模式",
            description="仅产出 Access Token",
        ),
    ]
    preferred_captcha_strategies = ["token", "browser"]
    supported_exporters = ["any2api", "kiro_account_manager"]
    default_extra_schema = {
        "type": "object",
        "properties": {
            "kiro_account_manager_url": {
                "type": "string",
                "title": "Kiro Account Manager 地址",
                "default": "",
            },
            "oauth_providers": {
                "type": "array",
                "title": "OAuth Providers",
                "items": {
                    "type": "string",
                    "enum": ["google", "github", "microsoft"],
                },
                "default": [],
            },
        },
    }

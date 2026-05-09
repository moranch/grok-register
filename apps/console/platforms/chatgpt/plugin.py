"""
ChatGPT 平台插件 — 基于上游 any-auto-register vendor 实现。
"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.chatgpt.plugin import ChatGPTPlatform as _UpstreamChatGPT


@register_platform
class ChatGPTPlatform(AARAdapter):
    upstream_cls = _UpstreamChatGPT

    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_oauth=True,
        supports_refresh=True,
        supports_trial_info=True,
        supports_switch=False,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="access_token",
            display_name="Access Token",
            description="仅产出 access_token，适合一次性使用",
        ),
        EngineSpec(
            id="refresh_token",
            display_name="Refresh Token",
            description="产出 refresh_token，可自动续期",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token", "browser", "batch"]
    supported_exporters = ["any2api", "cpa", "sub2api"]
    default_extra_schema = {
        "type": "object",
        "properties": {
            "cpa_url": {"type": "string", "title": "CPA 推送地址", "default": ""},
            "sub2api_url": {"type": "string", "title": "Sub2API 推送地址", "default": ""},
        },
    }

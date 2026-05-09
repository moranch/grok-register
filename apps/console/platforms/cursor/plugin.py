"""
Cursor 平台插件 — 基于上游 any-auto-register vendor 实现。
"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.cursor.plugin import CursorPlatform as _UpstreamCursor


@register_platform
class CursorPlatform(AARAdapter):
    upstream_cls = _UpstreamCursor

    name = "cursor"
    display_name = "Cursor"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_oauth=True,
        supports_refresh=True,
        supports_switch=True,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="上游 Cursor 注册流程",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token", "browser"]
    supported_exporters = ["any2api"]

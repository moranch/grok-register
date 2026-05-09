"""
OpenBlockLabs 平台插件 — 基于上游 any-auto-register vendor 实现。
"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.openblocklabs.plugin import (
    OpenBlockLabsPlatform as _UpstreamOBL,
)


@register_platform
class OpenBlockLabsPlatform(AARAdapter):
    upstream_cls = _UpstreamOBL

    name = "openblocklabs"
    display_name = "OpenBlockLabs"
    version = "1.0.0"
    supported_executors = ["protocol"]
    capabilities = Capabilities(
        supports_refresh=False,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="上游 OpenBlockLabs 注册流程",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token"]
    supported_exporters = ["any2api"]

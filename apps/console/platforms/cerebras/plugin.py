"""Cerebras 平台 — 基于 vendor any-auto-register。"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.cerebras.plugin import CerebrasPlatform as _Upstream


@register_platform
class CerebrasPlatform(AARAdapter):
    upstream_cls = _Upstream

    name = "cerebras"
    display_name = "Cerebras"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_validity_check=True,
        supports_api_push=True,
    )
    register_engines = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="上游 Cerebras 注册流程",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token", "browser"]
    supported_exporters = ["any2api"]

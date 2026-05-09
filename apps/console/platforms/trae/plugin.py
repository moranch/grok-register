"""Trae.ai 平台 — 基于 vendor any-auto-register。"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.trae.plugin import TraePlatform as _Upstream


@register_platform
class TraePlatform(AARAdapter):
    upstream_cls = _Upstream

    name = "trae"
    display_name = "Trae.ai"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_refresh=True,
        supports_trial_info=True,
        supports_switch=True,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="上游 Trae 注册流程（含 Pro 升级链接生成）",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token", "browser"]
    supported_exporters = ["any2api"]

"""
Anything 通用平台 — 基于上游 any-auto-register vendor 实现。

上游 Anything 是一个"泛型适配器"平台：用户在配置中声明目标站点的注册 URL/
表单字段/OTP 提取规则，系统用同一套流程完成注册。适合那些协议简单、
仅需邮箱 OTP 的小平台。
"""
from __future__ import annotations

from core.base_platform import Capabilities, EngineSpec
from core.registry import register_platform
from platforms._shared.aar_adapter import AARAdapter
from platforms._vendor_aar.anything.plugin import AnythingPlatform as _Upstream


@register_platform
class AnythingPlatform(AARAdapter):
    upstream_cls = _Upstream

    name = "anything"
    display_name = "Anything (通用)"
    version = "1.0.0"
    supported_executors = ["protocol"]
    capabilities = Capabilities(
        supports_validity_check=True,
        supports_api_push=True,
    )
    register_engines = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="上游通用协议模式注册",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies = ["token"]
    supported_exporters = ["any2api"]
    default_extra_schema = {
        "type": "object",
        "properties": {
            "target_url": {"type": "string", "title": "目标站点注册 URL"},
            "form_schema": {"type": "object", "title": "表单字段映射"},
        },
    }

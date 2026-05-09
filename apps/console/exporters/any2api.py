# Any2API 通用 POST/PUT 模板 Exporter
# 支持自定义 endpoint、HTTP 方法和 Jinja2 模板

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class Any2APIExporter(BaseExporter):
    """
    通用 POST/PUT 模板 Exporter。

    支持：
    - 自定义 endpoint URL
    - POST（追加）或 PUT（覆盖）模式
    - Jinja2 模板渲染请求体
    - 自定义 Headers

    config_schema:
    - endpoint: API 端点 URL
    - api_append: True=POST 追加, False=PUT 覆盖
    - template: Jinja2 模板字符串（渲染请求体）
    - headers: 自定义请求头字典
    """

    name = "any2api"
    display_name = "Any2API (通用)"
    description = "通用 REST API 推送，支持 POST/PUT 和 Jinja2 模板"
    config_schema = {
        "endpoint": {
            "type": "string",
            "title": "API 端点",
            "description": "目标 API 的完整 URL",
            "required": True,
        },
        "api_append": {
            "type": "boolean",
            "title": "追加模式",
            "description": "True=POST 追加, False=PUT 覆盖",
            "default": True,
        },
        "template": {
            "type": "string",
            "title": "请求体模板",
            "description": "Jinja2 模板，可用变量: email, password, token, cookie 等",
            "default": "",
        },
        "headers": {
            "type": "object",
            "title": "自定义请求头",
            "description": "额外的 HTTP 请求头",
            "default": {},
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        推送账号数据到指定 API 端点。

        根据 config.api_append 决定使用 POST 或 PUT 方法。
        如果配置了 template，使用 Jinja2 渲染请求体；否则直接发送 account_data。

        Args:
            account_data: 账号字典（含 email / password / token / extra 等）。
            config: Exporter 配置。

        Returns:
            PushResult。
        """
        endpoint = config.endpoint
        if not endpoint:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message="未配置 endpoint",
            )

        # 构建请求体
        body = self._render_body(account_data, config)

        # 构建请求头
        headers = {"Content-Type": "application/json"}
        extra_headers = config.extra.get("headers", {})
        if isinstance(extra_headers, dict):
            headers.update(extra_headers)

        # 选择 HTTP 方法
        method = "POST" if config.api_append else "PUT"

        try:
            with httpx.Client(timeout=30) as client:
                if method == "POST":
                    resp = client.post(endpoint, json=body, headers=headers)
                else:
                    resp = client.put(endpoint, json=body, headers=headers)

                resp.raise_for_status()

                return PushResult(
                    success=True,
                    exporter_id=self.name,
                    message=f"{method} 成功 (HTTP {resp.status_code})",
                    data={"status_code": resp.status_code, "response": resp.text[:500]},
                )

        except httpx.HTTPStatusError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"{method} 失败: HTTP {exc.response.status_code} - {exc.response.text[:200]}",
            )
        except httpx.HTTPError as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"网络请求失败: {exc}",
            )

    def _render_body(self, account_data: Dict[str, Any], config: ExporterConfig) -> Any:
        """
        使用 Jinja2 模板渲染请求体。

        如果未配置模板，直接返回 account_data。
        """
        template_str = config.template
        if not template_str:
            return account_data

        try:
            from jinja2 import Template
            template = Template(template_str)
            rendered = template.render(**account_data)
            # 尝试解析为 JSON
            return json.loads(rendered)
        except ImportError:
            logger.warning("[Any2API] Jinja2 未安装，直接发送原始数据")
            return account_data
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("[Any2API] 模板渲染失败: %s，使用原始数据", exc)
            return account_data

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        """校验配置是否完整。"""
        if not config.endpoint:
            return "必须配置 endpoint（API 端点 URL）"
        if not config.endpoint.startswith(("http://", "https://")):
            return "endpoint 必须以 http:// 或 https:// 开头"
        return None

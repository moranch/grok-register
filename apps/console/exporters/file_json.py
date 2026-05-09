# JSON 文件导出器
# 将账号数据格式化为 JSON，供前端下载

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class JsonFileExporter(BaseExporter):
    """
    JSON 文件导出器。

    不做网络请求，仅返回格式化后的 JSON 数据，供前端下载使用。
    """

    name = "json"
    display_name = "JSON 文件"
    description = "将账号数据导出为 JSON 格式文件"
    config_schema = {
        "indent": {
            "type": "integer",
            "title": "缩进空格数",
            "description": "JSON 格式化缩进（默认 2）",
            "default": 2,
        },
        "fields": {
            "type": "array",
            "title": "导出字段",
            "description": "指定导出的字段列表（为空则导出全部）",
            "default": [],
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        将账号数据格式化为 JSON。

        不做网络请求，仅返回格式化后的 JSON 数据。

        Args:
            account_data: 账号字典。
            config: Exporter 配置。

        Returns:
            PushResult，data 字段包含格式化后的 JSON 字符串。
        """
        indent = config.extra.get("indent", 2)
        fields = config.extra.get("fields", [])

        # 如果指定了字段列表，只导出指定字段
        if fields:
            filtered_data = {k: v for k, v in account_data.items() if k in fields}
        else:
            filtered_data = account_data

        try:
            formatted_json = json.dumps(
                filtered_data,
                ensure_ascii=False,
                indent=indent,
            )

            return PushResult(
                success=True,
                exporter_id=self.name,
                message="JSON 格式化成功",
                data={
                    "content": formatted_json,
                    "content_type": "application/json",
                    "filename": f"{account_data.get('email', 'account')}.json",
                },
            )

        except (TypeError, ValueError) as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"JSON 序列化失败: {exc}",
            )

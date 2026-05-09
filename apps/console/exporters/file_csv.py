# CSV 文件导出器
# 将账号数据格式化为 CSV 行，供前端下载

from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List, Optional

from core.base_exporter import BaseExporter, ExporterConfig, PushResult
from core.registry import register_exporter

logger = logging.getLogger(__name__)


@register_exporter
class CsvFileExporter(BaseExporter):
    """
    CSV 文件导出器。

    不做网络请求，仅返回格式化后的 CSV 数据，供前端下载使用。
    """

    name = "csv"
    display_name = "CSV 文件"
    description = "将账号数据导出为 CSV 格式文件"
    config_schema = {
        "fields": {
            "type": "array",
            "title": "导出字段",
            "description": "指定导出的字段列表及顺序（为空则导出全部）",
            "default": ["email", "password", "token"],
        },
        "delimiter": {
            "type": "string",
            "title": "分隔符",
            "description": "CSV 分隔符（默认逗号）",
            "default": ",",
        },
        "include_header": {
            "type": "boolean",
            "title": "包含表头",
            "description": "是否在首行输出字段名",
            "default": True,
        },
    }

    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        将账号数据格式化为 CSV 行。

        不做网络请求，仅返回格式化后的 CSV 数据。

        Args:
            account_data: 账号字典。
            config: Exporter 配置。

        Returns:
            PushResult，data 字段包含格式化后的 CSV 字符串。
        """
        fields: List[str] = config.extra.get("fields", []) or list(account_data.keys())
        delimiter: str = config.extra.get("delimiter", ",")
        include_header: bool = config.extra.get("include_header", True)

        try:
            output = io.StringIO()
            writer = csv.writer(output, delimiter=delimiter)

            # 写入表头
            if include_header:
                writer.writerow(fields)

            # 写入数据行
            row = [str(account_data.get(field, "")) for field in fields]
            writer.writerow(row)

            csv_content = output.getvalue()

            return PushResult(
                success=True,
                exporter_id=self.name,
                message="CSV 格式化成功",
                data={
                    "content": csv_content,
                    "content_type": "text/csv",
                    "filename": f"{account_data.get('email', 'account')}.csv",
                },
            )

        except Exception as exc:
            return PushResult(
                success=False,
                exporter_id=self.name,
                message=f"CSV 格式化失败: {exc}",
            )

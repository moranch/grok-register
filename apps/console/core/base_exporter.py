"""
账号导出器（Exporter）插件体系。

对应 Requirement 10 AC1-AC8。

Exporter 把 Account_Asset 推送到外部系统或转成文件。
- 内置 any2api（通用 POST/PUT 模板）
- 可选 cpa / sub2api / grok2api / kiro_account_manager
- 文件格式 json / csv / sso 视为三个"本地 Exporter"
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.registry import EXPORTER_REGISTRY

logger = logging.getLogger(__name__)


# ─── 数据类 ──────────────────────────────────────────────────────────────────


@dataclass
class PushResult:
    """Exporter push 结果。"""
    success: bool = False
    exporter_id: str = ""
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExporterConfig:
    """Exporter 运行时配置（从 settings 表加载）。"""
    exporter_id: str = ""
    enabled: bool = False
    endpoint: str = ""
    api_append: bool = True  # True=POST 追加, False=PUT 覆盖
    template: str = ""  # Jinja2 模板（用于 any2api）
    extra: Dict[str, Any] = field(default_factory=dict)


# ─── BaseExporter 抽象类 ─────────────────────────────────────────────────────


class BaseExporter(ABC):
    """
    账号导出器抽象基类。

    子类必须声明：
    - name: str（exporter 标识）
    - display_name: str

    子类必须实现：
    - push(account_data, config) -> PushResult
    """

    name: str = ""
    display_name: str = ""
    description: str = ""
    config_schema: Dict[str, Any] = {}  # JSON Schema 子集，供前端渲染配置表单

    @abstractmethod
    def push(self, account_data: Dict[str, Any], config: ExporterConfig) -> PushResult:
        """
        推送账号到外部系统。

        Args:
            account_data: 账号字典（含 email / password / token / extra 等）。
            config: Exporter 配置。

        Returns:
            PushResult。
        """
        ...

    def validate_config(self, config: ExporterConfig) -> Optional[str]:
        """
        校验配置是否完整。

        Returns:
            错误信息字符串，None 表示配置有效。
        """
        return None

    def to_dict(self) -> Dict[str, Any]:
        """序列化 Exporter 元数据。"""
        return {
            "id": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "config_schema": self.config_schema,
        }


# ─── ExporterDispatcher ──────────────────────────────────────────────────────


class ExporterDispatcher:
    """
    Exporter 调度器：按 supported_exporters 白名单过滤并执行 push。

    对应 Requirement 10 AC3/AC5/AC6。
    """

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None):
        self._log_fn = log_fn or logger.info

    def log(self, message: str):
        self._log_fn(message)

    async def push_all(
        self,
        account_data: Dict[str, Any],
        supported_exporters: List[str],
        exporter_configs: Dict[str, ExporterConfig],
    ) -> Dict[str, PushResult]:
        """
        对所有启用的 Exporter 执行 push。

        - 按 supported_exporters 白名单过滤（Req 10 AC3）。
        - 异常不阻断主流程（Req 10 AC6）。
        - 返回 {exporter_id: PushResult} 字典。

        Args:
            account_data: 账号字典。
            supported_exporters: 平台声明的白名单。
            exporter_configs: 各 Exporter 的配置（从 settings 表加载）。
        """
        results: Dict[str, PushResult] = {}

        for exporter_id in supported_exporters:
            config = exporter_configs.get(exporter_id)
            if config is None or not config.enabled:
                continue

            exporter_cls = EXPORTER_REGISTRY.get(exporter_id)
            if exporter_cls is None:
                self.log(f"[Exporter] '{exporter_id}' 未注册，跳过")
                continue

            exporter = exporter_cls()

            try:
                result = exporter.push(account_data, config)
                results[exporter_id] = result
                if result.success:
                    self.log(f"[Exporter] {exporter_id}: 推送成功")
                else:
                    self.log(f"[Exporter] {exporter_id}: 推送失败 - {result.message}")
            except Exception as exc:
                # Req 10 AC6：不阻断主流程
                self.log(f"[Exporter] {exporter_id}: 异常 - {exc}")
                results[exporter_id] = PushResult(
                    success=False,
                    exporter_id=exporter_id,
                    message=str(exc),
                )

        return results

    def push_all_sync(
        self,
        account_data: Dict[str, Any],
        supported_exporters: List[str],
        exporter_configs: Dict[str, ExporterConfig],
    ) -> Dict[str, PushResult]:
        """同步版本的 push_all（用于非 async 上下文）。"""
        results: Dict[str, PushResult] = {}

        for exporter_id in supported_exporters:
            config = exporter_configs.get(exporter_id)
            if config is None or not config.enabled:
                continue

            exporter_cls = EXPORTER_REGISTRY.get(exporter_id)
            if exporter_cls is None:
                self.log(f"[Exporter] '{exporter_id}' 未注册，跳过")
                continue

            exporter = exporter_cls()

            try:
                result = exporter.push(account_data, config)
                results[exporter_id] = result
                if result.success:
                    self.log(f"[Exporter] {exporter_id}: 推送成功")
                else:
                    self.log(f"[Exporter] {exporter_id}: 推送失败 - {result.message}")
            except Exception as exc:
                self.log(f"[Exporter] {exporter_id}: 异常 - {exc}")
                results[exporter_id] = PushResult(
                    success=False,
                    exporter_id=exporter_id,
                    message=str(exc),
                )

        return results


# ─── 工厂函数 ────────────────────────────────────────────────────────────────


def get_exporter(exporter_id: str) -> Optional[BaseExporter]:
    """获取已注册的 Exporter 实例。"""
    cls = EXPORTER_REGISTRY.get(exporter_id)
    if cls is None:
        return None
    return cls()


def list_exporters() -> List[Dict[str, Any]]:
    """返回所有已注册 Exporter 的元数据列表。"""
    results = []
    for cls in EXPORTER_REGISTRY.list_all():
        instance = cls()
        results.append(instance.to_dict())
    return results

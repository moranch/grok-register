"""
RegistrationContext：贯穿注册全过程的上下文对象。

对应 Requirement 1 AC5, Requirement 17 AC4。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.base_platform import BasePlatform, EngineSpec
    from core.base_mailbox import BaseMailbox
    from core.task_runtime import EventBus, TaskControl


@dataclass
class RegistrationContext:
    """
    注册上下文：在一次注册轮中传递给 Flow / Adapter / Worker 的所有信息。

    字段说明：
    - task_id: 所属任务 ID。
    - round_no: 当前轮次（从 1 开始）。
    - platform: 平台插件实例。
    - engine: 当前使用的注册引擎。
    - executor_type: 执行器类型（protocol / headless / headed）。
    - proxy: 当前轮使用的代理 URL。
    - mailbox: 邮箱 Provider 实例（已创建好的）。
    - email: 用户指定的邮箱（可选，为空时由 mailbox 自动生成）。
    - password: 密码（已解析好的）。
    - extra: 平台专属配置 + 全局配置合并后的字典。
    - event_bus: SSE 事件总线。
    - control: 任务控制对象（stop / skip / 熔断）。
    - log_fn: 日志函数。
    - settings: 全局 settings 快照。
    """

    task_id: str = ""
    round_no: int = 1
    platform: Optional[Any] = None  # BasePlatform 实例
    engine: Optional[Any] = None  # EngineSpec 实例
    executor_type: str = "protocol"
    proxy: Optional[str] = None
    mailbox: Optional[Any] = None  # BaseMailbox 实例
    email: Optional[str] = None
    password: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    event_bus: Optional[Any] = None  # EventBus 实例
    control: Optional[Any] = None  # TaskControl 实例
    log_fn: Callable[[str], None] = field(default_factory=lambda: print)
    settings: Dict[str, Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        """输出日志。"""
        self.log_fn(message)

    def emit(self, event_type: str, **data) -> None:
        """向 EventBus 发射事件。"""
        if self.event_bus:
            self.event_bus.emit(event_type, **data)

    def checkpoint(self) -> None:
        """调用 TaskControl 的检查点。"""
        if self.control:
            self.control.checkpoint()

    @property
    def engine_id(self) -> str:
        """当前引擎 ID。"""
        if self.engine:
            return getattr(self.engine, "id", "default")
        return "default"

    @property
    def platform_name(self) -> str:
        """当前平台名称。"""
        if self.platform:
            return getattr(self.platform, "name", "")
        return ""

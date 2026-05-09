"""
注册 Flow 基类。

所有 Flow（protocol_mailbox / browser / oauth）继承此基类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from core.base_platform import Account
from core.registration.context import RegistrationContext


class BaseRegistrationFlow(ABC):
    """注册流程基类。"""

    def __init__(self, ctx: Optional[RegistrationContext] = None):
        self.ctx = ctx

    def set_context(self, ctx: RegistrationContext) -> None:
        self.ctx = ctx

    @abstractmethod
    def run(self, email: Optional[str] = None, password: str = "") -> Account:
        """
        执行注册流程。

        Args:
            email: 指定邮箱（可选）。
            password: 密码。

        Returns:
            注册成功的 Account 对象。

        Raises:
            各种注册异常。
        """
        ...

"""
邮箱 Provider 抽象基类 + 加权挑选工厂。

对应 Requirement 5 AC1/AC2/AC3/AC4/AC5。
"""
from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.registry import MAILBOX_REGISTRY

logger = logging.getLogger(__name__)


@dataclass
class MailboxAccount:
    """邮箱创建结果。"""
    email: str
    password: str = ""
    account_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class BaseMailbox(ABC):
    """
    邮箱 Provider 抽象基类。

    子类必须声明：
    - name: str（provider_type 标识，如 tmail / moemail / duckmail）
    - display_name: str

    子类必须实现：
    - create() -> MailboxAccount
    - wait_otp(email, timeout) -> str
    """

    name: str = ""
    display_name: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None, proxy: Optional[str] = None):
        self.config = config or {}
        self.proxy = proxy
        self._log_fn: Callable[[str], None] = print

    def set_logger(self, log_fn: Callable[[str], None]):
        self._log_fn = log_fn or print

    def log(self, message: str):
        self._log_fn(message)

    @abstractmethod
    def create(self) -> MailboxAccount:
        """创建一个新的临时邮箱账号。"""
        ...

    @abstractmethod
    def wait_otp(self, email: str, timeout: int = 120) -> str:
        """
        等待并提取 OTP 验证码。

        Args:
            email: 目标邮箱地址。
            timeout: 最大等待秒数。

        Returns:
            提取到的验证码字符串。

        Raises:
            TimeoutError: 超时未收到验证码。
        """
        ...

    def list_domains(self) -> List[str]:
        """返回该 provider 可用的域名列表（可选实现）。"""
        return []

    def test_connectivity(self) -> bool:
        """
        连通性探测（不创建真实邮箱）。

        Returns:
            True 表示连通，False 表示不可用。
        """
        return True


# ─── 加权挑选工厂 ─────────────────────────────────────────────────────────────


@dataclass
class MailboxProviderStats:
    """邮箱 Provider 的运行时统计（从数据库加载）。"""
    id: int
    provider_type: str
    name: str
    enabled: bool = True
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    config: Dict[str, Any] = field(default_factory=dict)


def _weighted_score(stats: MailboxProviderStats) -> float:
    """
    加权公式：(success + 1) / (success + failure + 2)

    这是 Beta 分布的期望值，在无数据时给出 0.5 的先验。
    """
    return (stats.success_count + 1) / (stats.success_count + stats.failure_count + 2)


def select_mailbox_provider(
    providers: List[MailboxProviderStats],
) -> Optional[MailboxProviderStats]:
    """
    从启用的 provider 列表中按加权随机挑选一个。

    - 只考虑 enabled=True 的 provider。
    - 连续失败 ≥5 次的 provider 应在调用前已被禁用（由 DAO 层处理）。
    - 如果没有可用 provider，返回 None。
    """
    enabled = [p for p in providers if p.enabled]
    if not enabled:
        return None

    weights = [_weighted_score(p) for p in enabled]
    total = sum(weights)
    if total <= 0:
        return random.choice(enabled)

    # 加权随机选择
    chosen = random.choices(enabled, weights=weights, k=1)
    return chosen[0]


def create_mailbox(
    provider_type: str,
    config: Optional[Dict[str, Any]] = None,
    proxy: Optional[str] = None,
) -> BaseMailbox:
    """
    根据 provider_type 创建对应的 BaseMailbox 实例。

    Args:
        provider_type: 邮箱 provider 标识（如 tmail / moemail / duckmail）。
        config: provider 配置字典。
        proxy: 代理 URL。

    Returns:
        BaseMailbox 子类实例。

    Raises:
        KeyError: provider_type 未注册。
    """
    cls = MAILBOX_REGISTRY.get_or_raise(provider_type)
    return cls(config=config, proxy=proxy)


def should_disable_provider(stats: MailboxProviderStats, threshold: int = 5) -> bool:
    """
    判断是否应该禁用该 provider（连续失败达到阈值）。

    对应 Requirement 5 AC4。
    """
    return stats.consecutive_failures >= threshold

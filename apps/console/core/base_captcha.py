"""
验证码 Strategy × Provider 正交模型。

对应 Requirement 6 AC1-AC6。

两个维度：
- Strategy（执行路径）：browser / token / batch / probe
- Provider（服务商）：none / yescaptcha / 2captcha / local / capsolver

子类实现 Strategy 或 Provider 后通过 @register_strategy / @register_captcha 注册。
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.registry import CAPTCHA_REGISTRY, STRATEGY_REGISTRY

logger = logging.getLogger(__name__)


# ─── 数据类 ──────────────────────────────────────────────────────────────────


@dataclass
class CaptchaResult:
    """验证码求解结果。"""
    token: str = ""
    success: bool = False
    strategy: str = ""
    provider: str = ""
    elapsed_ms: int = 0
    error: str = ""


@dataclass
class CaptchaTask:
    """验证码求解任务参数。"""
    page_url: str = ""
    site_key: str = ""
    captcha_type: str = "turnstile"  # turnstile / hcaptcha / recaptcha
    extra: Dict[str, Any] = field(default_factory=dict)


# ─── BaseCaptchaStrategy 抽象类 ───────────────────────────────────────────────


class BaseCaptchaStrategy(ABC):
    """
    验证码求解策略抽象基类。

    四种策略：
    - browser：真实浏览器页面内解（点击 checkbox / 等待自动通过）
    - token：直接调 solver API 取 token（最常用）
    - batch：批量预解 + 池化复用（高并发场景）
    - probe：仅探测是否触发风控，不解算（用于跳过不需要验证码的场景）
    """

    name: str = ""
    display_name: str = ""

    def __init__(self, provider: "BaseCaptchaProvider", config: Optional[Dict[str, Any]] = None):
        self.provider = provider
        self.config = config or {}
        self._log_fn: Callable[[str], None] = print

    def set_logger(self, log_fn: Callable[[str], None]):
        self._log_fn = log_fn or print

    def log(self, message: str):
        self._log_fn(message)

    @abstractmethod
    def solve(self, task: CaptchaTask, timeout: int = 30) -> CaptchaResult:
        """
        执行验证码求解。

        Args:
            task: 验证码任务参数。
            timeout: 超时秒数（默认 30s）。

        Returns:
            CaptchaResult。
        """
        ...

    def supports_provider(self, provider_name: str) -> bool:
        """判断该策略是否支持指定 provider。子类可覆盖。"""
        return True


# ─── BaseCaptchaProvider 抽象类 ───────────────────────────────────────────────


class BaseCaptchaProvider(ABC):
    """
    验证码服务商抽象基类。

    五种 provider：none / yescaptcha / 2captcha / local / capsolver
    """

    name: str = ""
    display_name: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.api_key: str = config.get("captcha_api_key", "") if config else ""

    @abstractmethod
    def create_task(self, task: CaptchaTask) -> str:
        """创建验证码任务，返回 task_id。"""
        ...

    @abstractmethod
    def get_result(self, task_id: str, timeout: int = 30) -> CaptchaResult:
        """轮询获取验证码结果。"""
        ...

    def check_balance(self) -> Optional[float]:
        """
        余额探测（Requirement 6 AC3）。

        Returns:
            余额数值，None 表示不支持余额查询。
        """
        return None

    def is_configured(self) -> bool:
        """判断该 provider 是否已正确配置。"""
        if self.name == "none":
            return True
        return bool(self.api_key)


# ─── NoneProvider（不解算）────────────────────────────────────────────────────


class NoneCaptchaProvider(BaseCaptchaProvider):
    """不使用验证码的占位 provider。"""
    name = "none"
    display_name = "None (跳过)"

    def create_task(self, task: CaptchaTask) -> str:
        return ""

    def get_result(self, task_id: str, timeout: int = 30) -> CaptchaResult:
        return CaptchaResult(success=True, token="", provider="none")

    def is_configured(self) -> bool:
        return True


# ─── solve_with_fallback ─────────────────────────────────────────────────────


def solve_with_fallback(
    task: CaptchaTask,
    strategies: List[str],
    provider_key: str,
    config: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    max_consecutive_failures: int = 3,
    log_fn: Optional[Callable[[str], None]] = None,
) -> CaptchaResult:
    """
    按 preferred_captcha_strategies 顺序逐一尝试，前者失败 fallback 到后者。

    对应 Requirement 6 AC6。

    Args:
        task: 验证码任务。
        strategies: 策略名称列表（如 ["batch", "browser", "token"]）。
        provider_key: provider 标识。
        config: 全局配置。
        timeout: 单策略超时秒数。
        max_consecutive_failures: 连续失败次数达到此值则标记 captcha_timeout。
        log_fn: 日志函数。

    Returns:
        CaptchaResult（最终结果）。
    """
    _log = log_fn or logger.info
    consecutive_failures = 0
    last_error = ""

    for strategy_name in strategies:
        strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
        if strategy_cls is None:
            _log(f"[Captcha] 策略 '{strategy_name}' 未注册，跳过")
            continue

        provider = create_captcha_provider(provider_key, config)
        if not strategy_cls.supports_provider(strategy_cls, provider.name):
            _log(f"[Captcha] 策略 '{strategy_name}' 不支持 provider '{provider.name}'，跳过")
            continue

        strategy = strategy_cls(provider=provider, config=config)
        strategy.set_logger(_log)

        _log(f"[Captcha] 尝试策略={strategy_name}, provider={provider_key}")
        start = time.time()

        try:
            result = strategy.solve(task, timeout=timeout)
            elapsed = int((time.time() - start) * 1000)
            result.elapsed_ms = elapsed
            result.strategy = strategy_name
            result.provider = provider_key

            if result.success and result.token:
                _log(f"[Captcha] 成功: strategy={strategy_name}, {elapsed}ms")
                return result
            else:
                consecutive_failures += 1
                last_error = result.error or "未返回有效 token"
                _log(f"[Captcha] 失败: strategy={strategy_name}, error={last_error}")

        except TimeoutError:
            consecutive_failures += 1
            last_error = "captcha_timeout"
            _log(f"[Captcha] 超时: strategy={strategy_name}, timeout={timeout}s")

        except Exception as exc:
            consecutive_failures += 1
            last_error = str(exc)
            _log(f"[Captcha] 异常: strategy={strategy_name}, error={exc}")

        # 连续失败达到阈值（Req 6 AC5）
        if consecutive_failures >= max_consecutive_failures:
            _log(f"[Captcha] 连续失败 {consecutive_failures} 次，放弃")
            break

    return CaptchaResult(
        success=False,
        error=last_error or "captcha_timeout",
        strategy=strategies[-1] if strategies else "",
        provider=provider_key,
    )


# ─── 工厂函数 ────────────────────────────────────────────────────────────────


def create_captcha_provider(
    provider_key: str,
    config: Optional[Dict[str, Any]] = None,
) -> BaseCaptchaProvider:
    """
    根据 provider_key 创建 Provider 实例。

    Args:
        provider_key: none / yescaptcha / 2captcha / local / capsolver
        config: 全局配置字典。

    Returns:
        BaseCaptchaProvider 子类实例。
    """
    if provider_key == "none" or not provider_key:
        return NoneCaptchaProvider(config=config)

    cls = CAPTCHA_REGISTRY.get(provider_key)
    if cls is None:
        logger.warning("[Captcha] Provider '%s' 未注册，回退到 none", provider_key)
        return NoneCaptchaProvider(config=config)

    return cls(config=config)


def create_captcha_solver(
    strategy: str,
    provider_key: str,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[BaseCaptchaStrategy]:
    """
    创建指定 strategy + provider 的 solver 实例。

    对应 Requirement 6 AC2。

    Returns:
        BaseCaptchaStrategy 实例，或 None（strategy 未注册时）。
    """
    strategy_cls = STRATEGY_REGISTRY.get(strategy)
    if strategy_cls is None:
        return None

    provider = create_captcha_provider(provider_key, config)
    return strategy_cls(provider=provider, config=config)

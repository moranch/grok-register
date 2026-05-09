# 本地 Solver 验证码 Provider
# 复用 turnstilePatch 浏览器扩展，由浏览器自动处理验证码

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.base_captcha import BaseCaptchaProvider, CaptchaResult, CaptchaTask
from core.registry import register_captcha

logger = logging.getLogger(__name__)


@register_captcha
class LocalSolverProvider(BaseCaptchaProvider):
    """
    本地 Solver 验证码 Provider。

    复用 turnstilePatch 浏览器扩展，验证码由浏览器端自动处理。
    不需要外部 API 调用，不消耗余额。

    适用场景：
    - 使用 browser 策略时，浏览器已加载 turnstilePatch 扩展
    - 验证码在页面内自动通过，无需额外求解
    """

    name = "local"
    display_name = "本地 Solver"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)

    def create_task(self, task: CaptchaTask) -> str:
        """
        创建本地求解任务。

        本地 Solver 不需要创建远程任务，返回空 task_id。
        验证码由浏览器扩展自动处理。

        Args:
            task: 验证码任务参数（本地模式下忽略）。

        Returns:
            空字符串（本地不需要 task_id）。
        """
        logger.debug("[LocalSolver] 本地模式，无需创建远程任务")
        return ""

    def get_result(self, task_id: str, timeout: int = 30) -> CaptchaResult:
        """
        获取本地求解结果。

        本地 Solver 直接返回成功，实际验证码由浏览器扩展（turnstilePatch）自动处理。
        token 由浏览器端注入，此处不需要返回具体 token。

        Args:
            task_id: 任务 ID（本地模式下为空）。
            timeout: 超时秒数（本地模式下忽略）。

        Returns:
            CaptchaResult，success=True 表示浏览器扩展将自动处理。
        """
        logger.debug("[LocalSolver] 本地模式，由浏览器扩展自动处理验证码")
        return CaptchaResult(
            success=True,
            token="__LOCAL_SOLVER__",
            provider="local",
        )

    def check_balance(self) -> Optional[float]:
        """
        本地 Solver 无余额概念。

        Returns:
            None（不支持余额查询）。
        """
        return None

    def is_configured(self) -> bool:
        """本地 Solver 始终可用，无需额外配置。"""
        return True

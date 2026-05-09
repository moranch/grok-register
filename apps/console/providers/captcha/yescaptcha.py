# YesCaptcha 验证码 Provider
# 基于 YesCaptcha API 的验证码求解服务

from __future__ import annotations

import time
import logging
from typing import Any, Dict, Optional

import httpx

from core.base_captcha import BaseCaptchaProvider, CaptchaResult, CaptchaTask
from core.registry import register_captcha

logger = logging.getLogger(__name__)


@register_captcha
class YesCaptchaProvider(BaseCaptchaProvider):
    """
    YesCaptcha 验证码服务商 Provider。

    API 文档: https://yescaptcha.com/docs
    - createTask: 创建验证码任务
    - getTaskResult: 获取任务结果
    - getBalance: 查询余额
    """

    name = "yescaptcha"
    display_name = "YesCaptcha"

    BASE_URL = "https://api.yescaptcha.com"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)
        self.base_url = (config or {}).get("yescaptcha_api_url", self.BASE_URL)
        self.poll_interval = (config or {}).get("poll_interval", 3)

    def create_task(self, task: CaptchaTask) -> str:
        """
        创建验证码求解任务。

        Args:
            task: 验证码任务参数。

        Returns:
            task_id 字符串。
        """
        # 根据验证码类型选择对应的 task type
        task_type_map = {
            "turnstile": "TurnstileTaskProxyless",
            "hcaptcha": "HCaptchaTaskProxyless",
            "recaptcha": "RecaptchaV2TaskProxyless",
        }
        task_type = task_type_map.get(task.captcha_type, "TurnstileTaskProxyless")

        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": task_type,
                "websiteURL": task.page_url,
                "websiteKey": task.site_key,
                **task.extra,
            },
        }

        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{self.base_url}/createTask", json=payload)
            resp.raise_for_status()
            data = resp.json()

            if data.get("errorId", 0) != 0:
                raise RuntimeError(
                    f"YesCaptcha createTask 失败: {data.get('errorDescription', '未知错误')}"
                )

            task_id = data.get("taskId", "")
            if not task_id:
                raise RuntimeError("YesCaptcha 未返回 taskId")

            return str(task_id)

    def get_result(self, task_id: str, timeout: int = 30) -> CaptchaResult:
        """
        轮询获取验证码求解结果。

        Args:
            task_id: 任务 ID。
            timeout: 超时秒数。

        Returns:
            CaptchaResult。
        """
        start_time = time.time()

        with httpx.Client(timeout=30) as client:
            while time.time() - start_time < timeout:
                payload = {
                    "clientKey": self.api_key,
                    "taskId": task_id,
                }

                resp = client.post(f"{self.base_url}/getTaskResult", json=payload)
                resp.raise_for_status()
                data = resp.json()

                if data.get("errorId", 0) != 0:
                    return CaptchaResult(
                        success=False,
                        error=data.get("errorDescription", "未知错误"),
                        provider="yescaptcha",
                    )

                status = data.get("status", "")
                if status == "ready":
                    solution = data.get("solution", {})
                    token = solution.get("token", "") or solution.get("gRecaptchaResponse", "")
                    return CaptchaResult(
                        success=True,
                        token=token,
                        provider="yescaptcha",
                    )

                # 任务仍在处理中
                time.sleep(self.poll_interval)

        return CaptchaResult(
            success=False,
            error="captcha_timeout",
            provider="yescaptcha",
        )

    def check_balance(self) -> Optional[float]:
        """
        查询 YesCaptcha 账户余额。

        Returns:
            余额数值（美元），None 表示查询失败。
        """
        if not self.api_key:
            return None

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    f"{self.base_url}/getBalance",
                    params={"clientKey": self.api_key},
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("errorId", 0) != 0:
                    logger.warning(
                        "[YesCaptcha] 余额查询失败: %s",
                        data.get("errorDescription", "未知错误"),
                    )
                    return None

                return float(data.get("balance", 0))

        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("[YesCaptcha] 余额查询异常: %s", exc)
            return None

# 2Captcha 验证码 Provider
# 基于 2Captcha API 的验证码求解服务

from __future__ import annotations

import time
import logging
from typing import Any, Dict, Optional

import httpx

from core.base_captcha import BaseCaptchaProvider, CaptchaResult, CaptchaTask
from core.registry import register_captcha

logger = logging.getLogger(__name__)


@register_captcha
class TwoCaptchaProvider(BaseCaptchaProvider):
    """
    2Captcha 验证码服务商 Provider。

    API 文档: https://2captcha.com/2captcha-api
    - in.php: 提交验证码任务
    - res.php: 获取任务结果 / 查询余额
    """

    name = "2captcha"
    display_name = "2Captcha"

    BASE_URL = "https://2captcha.com"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)
        self.base_url = (config or {}).get("twocaptcha_api_url", self.BASE_URL)
        self.poll_interval = (config or {}).get("poll_interval", 5)

    def create_task(self, task: CaptchaTask) -> str:
        """
        提交验证码求解任务到 2Captcha。

        Args:
            task: 验证码任务参数。

        Returns:
            task_id 字符串。
        """
        # 根据验证码类型构建参数
        params: Dict[str, Any] = {
            "key": self.api_key,
            "json": 1,
        }

        if task.captcha_type == "turnstile":
            params.update({
                "method": "turnstile",
                "sitekey": task.site_key,
                "pageurl": task.page_url,
            })
        elif task.captcha_type == "hcaptcha":
            params.update({
                "method": "hcaptcha",
                "sitekey": task.site_key,
                "pageurl": task.page_url,
            })
        elif task.captcha_type == "recaptcha":
            params.update({
                "method": "userrecaptcha",
                "googlekey": task.site_key,
                "pageurl": task.page_url,
            })
        else:
            params.update({
                "method": "turnstile",
                "sitekey": task.site_key,
                "pageurl": task.page_url,
            })

        # 合并额外参数
        params.update(task.extra)

        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{self.base_url}/in.php", data=params)
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != 1:
                raise RuntimeError(
                    f"2Captcha 提交任务失败: {data.get('request', '未知错误')}"
                )

            task_id = data.get("request", "")
            if not task_id:
                raise RuntimeError("2Captcha 未返回 task_id")

            return task_id

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

        # 2Captcha 建议首次查询前等待几秒
        time.sleep(min(5, timeout))

        with httpx.Client(timeout=30) as client:
            while time.time() - start_time < timeout:
                params = {
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                }

                resp = client.get(f"{self.base_url}/res.php", params=params)
                resp.raise_for_status()
                data = resp.json()

                status = data.get("status", 0)
                request_value = data.get("request", "")

                if status == 1:
                    # 求解成功
                    return CaptchaResult(
                        success=True,
                        token=request_value,
                        provider="2captcha",
                    )

                if request_value == "CAPCHA_NOT_READY":
                    # 仍在处理中
                    time.sleep(self.poll_interval)
                    continue

                # 其他错误
                return CaptchaResult(
                    success=False,
                    error=request_value,
                    provider="2captcha",
                )

        return CaptchaResult(
            success=False,
            error="captcha_timeout",
            provider="2captcha",
        )

    def check_balance(self) -> Optional[float]:
        """
        查询 2Captcha 账户余额。

        Returns:
            余额数值（美元），None 表示查询失败。
        """
        if not self.api_key:
            return None

        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(
                    f"{self.base_url}/res.php",
                    params={
                        "key": self.api_key,
                        "action": "getbalance",
                        "json": 1,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("status") == 1:
                    return float(data.get("request", 0))

                # 尝试直接解析为数字（某些版本直接返回数字）
                request_value = data.get("request", "0")
                try:
                    return float(request_value)
                except (ValueError, TypeError):
                    logger.warning("[2Captcha] 余额查询失败: %s", request_value)
                    return None

        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("[2Captcha] 余额查询异常: %s", exc)
            return None

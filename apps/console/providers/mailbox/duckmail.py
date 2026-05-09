# DuckMail 临时邮箱 Provider
# 公共临时邮箱服务

from __future__ import annotations

import re
import time
import secrets
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.registry import register_mailbox

logger = logging.getLogger(__name__)


@register_mailbox
class DuckMailProvider(BaseMailbox):
    """
    DuckMail 公共临时邮箱 Provider。

    使用公共临时邮箱服务，无需自建。

    需要配置：
    - duckmail_api_url: DuckMail 服务的 API 地址（可选，有默认值）
    """

    name = "duckmail"
    display_name = "DuckMail"

    DEFAULT_API_URL = "https://api.duckmail.dev"

    def __init__(self, config: Optional[Dict[str, Any]] = None, proxy: Optional[str] = None):
        super().__init__(config=config, proxy=proxy)
        self.api_url = (config or {}).get("duckmail_api_url", self.DEFAULT_API_URL)
        self.poll_interval = (config or {}).get("poll_interval", 5)

    def create(self) -> MailboxAccount:
        """
        通过公共 DuckMail 服务创建临时邮箱。

        Raises:
            NotImplementedError: 需要配置 duckmail_api_url。
        """
        if not self.api_url:
            raise NotImplementedError(
                "DuckMail Provider 需要配置 duckmail_api_url。"
                "请在 settings 中设置 duckmail_api_url。"
            )

        self.log("[DuckMail] 正在创建临时邮箱...")

        try:
            with httpx.Client(proxy=self.proxy, timeout=30) as client:
                # 请求创建临时邮箱
                resp = client.post(f"{self.api_url}/api/mailbox/create")
                resp.raise_for_status()
                result = resp.json()

                email = result.get("email", "")
                token = result.get("token", "")

                if not email:
                    raise RuntimeError("DuckMail 未返回有效邮箱地址")

                self.log(f"[DuckMail] 邮箱创建成功: {email}")
                return MailboxAccount(
                    email=email,
                    password="",
                    account_id=result.get("id", ""),
                    extra={
                        "token": token,
                        "api_url": self.api_url,
                    },
                )

        except httpx.HTTPError as exc:
            self.log(f"[DuckMail] API 请求失败: {exc}")
            raise RuntimeError(f"DuckMail API 请求失败: {exc}") from exc

    def wait_otp(self, email: str, timeout: int = 120) -> str:
        """
        轮询 DuckMail API 获取验证码。

        Args:
            email: 目标邮箱地址。
            timeout: 最大等待秒数。

        Returns:
            提取到的 OTP 验证码字符串。

        Raises:
            TimeoutError: 超时未收到验证码。
        """
        if not self.api_url:
            raise NotImplementedError(
                "DuckMail Provider 需要配置 duckmail_api_url 才能获取 OTP。"
            )

        self.log(f"[DuckMail] 等待 OTP: {email}, 超时={timeout}s")
        start_time = time.time()

        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            while time.time() - start_time < timeout:
                try:
                    resp = client.get(
                        f"{self.api_url}/api/mailbox/messages",
                        params={"email": email},
                    )
                    resp.raise_for_status()
                    messages = resp.json()

                    if messages and isinstance(messages, list) and len(messages) > 0:
                        # 取最新邮件
                        latest = messages[0]
                        body = latest.get("body", "") or latest.get("text", "") or latest.get("html", "")

                        # 提取 6 位数字验证码
                        otp_match = re.search(r"\b(\d{6})\b", body)
                        if otp_match:
                            otp = otp_match.group(1)
                            self.log(f"[DuckMail] 获取到 OTP: {otp}")
                            return otp

                except httpx.HTTPError as exc:
                    self.log(f"[DuckMail] 轮询失败: {exc}")

                time.sleep(self.poll_interval)

        raise TimeoutError(f"[DuckMail] 等待 OTP 超时 ({timeout}s): {email}")

    def test_connectivity(self) -> bool:
        """测试 DuckMail 服务连通性。"""
        if not self.api_url:
            return False

        try:
            with httpx.Client(proxy=self.proxy, timeout=10) as client:
                resp = client.get(f"{self.api_url}/api/health")
                return resp.status_code == 200
        except Exception:
            return False

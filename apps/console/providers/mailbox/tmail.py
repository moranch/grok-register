# TMail 临时邮箱 Provider
# 基于 TMail API 创建临时邮箱并获取 OTP 验证码

from __future__ import annotations

import re
import time
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.base_mailbox import BaseMailbox, MailboxAccount
from core.registry import register_mailbox

logger = logging.getLogger(__name__)


@register_mailbox
class TMailProvider(BaseMailbox):
    """
    TMail 临时邮箱 Provider。

    需要配置：
    - tmail_api_url: TMail 服务的 API 地址
    """

    name = "tmail"
    display_name = "TMail"

    def __init__(self, config: Optional[Dict[str, Any]] = None, proxy: Optional[str] = None):
        super().__init__(config=config, proxy=proxy)
        self.api_url = (config or {}).get("tmail_api_url", "")
        self.poll_interval = (config or {}).get("poll_interval", 5)

    def create(self) -> MailboxAccount:
        """
        调用 TMail API 创建临时邮箱。

        Raises:
            NotImplementedError: 需要配置 tmail_api_url 才能使用。
        """
        if not self.api_url:
            raise NotImplementedError(
                "TMail Provider 需要配置 tmail_api_url。"
                "请在 settings 中设置 tmail_api_url 为你的 TMail 服务地址。"
            )

        self.log("[TMail] 正在创建临时邮箱...")

        try:
            with httpx.Client(proxy=self.proxy, timeout=30) as client:
                # 获取可用域名
                resp = client.get(f"{self.api_url}/api/domains")
                resp.raise_for_status()
                domains = resp.json()

                if not domains:
                    raise RuntimeError("TMail 无可用域名")

                domain = domains[0] if isinstance(domains[0], str) else domains[0].get("domain", "")

                # 创建邮箱
                import secrets
                username = f"reg_{secrets.token_hex(6)}"
                email = f"{username}@{domain}"

                resp = client.post(
                    f"{self.api_url}/api/accounts",
                    json={"address": email, "password": secrets.token_hex(8)},
                )
                resp.raise_for_status()
                account_data = resp.json()

                self.log(f"[TMail] 邮箱创建成功: {email}")
                return MailboxAccount(
                    email=email,
                    password=account_data.get("password", ""),
                    account_id=account_data.get("id", ""),
                    extra={"domain": domain, "api_url": self.api_url},
                )

        except httpx.HTTPError as exc:
            self.log(f"[TMail] API 请求失败: {exc}")
            raise RuntimeError(f"TMail API 请求失败: {exc}") from exc

    def wait_otp(self, email: str, timeout: int = 120) -> str:
        """
        轮询 TMail API 获取最新邮件，提取 OTP 验证码。

        Args:
            email: 目标邮箱地址。
            timeout: 最大等待秒数。

        Returns:
            提取到的 OTP 验证码字符串。

        Raises:
            TimeoutError: 超时未收到验证码。
            NotImplementedError: 未配置 tmail_api_url。
        """
        if not self.api_url:
            raise NotImplementedError(
                "TMail Provider 需要配置 tmail_api_url 才能获取 OTP。"
            )

        self.log(f"[TMail] 等待 OTP: {email}, 超时={timeout}s")
        start_time = time.time()

        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            while time.time() - start_time < timeout:
                try:
                    resp = client.get(
                        f"{self.api_url}/api/messages",
                        params={"address": email},
                    )
                    resp.raise_for_status()
                    messages = resp.json()

                    if messages:
                        # 取最新一封邮件
                        latest = messages[0] if isinstance(messages, list) else messages
                        body = latest.get("body", "") or latest.get("text", "")

                        # 提取 6 位数字验证码
                        otp_match = re.search(r"\b(\d{6})\b", body)
                        if otp_match:
                            otp = otp_match.group(1)
                            self.log(f"[TMail] 获取到 OTP: {otp}")
                            return otp

                except httpx.HTTPError as exc:
                    self.log(f"[TMail] 轮询失败: {exc}")

                time.sleep(self.poll_interval)

        raise TimeoutError(f"[TMail] 等待 OTP 超时 ({timeout}s): {email}")

    def list_domains(self) -> List[str]:
        """返回 TMail 可用域名列表。"""
        if not self.api_url:
            return []

        try:
            with httpx.Client(proxy=self.proxy, timeout=10) as client:
                resp = client.get(f"{self.api_url}/api/domains")
                resp.raise_for_status()
                domains = resp.json()
                return [d if isinstance(d, str) else d.get("domain", "") for d in domains]
        except Exception:
            return []

    def test_connectivity(self) -> bool:
        """测试 TMail API 连通性。"""
        if not self.api_url:
            return False

        try:
            with httpx.Client(proxy=self.proxy, timeout=10) as client:
                resp = client.get(f"{self.api_url}/api/domains")
                return resp.status_code == 200
        except Exception:
            return False

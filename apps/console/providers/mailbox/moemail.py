# MoeMail 临时邮箱 Provider
# 基于 cloudflare_temp_email 自建邮箱服务

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
class MoeMailProvider(BaseMailbox):
    """
    MoeMail 临时邮箱 Provider。

    基于 cloudflare_temp_email 自建邮箱服务。

    需要配置：
    - moemail_api_url: MoeMail 服务的 API 地址
    - moemail_domain: 邮箱域名（可选，默认从 API 获取）
    """

    name = "moemail"
    display_name = "MoeMail"

    def __init__(self, config: Optional[Dict[str, Any]] = None, proxy: Optional[str] = None):
        super().__init__(config=config, proxy=proxy)
        self.api_url = (config or {}).get("moemail_api_url", "")
        self.domain = (config or {}).get("moemail_domain", "")
        self.poll_interval = (config or {}).get("poll_interval", 5)

    def create(self) -> MailboxAccount:
        """
        自动注册临时账号并生成邮箱。

        Raises:
            NotImplementedError: 需要配置 moemail_api_url。
        """
        if not self.api_url:
            raise NotImplementedError(
                "MoeMail Provider 需要配置 moemail_api_url。"
                "请在 settings 中设置 moemail_api_url 为你的 MoeMail 服务地址。"
            )

        self.log("[MoeMail] 正在创建临时邮箱...")

        try:
            with httpx.Client(proxy=self.proxy, timeout=30) as client:
                # 生成随机用户名
                username = f"reg_{secrets.token_hex(6)}"
                password = secrets.token_hex(8)

                # 注册临时账号
                resp = client.post(
                    f"{self.api_url}/api/register",
                    json={
                        "username": username,
                        "password": password,
                        "domain": self.domain,
                    },
                )
                resp.raise_for_status()
                result = resp.json()

                email = result.get("email", f"{username}@{self.domain}")
                account_id = result.get("id", "")

                self.log(f"[MoeMail] 邮箱创建成功: {email}")
                return MailboxAccount(
                    email=email,
                    password=password,
                    account_id=account_id,
                    extra={
                        "domain": self.domain,
                        "api_url": self.api_url,
                        "token": result.get("token", ""),
                    },
                )

        except httpx.HTTPError as exc:
            self.log(f"[MoeMail] API 请求失败: {exc}")
            raise RuntimeError(f"MoeMail API 请求失败: {exc}") from exc

    def wait_otp(self, email: str, timeout: int = 120) -> str:
        """
        轮询 MoeMail API 获取验证码。

        Args:
            email: 目标邮箱地址。
            timeout: 最大等待秒数。

        Returns:
            提取到的 OTP 验证码字符串。

        Raises:
            TimeoutError: 超时未收到验证码。
            NotImplementedError: 未配置 moemail_api_url。
        """
        if not self.api_url:
            raise NotImplementedError(
                "MoeMail Provider 需要配置 moemail_api_url 才能获取 OTP。"
            )

        self.log(f"[MoeMail] 等待 OTP: {email}, 超时={timeout}s")
        start_time = time.time()

        with httpx.Client(proxy=self.proxy, timeout=30) as client:
            while time.time() - start_time < timeout:
                try:
                    resp = client.get(
                        f"{self.api_url}/api/messages",
                        params={"email": email},
                    )
                    resp.raise_for_status()
                    messages = resp.json()

                    if messages and isinstance(messages, list) and len(messages) > 0:
                        # 取最新邮件
                        latest = messages[0]
                        body = latest.get("body", "") or latest.get("html", "") or latest.get("text", "")

                        # 提取 6 位数字验证码
                        otp_match = re.search(r"\b(\d{6})\b", body)
                        if otp_match:
                            otp = otp_match.group(1)
                            self.log(f"[MoeMail] 获取到 OTP: {otp}")
                            return otp

                except httpx.HTTPError as exc:
                    self.log(f"[MoeMail] 轮询失败: {exc}")

                time.sleep(self.poll_interval)

        raise TimeoutError(f"[MoeMail] 等待 OTP 超时 ({timeout}s): {email}")

    def list_domains(self) -> List[str]:
        """返回 MoeMail 可用域名列表。"""
        if not self.api_url:
            return []

        try:
            with httpx.Client(proxy=self.proxy, timeout=10) as client:
                resp = client.get(f"{self.api_url}/api/domains")
                resp.raise_for_status()
                return resp.json()
        except Exception:
            return [self.domain] if self.domain else []

    def test_connectivity(self) -> bool:
        """测试 MoeMail API 连通性。"""
        if not self.api_url:
            return False

        try:
            with httpx.Client(proxy=self.proxy, timeout=10) as client:
                resp = client.get(f"{self.api_url}/api/health")
                return resp.status_code == 200
        except Exception:
            return False

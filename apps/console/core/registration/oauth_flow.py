"""
OAuth 模式注册流程。

对应 Requirement 1 AC6。

流程：
1. 启动 OAuth 浏览器会话（Google / GitHub / Microsoft）
2. 用户授权（或自动化填充已登录 session）
3. 回调获取 token
4. 返回 Account
"""
from __future__ import annotations

from typing import Callable, Optional

from core.base_platform import Account, AccountStatus
from core.registration.base_flow import BaseRegistrationFlow
from core.registration.context import RegistrationContext


class OAuthRegistrationFlow(BaseRegistrationFlow):
    """
    OAuth 模式注册流程。

    子类通过 oauth_runner / result_mapper 注入具体逻辑。
    """

    def __init__(
        self,
        ctx: Optional[RegistrationContext] = None,
        oauth_runner: Optional[Callable] = None,
        result_mapper: Optional[Callable] = None,
    ):
        super().__init__(ctx)
        self._oauth_runner = oauth_runner
        self._result_mapper = result_mapper

    def run(self, email: Optional[str] = None, password: str = "") -> Account:
        ctx = self.ctx
        if ctx is None:
            raise RuntimeError("RegistrationContext 未设置")

        ctx.checkpoint()

        if self._oauth_runner is None:
            raise NotImplementedError("oauth_runner 未提供")

        ctx.log("启动 OAuth 注册流程...")
        ctx.emit("phase", phase="oauth_start")

        # 执行 OAuth 流程
        result = self._oauth_runner(ctx)

        ctx.checkpoint()

        # 映射结果
        if self._result_mapper:
            account = self._result_mapper(ctx, result)
        else:
            account = Account(
                platform=ctx.platform_name,
                email=result.get("email", email or ""),
                password="",
                token=result.get("accessToken", "") or result.get("token", ""),
                status=AccountStatus.REGISTERED,
                extra=result if isinstance(result, dict) else {},
            )

        ctx.emit("success", email=account.email)
        return account

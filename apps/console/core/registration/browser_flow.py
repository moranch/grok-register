"""
浏览器模式注册流程（headless / headed）。

对应 Requirement 2 AC9。

流程：
1. 启动浏览器（DrissionPage / Playwright）
2. 导航到注册页
3. 填写表单
4. 处理验证码（turnstilePatch / 远程 solver）
5. 等待 OTP（如需要）
6. 提取凭证
7. 返回 Account
"""
from __future__ import annotations

from typing import Callable, Optional

from core.base_platform import Account, AccountStatus
from core.registration.base_flow import BaseRegistrationFlow
from core.registration.context import RegistrationContext


class BrowserRegistrationFlow(BaseRegistrationFlow):
    """
    浏览器模式注册流程。

    子类（平台 BrowserRegister）通过 browser_worker_builder / browser_register_runner 注入具体逻辑。
    """

    def __init__(
        self,
        ctx: Optional[RegistrationContext] = None,
        browser_worker_builder: Optional[Callable] = None,
        browser_register_runner: Optional[Callable] = None,
        result_mapper: Optional[Callable] = None,
        otp_wait_message: str = "等待验证码...",
        otp_timeout: int = 120,
    ):
        super().__init__(ctx)
        self._browser_worker_builder = browser_worker_builder
        self._browser_register_runner = browser_register_runner
        self._result_mapper = result_mapper
        self._otp_wait_message = otp_wait_message
        self._otp_timeout = otp_timeout

    def run(self, email: Optional[str] = None, password: str = "") -> Account:
        ctx = self.ctx
        if ctx is None:
            raise RuntimeError("RegistrationContext 未设置")

        ctx.checkpoint()

        # 1. 创建邮箱（如需要）
        if ctx.mailbox and not email:
            ctx.log("创建临时邮箱...")
            mailbox_account = ctx.mailbox.create()
            email = mailbox_account.email
            ctx.emit("mailbox_created", email=email)
            ctx.log(f"邮箱: {email}")

        ctx.checkpoint()

        # 2. 构建浏览器 Worker
        if self._browser_worker_builder is None:
            raise NotImplementedError("browser_worker_builder 未提供")

        def otp_callback() -> str:
            ctx.log(self._otp_wait_message)
            if ctx.mailbox:
                code = ctx.mailbox.wait_otp(email or "", timeout=self._otp_timeout)
                ctx.emit("otp_received", code=code)
                return code
            raise RuntimeError("无邮箱实例，无法获取 OTP")

        worker = self._browser_worker_builder(ctx, otp_callback)

        ctx.checkpoint()

        # 3. 执行浏览器注册
        if self._browser_register_runner:
            result = self._browser_register_runner(worker, ctx, email, password)
        else:
            result = worker.run(email=email, password=password)

        ctx.checkpoint()

        # 4. 映射结果
        if self._result_mapper:
            account = self._result_mapper(ctx, result)
        else:
            account = Account(
                platform=ctx.platform_name,
                email=result.get("email", email or ""),
                password=result.get("password", password),
                token=result.get("token", "") or result.get("sso", ""),
                status=AccountStatus.REGISTERED,
                extra=result if isinstance(result, dict) else {},
            )

        ctx.emit("success", email=account.email)
        return account

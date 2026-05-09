"""
协议模式 + 邮箱验证码注册流程。

对应 Requirement 1 AC5, Requirement 2 AC3。

流程：
1. 创建临时邮箱
2. 调用目标平台注册 API（curl_cffi）
3. 等待 OTP 验证码
4. 提交验证码
5. 提取 token / 凭证
6. 返回 Account
"""
from __future__ import annotations

from typing import Callable, Optional

from core.base_platform import Account, AccountStatus
from core.registration.base_flow import BaseRegistrationFlow
from core.registration.context import RegistrationContext


class ProtocolMailboxFlow(BaseRegistrationFlow):
    """
    协议模式邮箱注册流程。

    子类（平台 Worker）通过 worker_builder / register_runner 注入具体逻辑。
    """

    def __init__(
        self,
        ctx: Optional[RegistrationContext] = None,
        worker_builder: Optional[Callable] = None,
        register_runner: Optional[Callable] = None,
        result_mapper: Optional[Callable] = None,
        otp_wait_message: str = "等待验证码...",
        otp_timeout: int = 120,
    ):
        super().__init__(ctx)
        self._worker_builder = worker_builder
        self._register_runner = register_runner
        self._result_mapper = result_mapper
        self._otp_wait_message = otp_wait_message
        self._otp_timeout = otp_timeout

    def run(self, email: Optional[str] = None, password: str = "") -> Account:
        ctx = self.ctx
        if ctx is None:
            raise RuntimeError("RegistrationContext 未设置")

        ctx.checkpoint()

        # 1. 创建邮箱
        if ctx.mailbox and not email:
            ctx.log("创建临时邮箱...")
            mailbox_account = ctx.mailbox.create()
            email = mailbox_account.email
            ctx.emit("mailbox_created", email=email)
            ctx.log(f"邮箱: {email}")
        elif email:
            ctx.emit("mailbox_created", email=email)

        ctx.checkpoint()

        # 2. 构建 Worker
        if self._worker_builder is None:
            raise NotImplementedError("worker_builder 未提供")

        # OTP 回调
        def otp_callback() -> str:
            ctx.log(self._otp_wait_message)
            if ctx.mailbox:
                code = ctx.mailbox.wait_otp(email or "", timeout=self._otp_timeout)
                ctx.emit("otp_received", code=code)
                ctx.log(f"收到验证码: {code}")
                return code
            raise RuntimeError("无邮箱实例，无法获取 OTP")

        worker = self._worker_builder(ctx, otp_callback)

        ctx.checkpoint()

        # 3. 执行注册
        if self._register_runner:
            result = self._register_runner(worker, ctx, email, password)
        else:
            result = worker.run(email=email, password=password)

        ctx.checkpoint()

        # 4. 映射结果
        if self._result_mapper:
            account = self._result_mapper(ctx, result)
        else:
            # 默认映射
            account = Account(
                platform=ctx.platform_name,
                email=result.get("email", email or ""),
                password=result.get("password", password),
                token=result.get("token", "") or result.get("accessToken", ""),
                status=AccountStatus.REGISTERED,
                extra=result if isinstance(result, dict) else {},
            )

        ctx.emit("success", email=account.email)
        return account

"""
AAR (any-auto-register) 适配器。

上游 BasePlatform.register(email, password) -> Account 与我们的
BasePlatform.build_register_flow(engine) -> Flow 之间存在接口差异。

本适配器做两件事：
1. 声明我们所需的元数据（name / display_name / register_engines 等）
2. 提供 build_register_flow() 的骨架实现：构建一个 wrapper flow，
   flow.run() 在有 ctx 的情况下委派给上游 vendor 平台的 register()。

supervisor 接入真多平台 dispatch 后，调 flow.run(email, password) 即可。
在此之前，任务进队列后 supervisor 仍按安全网置 FAILED——本适配器
只保证 '元数据 + 前端可见 + 任务可入库'，不保证真能跑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from core.base_platform import (
    Account,
    AccountStatus,
    BasePlatform,
    Capabilities,
    EngineSpec,
    RegisterConfig,
)
from core.registration.protocol_mailbox_flow import ProtocolMailboxFlow


class AARAdapter(BasePlatform):
    """
    子类只需声明：
        upstream_cls: Type[vendor_aar.base_platform.BasePlatform]
        name / display_name / version / supported_executors / ...
    以及（可选）register_engines / capabilities / supported_exporters
    即可复用下方的 build_register_flow 骨架。
    """

    #: 上游 vendor 的平台类（必填；子类覆盖）
    upstream_cls: Optional[Type] = None

    # —— 以下字段子类可覆盖（默认给出通用值，不强制） —— #
    supported_executors: List[str] = ["protocol", "headless", "headed"]
    capabilities: Capabilities = Capabilities(
        supports_validity_check=True,
        supports_api_push=True,
    )
    register_engines: List[EngineSpec] = [
        EngineSpec(
            id="default",
            display_name="默认",
            description="使用上游 vendor 注册流程",
            is_recommended=True,
        ),
    ]
    preferred_captcha_strategies: List[str] = ["token", "browser"]
    supported_exporters: List[str] = ["any2api"]
    default_extra_schema: Dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, config: Optional[RegisterConfig] = None):
        super().__init__(config)
        self._upstream = None

    def _get_upstream(self):
        """懒加载上游平台实例。"""
        if self._upstream is None:
            if self.upstream_cls is None:
                raise RuntimeError(
                    f"{type(self).__name__}.upstream_cls 未设置"
                )
            # 上游构造签名是 (config=None, mailbox=None)——传空占位
            try:
                self._upstream = self.upstream_cls(
                    config=self.config if self.config else None
                )
            except TypeError:
                # 有些平台构造签名不同，退回默认构造
                self._upstream = self.upstream_cls()
        return self._upstream

    def check_validity(self, account) -> bool:
        """默认委派给上游 check_valid（上游函数名少个 ity）。无实现则返回 True。"""
        upstream = self._get_upstream()
        fn = getattr(upstream, "check_valid", None) or getattr(
            upstream, "check_validity", None
        )
        if fn is None:
            return True
        try:
            return bool(fn(account))
        except Exception:
            return False

    def build_register_flow(self, engine: EngineSpec):
        """
        构建一个 wrapper flow。真执行时（flow.run）把邮箱/OTP 回调转给
        上游 vendor 的 register()。
        """
        upstream = self._get_upstream

        def worker_builder(ctx, otp_callback):
            return _UpstreamWorker(
                get_upstream=upstream,
                engine_id=engine.id,
                ctx=ctx,
                otp_callback=otp_callback,
            )

        def result_mapper(ctx, result):
            # result 形如 vendor Account 实例，统一映射到我们的 Account。
            if isinstance(result, dict):
                email = result.get("email", "")
                token = result.get("token", "") or result.get("accessToken", "")
                password = result.get("password", "")
                extra = {k: v for k, v in result.items()
                         if k not in {"email", "password", "token"}}
            else:
                email = getattr(result, "email", "")
                token = getattr(result, "token", "")
                password = getattr(result, "password", "")
                extra = getattr(result, "extra", {}) or {}
            return Account(
                platform=self.name,
                email=email,
                password=password,
                token=token,
                status=AccountStatus.REGISTERED,
                extra=extra,
            )

        flow = ProtocolMailboxFlow(
            worker_builder=worker_builder,
            result_mapper=result_mapper,
            otp_wait_message=f"等待 {self.display_name} 验证码...",
            otp_timeout=120,
        )
        flow.set_context(None)
        return flow


class _UpstreamWorker:
    """把 vendor BasePlatform.register() 包成我们的 worker.run() 形式。"""

    def __init__(self, get_upstream, engine_id: str, ctx, otp_callback):
        self._get_upstream = get_upstream
        self.engine_id = engine_id
        self.ctx = ctx
        self.otp_callback = otp_callback

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        upstream = self._get_upstream()
        # 上游 register 内部自己处理 OTP（通过绑定的 mailbox），engine_id 被忽略——
        # 如果子类的上游支持多引擎，子类可以覆盖 build_register_flow 传不同参数。
        try:
            account = upstream.register(email=email, password=password)
        except NotImplementedError:
            raise
        except Exception:
            raise
        # 返回一个 dict，供 result_mapper 归一化
        return {
            "email": getattr(account, "email", email),
            "password": getattr(account, "password", password),
            "token": getattr(account, "token", ""),
            **(getattr(account, "extra", {}) or {}),
        }

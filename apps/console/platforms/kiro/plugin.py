"""
Kiro 平台插件 — 基于 AWS Builder ID 注册。

对应 Requirement 1 AC6, Requirement 14 AC1, Requirement 17 AC1。

- 支持 OAuth（Google/GitHub）+ 邮箱 OTP 双路径
- 双引擎：access_token / refresh_token
- 支持 token 刷新 + 账号切换
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.base_platform import (
    Account,
    AccountStatus,
    BasePlatform,
    Capabilities,
    EngineSpec,
    RegisterConfig,
)
from core.registry import register_platform

logger = logging.getLogger(__name__)


@register_platform
class KiroPlatform(BasePlatform):
    """Kiro (AWS Builder ID) 平台插件。"""

    name = "kiro"
    display_name = "Kiro"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    capabilities = Capabilities(
        supports_oauth=True,
        supports_refresh=True,
        supports_trial_info=True,
        supports_switch=True,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="refresh_token",
            display_name="Refresh Token 模式",
            description="走新 PR 链路，产出 Access Token + Refresh Token，支持自动续期",
            is_recommended=True,
        ),
        EngineSpec(
            id="access_token",
            display_name="Access Token 模式",
            description="仅产出 Access Token，不支持自动续期",
        ),
    ]
    preferred_captcha_strategies = ["token", "browser"]
    supported_exporters = ["any2api", "kiro_account_manager"]
    default_extra_schema = {
        "type": "object",
        "properties": {
            "kiro_account_manager_url": {
                "type": "string",
                "title": "Kiro Account Manager 地址",
                "default": "",
            },
            "oauth_providers": {
                "type": "array",
                "title": "OAuth Providers",
                "items": {"type": "string", "enum": ["google", "github", "microsoft"]},
                "default": [],
            },
        },
    }

    def __init__(self, config: Optional[RegisterConfig] = None):
        super().__init__(config)

    def build_register_flow(self, engine: EngineSpec):
        """构建协议模式注册 Flow（邮箱 OTP 路径）。"""
        from core.registration.protocol_mailbox_flow import ProtocolMailboxFlow

        def worker_builder(ctx, otp_callback):
            return KiroProtocolWorker(
                proxy=ctx.proxy,
                engine_id=engine.id,
                log_fn=ctx.log,
                otp_callback=otp_callback,
            )

        def result_mapper(ctx, result):
            return Account(
                platform="kiro",
                email=result.get("email", ""),
                password=result.get("password", ""),
                token=result.get("accessToken", ""),
                status=AccountStatus.REGISTERED,
                extra={
                    "accessToken": result.get("accessToken", ""),
                    "refreshToken": result.get("refreshToken", ""),
                    "clientId": result.get("clientId", ""),
                    "clientSecret": result.get("clientSecret", ""),
                    "sessionToken": result.get("sessionToken", ""),
                    "csrfToken": result.get("csrfToken", ""),
                    "oauthProvider": result.get("oauthProvider", ""),
                    "name": result.get("name", ""),
                },
            )

        flow = ProtocolMailboxFlow(
            worker_builder=worker_builder,
            result_mapper=result_mapper,
            otp_wait_message="等待 Kiro 验证码...",
            otp_timeout=120,
        )
        flow.set_context(None)
        return flow

    def build_browser_flow(self, engine: EngineSpec):
        """构建浏览器模式注册 Flow。"""
        from core.registration.browser_flow import BrowserRegistrationFlow

        def browser_worker_builder(ctx, otp_callback):
            return KiroBrowserWorker(
                proxy=ctx.proxy,
                headless=(ctx.executor_type == "headless"),
                engine_id=engine.id,
                log_fn=ctx.log,
                otp_callback=otp_callback,
            )

        def result_mapper(ctx, result):
            return Account(
                platform="kiro",
                email=result.get("email", ""),
                password=result.get("password", ""),
                token=result.get("accessToken", ""),
                status=AccountStatus.REGISTERED,
                extra={
                    "accessToken": result.get("accessToken", ""),
                    "refreshToken": result.get("refreshToken", ""),
                    "clientId": result.get("clientId", ""),
                    "clientSecret": result.get("clientSecret", ""),
                    "oauthProvider": result.get("oauthProvider", ""),
                },
            )

        flow = BrowserRegistrationFlow(
            browser_worker_builder=browser_worker_builder,
            result_mapper=result_mapper,
        )
        flow.set_context(None)
        return flow

    def check_validity(self, account) -> bool:
        """通过 refreshToken 检测账号是否有效。"""
        import json

        if isinstance(account, dict):
            extra_raw = account.get("extra_json", "{}")
            if isinstance(extra_raw, str):
                try:
                    extra = json.loads(extra_raw)
                except (json.JSONDecodeError, TypeError):
                    extra = {}
            else:
                extra = extra_raw
        else:
            extra = getattr(account, "extra", {})

        refresh_token = extra.get("refreshToken", "")
        if not refresh_token:
            # 没有 refresh_token，检查 accessToken
            return bool(extra.get("accessToken", ""))

        # 尝试用 refresh_token 刷新
        try:
            result = self._refresh_kiro_token(
                refresh_token,
                extra.get("clientId", ""),
                extra.get("clientSecret", ""),
            )
            return result is not None
        except Exception:
            return False

    def refresh_token(self, account) -> Optional[Dict[str, Any]]:
        """刷新 Kiro token。"""
        import json

        if isinstance(account, dict):
            extra_raw = account.get("extra_json", "{}")
            if isinstance(extra_raw, str):
                try:
                    extra = json.loads(extra_raw)
                except (json.JSONDecodeError, TypeError):
                    extra = {}
            else:
                extra = extra_raw
        else:
            extra = getattr(account, "extra", {})

        refresh_token = extra.get("refreshToken", "")
        client_id = extra.get("clientId", "")
        client_secret = extra.get("clientSecret", "")

        if not refresh_token or not client_id:
            return None

        return self._refresh_kiro_token(refresh_token, client_id, client_secret)

    def _refresh_kiro_token(
        self, refresh_token: str, client_id: str, client_secret: str
    ) -> Optional[Dict[str, Any]]:
        """
        调用 AWS OIDC token endpoint 刷新 token。

        实际实现需要 curl_cffi 调用：
        POST https://oidc.us-east-1.amazonaws.com/token
        grant_type=refresh_token&client_id=...&refresh_token=...
        """
        # TODO: 实现具体的 AWS OIDC token 刷新逻辑
        logger.info("[Kiro] Token 刷新（骨架实现）")
        return None

    def get_platform_actions(self) -> List[Dict[str, Any]]:
        return [
            {"id": "switch_desktop", "label": "切换到桌面应用", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "query_state", "label": "查询账号状态", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: Dict[str, Any]) -> Dict[str, Any]:
        if action_id == "refresh_token":
            result = self.refresh_token(account)
            if result:
                return {"ok": True, "data": result}
            return {"ok": False, "error": "刷新失败"}

        if action_id == "query_state":
            is_valid = self.check_validity(account)
            return {"ok": True, "data": {"valid": is_valid}}

        if action_id == "switch_desktop":
            return {"ok": True, "message": "桌面切换功能待实现"}

        raise NotImplementedError(f"未知操作: {action_id}")


# ─── Workers ─────────────────────────────────────────────────────────────────


class KiroProtocolWorker:
    """Kiro 协议模式注册 Worker（curl_cffi + AWS Builder ID API）。"""

    def __init__(self, proxy=None, engine_id="refresh_token", log_fn=None, otp_callback=None):
        self.proxy = proxy
        self.engine_id = engine_id
        self.log = log_fn or print
        self.otp_callback = otp_callback

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        """
        执行 Kiro 协议注册。

        流程：
        1. POST /register (AWS Builder ID signup)
        2. 等待邮箱 OTP
        3. POST /verify-email
        4. 获取 accessToken + refreshToken

        当前为骨架实现。
        """
        self.log(f"[Kiro Protocol] 开始注册: {email}, engine={self.engine_id}")
        self.log(f"[Kiro Protocol] 代理: {self.proxy or '无'}")

        # TODO: 实现具体的 AWS Builder ID 注册协议
        # 参考 lxf746/any-auto-register 的 platforms/kiro/core.py (78KB)
        raise NotImplementedError(
            "Kiro 协议模式注册尚未实现具体协议逻辑，"
            "请等待后续更新或参考上游 lxf746/any-auto-register"
        )


class KiroBrowserWorker:
    """Kiro 浏览器模式注册 Worker。"""

    def __init__(self, proxy=None, headless=True, engine_id="refresh_token", log_fn=None, otp_callback=None):
        self.proxy = proxy
        self.headless = headless
        self.engine_id = engine_id
        self.log = log_fn or print
        self.otp_callback = otp_callback

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        """使用浏览器执行 Kiro 注册。"""
        self.log(f"[Kiro Browser] 开始注册: {email}, engine={self.engine_id}")
        self.log(f"[Kiro Browser] headless={self.headless}, proxy={self.proxy or '无'}")

        # TODO: 实现浏览器自动化注册
        raise NotImplementedError(
            "Kiro 浏览器模式注册尚未实现，"
            "请等待后续更新"
        )

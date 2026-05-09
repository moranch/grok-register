"""
Grok 平台插件 — 收编现有 email_register.py。

对应 Requirement 1 AC7, Requirement 14 AC1。

- executor=headless 时以子进程方式调用旧脚本（向后兼容）
- executor=protocol 时使用新的 ProtocolMailboxFlow（curl_cffi）
- 支持 turnstilePatch 本地验证码
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
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

# 旧脚本路径（相对于项目根目录）
LEGACY_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "email_register.py"
)


@register_platform
class GrokPlatform(BasePlatform):
    """Grok (x.ai) 平台插件。"""

    name = "grok"
    display_name = "Grok"
    version = "2.0.0"
    supported_executors = ["headless", "headed", "protocol"]
    capabilities = Capabilities(
        supports_oauth=False,
        supports_refresh=False,
        supports_trial_info=False,
        supports_switch=False,
        supports_api_push=True,
        supports_validity_check=True,
    )
    register_engines = [
        EngineSpec(
            id="email_otp",
            display_name="邮箱 OTP 注册",
            description="通过临时邮箱接收 OTP 验证码完成注册",
            is_recommended=True,
        ),
        EngineSpec(
            id="legacy_script",
            display_name="旧版脚本（兼容）",
            description="调用原 email_register.py 子进程",
            deprecated=True,
            supported_executors=["headless", "headed"],
        ),
    ]
    preferred_captcha_strategies = ["browser", "token"]
    supported_exporters = ["any2api", "grok2api"]
    default_extra_schema = {
        "type": "object",
        "properties": {
            "grok2api_url": {"type": "string", "title": "grok2api 地址", "default": ""},
            "grok2api_token": {"type": "string", "title": "grok2api Token", "default": ""},
        },
    }

    def __init__(self, config: Optional[RegisterConfig] = None):
        super().__init__(config)

    def build_register_flow(self, engine: EngineSpec):
        """构建协议模式注册 Flow。"""
        if engine.id == "legacy_script":
            return None  # legacy 走 _run_browser_flow

        from core.registration.protocol_mailbox_flow import ProtocolMailboxFlow

        def worker_builder(ctx, otp_callback):
            return GrokProtocolWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
                otp_callback=otp_callback,
            )

        def result_mapper(ctx, result):
            return Account(
                platform="grok",
                email=result.get("email", ""),
                password=result.get("password", ""),
                token=result.get("sso", ""),
                status=AccountStatus.REGISTERED,
                extra={
                    "sso": result.get("sso", ""),
                    "sso_rw": result.get("sso_rw", ""),
                    "given_name": result.get("given_name", ""),
                    "family_name": result.get("family_name", ""),
                },
            )

        flow = ProtocolMailboxFlow(
            worker_builder=worker_builder,
            result_mapper=result_mapper,
            otp_wait_message="等待 Grok 验证码...",
            otp_timeout=120,
        )
        flow.set_context(None)  # 由 TaskRuntime 在执行时注入
        return flow

    def build_browser_flow(self, engine: EngineSpec):
        """构建浏览器模式注册 Flow（含 legacy 兼容）。"""
        from core.registration.browser_flow import BrowserRegistrationFlow

        if engine.id == "legacy_script":
            # 向后兼容：以子进程方式调用旧脚本
            def browser_worker_builder(ctx, otp_callback):
                return GrokLegacyWorker(
                    config=ctx.extra,
                    proxy=ctx.proxy,
                    log_fn=ctx.log,
                )

            def result_mapper(ctx, result):
                return Account(
                    platform="grok",
                    email=result.get("email", ""),
                    password="",
                    token=result.get("sso", ""),
                    status=AccountStatus.REGISTERED,
                    extra=result,
                )

            flow = BrowserRegistrationFlow(
                browser_worker_builder=browser_worker_builder,
                result_mapper=result_mapper,
            )
            flow.set_context(None)
            return flow

        # 新版浏览器注册（DrissionPage）
        def browser_worker_builder(ctx, otp_callback):
            return GrokBrowserWorker(
                proxy=ctx.proxy,
                headless=(ctx.executor_type == "headless"),
                log_fn=ctx.log,
                otp_callback=otp_callback,
            )

        def result_mapper(ctx, result):
            return Account(
                platform="grok",
                email=result.get("email", ""),
                password=result.get("password", ""),
                token=result.get("sso", ""),
                status=AccountStatus.REGISTERED,
                extra={
                    "sso": result.get("sso", ""),
                    "sso_rw": result.get("sso_rw", ""),
                },
            )

        flow = BrowserRegistrationFlow(
            browser_worker_builder=browser_worker_builder,
            result_mapper=result_mapper,
        )
        flow.set_context(None)
        return flow

    def check_validity(self, account) -> bool:
        """检测 Grok 账号是否有效（通过 sso cookie 验证）。"""
        if isinstance(account, dict):
            sso = account.get("sso", "") or account.get("extra_json", {}).get("sso", "")
        else:
            sso = getattr(account, "token", "") or ""
        return bool(sso and len(sso) > 10)

    def get_platform_actions(self) -> List[Dict[str, Any]]:
        return [
            {"id": "push_grok2api", "label": "推送到 grok2api", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: Dict[str, Any]) -> Dict[str, Any]:
        if action_id == "push_grok2api":
            # 由 Exporter 体系处理
            return {"ok": True, "message": "请使用 Exporter 推送"}
        raise NotImplementedError(f"未知操作: {action_id}")


# ─── Workers ─────────────────────────────────────────────────────────────────


class GrokProtocolWorker:
    """Grok 协议模式注册 Worker（curl_cffi）。"""

    def __init__(self, proxy=None, log_fn=None, otp_callback=None):
        self.proxy = proxy
        self.log = log_fn or print
        self.otp_callback = otp_callback

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        """
        执行 Grok 协议注册。

        实际实现需要：
        1. 调用 x.ai signup API
        2. 等待 OTP
        3. 提交验证码
        4. 提取 sso cookie

        当前为骨架实现，具体协议逻辑待后续填充。
        """
        self.log(f"[Grok Protocol] 开始注册: {email}")
        self.log(f"[Grok Protocol] 代理: {self.proxy or '无'}")

        # TODO: 实现具体的 x.ai 注册协议
        # 1. POST https://x.ai/api/auth/signup
        # 2. 等待 OTP
        # 3. POST https://x.ai/api/auth/verify
        # 4. 提取 sso cookie

        raise NotImplementedError(
            "Grok 协议模式注册尚未实现具体协议逻辑，"
            "请使用 engine=legacy_script + executor=headless 或等待后续更新"
        )


class GrokLegacyWorker:
    """
    Grok 旧版脚本 Worker（向后兼容）。

    以子进程方式调用 email_register.py，解析 stdout 日志。
    对应 Requirement 1 AC7。
    """

    def __init__(self, config=None, proxy=None, log_fn=None):
        self.config = config or {}
        self.proxy = proxy
        self.log = log_fn or print

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        """以子进程方式调用旧脚本。"""
        script_path = os.path.abspath(LEGACY_SCRIPT)
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"旧版脚本不存在: {script_path}")

        self.log(f"[Grok Legacy] 调用旧脚本: {script_path}")

        env = os.environ.copy()
        if self.proxy:
            env["HTTP_PROXY"] = self.proxy
            env["HTTPS_PROXY"] = self.proxy

        try:
            result = subprocess.run(
                ["python", script_path],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
                cwd=os.path.dirname(script_path),
            )

            self.log(f"[Grok Legacy] 退出码: {result.returncode}")

            if result.returncode != 0:
                self.log(f"[Grok Legacy] stderr: {result.stderr[:500]}")
                raise RuntimeError(f"旧脚本执行失败: {result.stderr[:200]}")

            # 解析 stdout 中的注册结果
            return self._parse_output(result.stdout)

        except subprocess.TimeoutExpired:
            raise TimeoutError("旧脚本执行超时 (300s)")

    def _parse_output(self, stdout: str) -> Dict[str, Any]:
        """解析旧脚本的 stdout 输出，提取注册结果。"""
        # 旧脚本输出格式：JSON 行或特定标记
        lines = stdout.strip().split("\n")

        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    if "sso" in data or "email" in data:
                        return data
                except json.JSONDecodeError:
                    continue

        # 尝试从日志中提取 email 和 sso
        result = {"email": "", "sso": ""}
        for line in lines:
            if "email:" in line.lower() or "邮箱:" in line:
                parts = line.split(":", 1)
                if len(parts) > 1:
                    result["email"] = parts[1].strip()
            if "sso:" in line.lower() or "cookie:" in line.lower():
                parts = line.split(":", 1)
                if len(parts) > 1:
                    result["sso"] = parts[1].strip()

        if not result["sso"]:
            raise RuntimeError("无法从旧脚本输出中解析到 sso token")

        return result


class GrokBrowserWorker:
    """Grok 浏览器模式注册 Worker（DrissionPage）。"""

    def __init__(self, proxy=None, headless=True, log_fn=None, otp_callback=None):
        self.proxy = proxy
        self.headless = headless
        self.log = log_fn or print
        self.otp_callback = otp_callback

    def run(self, email: str = "", password: str = "") -> Dict[str, Any]:
        """
        使用 DrissionPage 执行浏览器注册。

        当前为骨架，具体浏览器自动化逻辑复用现有 email_register.py 的核心流程。
        """
        self.log(f"[Grok Browser] 开始注册: {email}")
        self.log(f"[Grok Browser] headless={self.headless}, proxy={self.proxy or '无'}")

        # TODO: 将 email_register.py 的核心浏览器逻辑迁移到此处
        raise NotImplementedError(
            "Grok 浏览器模式 Worker 尚未完成迁移，"
            "请使用 engine=legacy_script 调用旧脚本"
        )

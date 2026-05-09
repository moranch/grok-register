"""
平台插件基类 + EngineSpec + Capabilities 数据类。

对应 Requirement 1 AC2/AC5/AC8/AC9/AC10, Requirement 17 AC1。
子类只需声明类变量 + 实现 build_*_adapter 工厂方法。
"""
from __future__ import annotations

import random
import string
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.registration.context import RegistrationContext


# ─── 数据类 ──────────────────────────────────────────────────────────────────


class AccountStatus(str, Enum):
    REGISTERED = "registered"
    TRIAL = "trial"
    SUBSCRIBED = "subscribed"
    EXPIRED = "expired"
    INVALID = "invalid"


@dataclass
class EngineSpec:
    """
    注册引擎声明（Requirement 1 AC8, Requirement 17 AC1）。

    一个平台可以有多个注册引擎（例如 ChatGPT 的 access_token_only / refresh_token）。
    """
    id: str
    display_name: str
    description: str = ""
    is_recommended: bool = False
    deprecated: bool = False
    supported_executors: Optional[List[str]] = None  # 为 None 时继承平台级声明


@dataclass
class Capabilities:
    """平台能力位声明。"""
    supports_oauth: bool = False
    supports_refresh: bool = False
    supports_trial_info: bool = False
    supports_switch: bool = False
    supports_api_push: bool = False
    supports_validity_check: bool = True
    supports_batch_status_sync: bool = False
    supports_remote_auth_file: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return {
            "supports_oauth": self.supports_oauth,
            "supports_refresh": self.supports_refresh,
            "supports_trial_info": self.supports_trial_info,
            "supports_switch": self.supports_switch,
            "supports_api_push": self.supports_api_push,
            "supports_validity_check": self.supports_validity_check,
            "supports_batch_status_sync": self.supports_batch_status_sync,
            "supports_remote_auth_file": self.supports_remote_auth_file,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Capabilities":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Account:
    """注册成功后的账号实体。"""
    platform: str
    email: str
    password: str = ""
    user_id: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    trial_end_time: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    """注册任务配置（传入 register() 的上下文配置部分）。"""
    executor_type: str = "protocol"
    captcha_solver: str = "auto"
    proxy: Optional[str] = None
    engine_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# ─── BasePlatform 抽象基类 ────────────────────────────────────────────────────


class BasePlatform(ABC):
    """
    平台插件抽象基类。

    子类必须声明：
    - name: str
    - display_name: str
    - supported_executors: List[str]

    可选声明：
    - version: str
    - capabilities: Capabilities
    - register_engines: List[EngineSpec]
    - preferred_captcha_strategies: List[str]
    - supported_exporters: List[str]
    - default_extra_schema: Dict[str, Any]  (JSON Schema 子集)
    """

    # ── 子类必须声明 ──
    name: str = ""
    display_name: str = ""
    supported_executors: List[str] = []

    # ── 子类可选声明 ──
    version: str = "1.0.0"
    capabilities: Capabilities = field(default_factory=Capabilities) if False else Capabilities()
    register_engines: List[EngineSpec] = []
    preferred_captcha_strategies: List[str] = []
    supported_exporters: List[str] = ["any2api"]
    default_extra_schema: Dict[str, Any] = {}

    def __init__(self, config: Optional[RegisterConfig] = None):
        self.config = config or RegisterConfig()
        self._log_fn = print

    # ── 日志 ──

    def set_logger(self, logger):
        self._log_fn = logger or print

    def log(self, message: str):
        self._log_fn(message)

    # ── 引擎解析 ──

    def get_engine(self, engine_id: Optional[str] = None) -> EngineSpec:
        """
        获取指定 engine_id 的 EngineSpec。

        - engine_id 为 None 或 "default" 时返回推荐引擎或第一个。
        - engine_id 不存在时抛 ValueError（对应 Req 1 AC10）。
        """
        engines = self.get_register_engines()

        if not engine_id or engine_id == "default":
            # 返回 is_recommended 的，否则第一个
            for e in engines:
                if e.is_recommended:
                    return e
            return engines[0] if engines else EngineSpec(id="default", display_name="Default")

        for e in engines:
            if e.id == engine_id:
                return e

        available = [e.id for e in engines]
        raise ValueError(
            f"引擎 '{engine_id}' 不存在于平台 '{self.name}'，可用: {available}"
        )

    def get_register_engines(self) -> List[EngineSpec]:
        """返回平台的注册引擎列表，未声明时返回默认单引擎。"""
        if self.register_engines:
            return self.register_engines
        return [EngineSpec(id="default", display_name="Default", is_recommended=True)]

    # ── 注册主流程 ──

    def register(self, email: Optional[str] = None, password: Optional[str] = None) -> Account:
        """
        注册主入口。子类通常不需要覆盖此方法。

        流程：
        1. 解析 engine
        2. 准备密码
        3. 根据 executor_type 选择 Flow
        4. 执行 Flow → Account
        """
        engine = self.get_engine(self.config.engine_id)
        resolved_password = self._prepare_password(password)

        # 确定实际可用的 executor_type
        effective_executors = engine.supported_executors or self.supported_executors
        if self.config.executor_type not in effective_executors:
            raise NotImplementedError(
                f"{self.display_name} 引擎 '{engine.id}' 不支持执行器 "
                f"'{self.config.executor_type}'，可用: {effective_executors}"
            )

        self.log(f"[{self.display_name}] 引擎={engine.id}, 执行器={self.config.executor_type}")

        # 根据 executor_type 分发到不同 Flow
        if self.config.executor_type in ("headless", "headed"):
            return self._run_browser_flow(email, resolved_password, engine)
        else:
            return self._run_protocol_flow(email, resolved_password, engine)

    def _run_protocol_flow(self, email: Optional[str], password: str, engine: EngineSpec) -> Account:
        """协议模式注册（子类通过 build_register_flow 提供具体实现）。"""
        flow = self.build_register_flow(engine)
        if flow is None:
            raise NotImplementedError(
                f"{self.display_name} 未实现协议模式注册 (engine={engine.id})"
            )
        return flow.run(email=email, password=password)

    def _run_browser_flow(self, email: Optional[str], password: str, engine: EngineSpec) -> Account:
        """浏览器模式注册（子类通过 build_browser_flow 提供具体实现）。"""
        flow = self.build_browser_flow(engine)
        if flow is None:
            raise NotImplementedError(
                f"{self.display_name} 未实现浏览器模式注册 (engine={engine.id})"
            )
        return flow.run(email=email, password=password)

    # ── 子类工厂方法（按需实现）──

    def build_register_flow(self, engine: EngineSpec):
        """构建协议模式注册 Flow。子类按需实现。"""
        return None

    def build_browser_flow(self, engine: EngineSpec):
        """构建浏览器模式注册 Flow。子类按需实现。"""
        return None

    def build_mailbox_adapter(self):
        """构建邮箱适配器。子类按需实现。"""
        return None

    def build_captcha_adapter(self):
        """构建验证码适配器。子类按需实现。"""
        return None

    # ── 生命周期方法 ──

    @abstractmethod
    def check_validity(self, account: Account) -> bool:
        """检测账号是否有效（Requirement 8）。"""
        ...

    def refresh_token(self, account: Account) -> Optional[Dict[str, Any]]:
        """刷新 token（Requirement 8 AC2）。返回新 token 字典或 None。"""
        return None

    def fetch_trial_info(self, account: Account) -> Optional[Dict[str, Any]]:
        """获取试用信息（Requirement 8 AC3）。"""
        return None

    def batch_sync_status(self, accounts: List[Account]) -> List[Dict[str, Any]]:
        """批量状态同步（Requirement 16）。"""
        return []

    def backfill_remote_auth(self, accounts: List[Account]) -> List[Dict[str, Any]]:
        """补传远端 auth-file（Requirement 16 AC3）。"""
        return []

    # ── 平台动作 ──

    def get_platform_actions(self) -> List[Dict[str, Any]]:
        """返回平台支持的额外操作列表。"""
        return []

    def execute_action(self, action_id: str, account: Account, params: Dict[str, Any]) -> Dict[str, Any]:
        """执行平台特定操作。"""
        raise NotImplementedError(f"未知操作: {action_id}")

    # ── 元数据序列化（Requirement 15）──

    def to_dict(self) -> Dict[str, Any]:
        """序列化平台元数据（用于 platform_to_dict round-trip）。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "supported_executors": list(self.supported_executors),
            "capabilities": self.capabilities.to_dict(),
            "register_engines": [
                {
                    "id": e.id,
                    "display_name": e.display_name,
                    "description": e.description,
                    "is_recommended": e.is_recommended,
                    "deprecated": e.deprecated,
                    "supported_executors": e.supported_executors,
                }
                for e in self.get_register_engines()
            ],
            "preferred_captcha_strategies": list(self.preferred_captcha_strategies),
            "supported_exporters": list(self.supported_exporters),
            "default_extra_schema": dict(self.default_extra_schema),
        }

    # ── 工具方法 ──

    def _prepare_password(self, password: Optional[str]) -> str:
        """准备密码：有则用，无则随机生成。"""
        if password:
            return password
        return self._make_random_password()

    @staticmethod
    def _make_random_password(length: int = 16) -> str:
        chars = string.ascii_letters + string.digits + "!@#$"
        return "".join(random.choices(chars, k=length))

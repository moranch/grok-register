"""
通用插件注册表 + @register 装饰器 + load_all 自动发现。

设计要点（对应 Requirement 1 AC1/AC2）：
- Registry[T] 是泛型容器，按 name 索引插件实例/类。
- @register 装饰器把类注册到指定 Registry。
- load_all(pkg) 递归导入目录下所有模块，触发装饰器执行。
- 加载失败不阻塞应用启动，仅打印错误日志。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable, Dict, Generic, List, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Registry(Generic[T]):
    """泛型插件注册表。"""

    def __init__(self, name: str, required_fields: Optional[List[str]] = None):
        """
        Args:
            name: 注册表名称（用于日志）。
            required_fields: 注册时要求插件类必须声明的类变量名列表。
        """
        self._name = name
        self._required_fields = required_fields or []
        self._items: Dict[str, Type[T]] = {}

    @property
    def name(self) -> str:
        return self._name

    def register(self, cls: Type[T]) -> Type[T]:
        """
        装饰器：将 cls 注册到本注册表。

        如果 cls 缺少 required_fields 中的任何字段，拒绝注册并打印错误。
        """
        plugin_name = getattr(cls, "name", None) or ""
        if not plugin_name:
            logger.error(
                "[%s] 拒绝加载 %s：缺少 'name' 字段",
                self._name,
                cls.__qualname__,
            )
            return cls

        missing = [
            f for f in self._required_fields if not getattr(cls, f, None)
        ]
        if missing:
            logger.error(
                "[%s] 拒绝加载 '%s'（%s）：缺少必填字段 %s",
                self._name,
                plugin_name,
                cls.__qualname__,
                missing,
            )
            return cls

        if plugin_name in self._items:
            logger.warning(
                "[%s] 平台 '%s' 已注册（%s），将被 %s 覆盖",
                self._name,
                plugin_name,
                self._items[plugin_name].__qualname__,
                cls.__qualname__,
            )

        self._items[plugin_name] = cls
        logger.info("[%s] 已注册: %s (%s)", self._name, plugin_name, cls.__qualname__)
        return cls

    def get(self, name: str) -> Optional[Type[T]]:
        """按 name 获取已注册的类。"""
        return self._items.get(name)

    def get_or_raise(self, name: str) -> Type[T]:
        """按 name 获取，不存在时抛 KeyError。"""
        cls = self._items.get(name)
        if cls is None:
            raise KeyError(f"[{self._name}] 未找到: '{name}'")
        return cls

    def list_all(self) -> List[Type[T]]:
        """返回所有已注册的类列表。"""
        return list(self._items.values())

    def list_names(self) -> List[str]:
        """返回所有已注册的 name 列表。"""
        return list(self._items.keys())

    def exists(self, name: str) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __repr__(self) -> str:
        return f"<Registry '{self._name}' [{len(self._items)} items]>"


def load_all(package: Any) -> None:
    """
    递归导入 package 下所有子模块，触发其中的 @register 装饰器。

    加载失败不阻塞应用启动，仅打印错误日志。
    """
    if isinstance(package, str):
        package = importlib.import_module(package)

    pkg_path = getattr(package, "__path__", None)
    if pkg_path is None:
        logger.warning("load_all: %s 不是一个包（无 __path__）", package)
        return

    pkg_name = package.__name__

    for importer, module_name, is_pkg in pkgutil.walk_packages(
        pkg_path, prefix=f"{pkg_name}."
    ):
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            logger.error(
                "[load_all] 加载模块 '%s' 失败: %s", module_name, exc
            )


# ─── 全局注册表实例 ───────────────────────────────────────────────────────────

PLATFORM_REGISTRY: Registry = Registry(
    "Platform",
    required_fields=["name", "display_name", "supported_executors"],
)

MAILBOX_REGISTRY: Registry = Registry(
    "Mailbox",
    required_fields=["name"],
)

CAPTCHA_REGISTRY: Registry = Registry(
    "Captcha",
    required_fields=["name"],
)

STRATEGY_REGISTRY: Registry = Registry(
    "CaptchaStrategy",
    required_fields=["name"],
)

EXPORTER_REGISTRY: Registry = Registry(
    "Exporter",
    required_fields=["name"],
)


# ─── 便捷装饰器 ──────────────────────────────────────────────────────────────

def register_platform(cls: Type[T]) -> Type[T]:
    """将平台插件类注册到 PLATFORM_REGISTRY。"""
    return PLATFORM_REGISTRY.register(cls)


def register_mailbox(cls: Type[T]) -> Type[T]:
    """将邮箱 Provider 类注册到 MAILBOX_REGISTRY。"""
    return MAILBOX_REGISTRY.register(cls)


def register_captcha(cls: Type[T]) -> Type[T]:
    """将验证码 Provider 类注册到 CAPTCHA_REGISTRY。"""
    return CAPTCHA_REGISTRY.register(cls)


def register_strategy(cls: Type[T]) -> Type[T]:
    """将验证码策略类注册到 STRATEGY_REGISTRY。"""
    return STRATEGY_REGISTRY.register(cls)


def register_exporter(cls: Type[T]) -> Type[T]:
    """将导出器类注册到 EXPORTER_REGISTRY。"""
    return EXPORTER_REGISTRY.register(cls)

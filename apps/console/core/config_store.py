"""
配置存储：settings CRUD + platform_to_dict/from_dict + export_config/import_config。

对应 Requirement 9 AC1/AC4, Requirement 15 AC1-AC6。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.base_platform import BasePlatform, Capabilities, EngineSpec

logger = logging.getLogger(__name__)


# ─── ImportReport ────────────────────────────────────────────────────────────


@dataclass
class ImportReport:
    """配置导入报告。"""
    success: bool = True
    upserted: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "upserted": self.upserted,
            "warnings": self.warnings,
            "errors": self.errors,
        }


# ─── platform_to_dict / platform_from_dict（Requirement 15 AC1/AC3/AC6）──────


def platform_to_dict(plugin: BasePlatform) -> Dict[str, Any]:
    """
    序列化 Platform_Plugin 元数据。

    round-trip 保证：platform_from_dict(platform_to_dict(p)) == p 的元数据部分。
    """
    return {
        "name": plugin.name,
        "display_name": plugin.display_name,
        "version": plugin.version,
        "supported_executors": list(plugin.supported_executors),
        "capabilities": plugin.capabilities.to_dict() if hasattr(plugin.capabilities, "to_dict") else {},
        "register_engines": [
            {
                "id": e.id,
                "display_name": e.display_name,
                "description": e.description,
                "is_recommended": e.is_recommended,
                "deprecated": e.deprecated,
                "supported_executors": e.supported_executors,
            }
            for e in plugin.get_register_engines()
        ],
        "preferred_captcha_strategies": list(plugin.preferred_captcha_strategies),
        "supported_exporters": list(plugin.supported_exporters),
        "default_extra_schema": dict(plugin.default_extra_schema),
    }


def platform_from_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    反序列化 Platform_Plugin 元数据。

    返回一个字典，包含所有可还原的字段。
    用于 round-trip 验证和配置导入。
    """
    engines_raw = data.get("register_engines", [])
    engines = []
    for e in engines_raw:
        if isinstance(e, dict):
            engines.append(EngineSpec(
                id=e.get("id", "default"),
                display_name=e.get("display_name", ""),
                description=e.get("description", ""),
                is_recommended=e.get("is_recommended", False),
                deprecated=e.get("deprecated", False),
                supported_executors=e.get("supported_executors"),
            ))

    caps_raw = data.get("capabilities", {})
    capabilities = Capabilities.from_dict(caps_raw) if isinstance(caps_raw, dict) else Capabilities()

    return {
        "name": data.get("name", ""),
        "display_name": data.get("display_name", ""),
        "version": data.get("version", "1.0.0"),
        "supported_executors": list(data.get("supported_executors", [])),
        "capabilities": capabilities.to_dict(),
        "register_engines": [
            {
                "id": e.id,
                "display_name": e.display_name,
                "description": e.description,
                "is_recommended": e.is_recommended,
                "deprecated": e.deprecated,
                "supported_executors": e.supported_executors,
            }
            for e in engines
        ],
        "preferred_captcha_strategies": list(data.get("preferred_captcha_strategies", [])),
        "supported_exporters": list(data.get("supported_exporters", ["any2api"])),
        "default_extra_schema": dict(data.get("default_extra_schema", {})),
    }


# ─── ConfigStore ─────────────────────────────────────────────────────────────


# 已知的配置 key 前缀
KNOWN_PREFIXES = {
    "system", "platform_", "exporter_", "lifecycle_state",
    "captcha_", "mailbox_", "proxy_",
}


class ConfigStore:
    """
    配置存储管理器。

    底层使用 settings 表（key-value 形式），本类提供内存缓存 + CRUD。
    实际持久化由 DAO 层完成，本类只做逻辑。
    """

    def __init__(self):
        self._cache: Dict[str, Any] = {}

    def load(self, data: Dict[str, Any]) -> None:
        """从数据库加载全量配置到内存。"""
        self._cache = dict(data)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值。"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置配置值（内存）。"""
        self._cache[key] = value

    def get_all(self) -> Dict[str, Any]:
        """获取全量配置。"""
        return dict(self._cache)

    def get_platform_config(self, platform_name: str) -> Dict[str, Any]:
        """获取平台专属配置。"""
        raw = self._cache.get(f"platform_{platform_name}", "{}")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw if isinstance(raw, dict) else {}

    def set_platform_config(self, platform_name: str, config: Dict[str, Any]) -> None:
        """设置平台专属配置。"""
        self._cache[f"platform_{platform_name}"] = json.dumps(config, ensure_ascii=False)

    def get_exporter_config(self, exporter_id: str) -> Dict[str, Any]:
        """获取 Exporter 专属配置。"""
        raw = self._cache.get(f"exporter_{exporter_id}", "{}")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return raw if isinstance(raw, dict) else {}

    def set_exporter_config(self, exporter_id: str, config: Dict[str, Any]) -> None:
        """设置 Exporter 专属配置。"""
        self._cache[f"exporter_{exporter_id}"] = json.dumps(config, ensure_ascii=False)


# ─── export_config / import_config（Requirement 15 AC2/AC4/AC5）──────────────


def export_config(config_store: ConfigStore) -> Dict[str, Any]:
    """
    导出全量配置为 JSON 字典。

    包含 settings + platform configs + exporter configs。
    """
    return {
        "version": "2.0.0",
        "settings": config_store.get_all(),
    }


def import_config(data: Dict[str, Any], config_store: ConfigStore) -> ImportReport:
    """
    导入配置（upsert 模式）。

    - 已知字段正常导入。
    - 未知字段忽略并记录到 warnings（Req 15 AC5）。
    - 不因未知字段拒绝整次导入。
    """
    report = ImportReport()

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        report.errors.append("settings 字段不是字典")
        report.success = False
        return report

    for key, value in settings.items():
        # 检查是否为已知 key
        is_known = any(key.startswith(prefix) for prefix in KNOWN_PREFIXES)
        if not is_known and key not in ("system",):
            report.warnings.append(f"未知配置 key: '{key}'，已忽略")
            continue

        config_store.set(key, value)
        report.upserted.append(key)

    return report

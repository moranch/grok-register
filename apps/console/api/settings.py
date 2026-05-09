"""
全局配置 + 系统信息路由。

对应 Requirement 9 AC1-AC4：全局配置管理。
对应 Requirement 15 AC1-AC6：配置导入导出。

settings_router:
- GET / — 获取全局配置
- POST / — 更新全局配置

system_router:
- GET /info — 系统信息
- GET /export-config — 导出全量配置
- POST /import-config — 导入配置
"""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel

from core.config_store import ConfigStore, export_config, import_config
from core.registry import (
    PLATFORM_REGISTRY,
    MAILBOX_REGISTRY,
    CAPTCHA_REGISTRY,
    STRATEGY_REGISTRY,
    EXPORTER_REGISTRY,
)
from data import dao

router = APIRouter(prefix="/settings", tags=["settings"])
system_router = APIRouter(prefix="/system", tags=["system"])

# 全局配置存储实例
_config_store = ConfigStore()

APP_VERSION = "2.0.0"


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


class SettingsUpdateRequest(BaseModel):
    """更新全局配置请求。"""
    settings: Dict[str, Any]


class ImportConfigRequest(BaseModel):
    """导入配置请求。"""
    data: Dict[str, Any]


# ─── Settings 路由 ────────────────────────────────────────────────────────────


@router.get("/")
async def get_settings():
    """获取全局配置。"""
    settings = dao.get_all_settings()
    return {"settings": settings}


@router.post("/")
async def update_settings(body: SettingsUpdateRequest):
    """更新全局配置（upsert 模式）。"""
    updated_keys = []
    for key, value in body.settings.items():
        str_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        dao.upsert_setting(key, str_value)
        updated_keys.append(key)

    return {"ok": True, "updated": updated_keys}


# ─── System 路由 ──────────────────────────────────────────────────────────────


@system_router.get("/info")
async def get_system_info():
    """获取系统信息。"""
    db_path = os.getenv("DATABASE_PATH", "data/console.db")
    db_size = 0
    if Path(db_path).exists():
        db_size = Path(db_path).stat().st_size

    # 表记录数统计
    table_counts = {}
    try:
        from sqlmodel import select, func
        from data.models import (
            TaskModel, AccountModel, RegisterEventModel,
            ProxyModel, MailboxProviderModel, SettingModel,
        )
        with dao.get_session() as s:
            table_counts["tasks"] = s.exec(select(func.count()).select_from(TaskModel)).one()
            table_counts["accounts"] = s.exec(select(func.count()).select_from(AccountModel)).one()
            table_counts["events"] = s.exec(select(func.count()).select_from(RegisterEventModel)).one()
            table_counts["proxies"] = s.exec(select(func.count()).select_from(ProxyModel)).one()
            table_counts["mailbox_providers"] = s.exec(select(func.count()).select_from(MailboxProviderModel)).one()
            table_counts["settings"] = s.exec(select(func.count()).select_from(SettingModel)).one()
    except Exception:
        pass

    # 已加载插件
    loaded_plugins = {
        "platforms": PLATFORM_REGISTRY.list_names(),
        "mailbox_providers": MAILBOX_REGISTRY.list_names(),
        "captcha_providers": CAPTCHA_REGISTRY.list_names(),
        "captcha_strategies": STRATEGY_REGISTRY.list_names(),
        "exporters": EXPORTER_REGISTRY.list_names(),
    }

    return {
        "app_version": APP_VERSION,
        "python_version": sys.version,
        "platform": platform.platform(),
        "db_size": db_size,
        "db_size_human": _format_size(db_size),
        "table_counts": table_counts,
        "loaded_plugins": loaded_plugins,
        "upstream_credits": {
            "framework": "FastAPI",
            "orm": "SQLModel",
            "database": "SQLite",
        },
    }


@system_router.get("/export-config")
async def export_system_config():
    """导出全量配置。"""
    settings = dao.get_all_settings()
    _config_store.load(settings)
    return export_config(_config_store)


@system_router.post("/import-config")
async def import_system_config(body: ImportConfigRequest):
    """导入配置（upsert 模式）。"""
    # 加载当前配置到内存
    settings = dao.get_all_settings()
    _config_store.load(settings)

    # 执行导入
    report = import_config(body.data, _config_store)

    # 持久化到数据库
    if report.success:
        for key in report.upserted:
            value = _config_store.get(key, "")
            str_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            dao.upsert_setting(key, str_value)

    return report.to_dict()


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


def _format_size(size_bytes: int) -> str:
    """格式化文件大小。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

"""
平台管理路由。

对应 Requirement 1 AC1-AC10：多平台插件体系。
- GET / — 返回所有已加载平台的元信息列表
- GET /{name} — 返回单个平台详情（含 register_engines）
- PATCH /{name}/config — 更新平台专属配置
- PATCH /{name}/enabled — 启用/禁用平台
- POST /{name}/test-run — 以 count=1 发起试跑任务
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel

from core.registry import PLATFORM_REGISTRY
from core.config_store import ConfigStore, platform_to_dict

from ._shared import execute_no_return, fetch_one, now_iso

router = APIRouter(prefix="/platforms", tags=["platforms"])

# 全局配置存储实例（由 main_app lifespan 初始化后可用）
_config_store = ConfigStore()


# ─── 响应模型 ─────────────────────────────────────────────────────────────────


class PlatformSummary(BaseModel):
    """平台摘要信息。"""
    name: str
    display_name: str
    version: str
    supported_executors: List[str]
    engine_count: int
    enabled: bool
    register_engines: List[Dict[str, Any]] = []


class PlatformConfigUpdate(BaseModel):
    """平台配置更新请求。"""
    config: Dict[str, Any]


class PlatformEnabledUpdate(BaseModel):
    """平台启用/禁用请求。"""
    enabled: bool


class TestRunRequest(BaseModel):
    """试跑请求。"""
    executor_type: str = "protocol"
    engine_id: str = "default"


# ─── 辅助：settings 表读写（用 _shared 原生 sqlite，避免与 SQLModel DAO 的 schema 冲突） ─


def _get_setting(key: str) -> Optional[str]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


def _set_setting(key: str, value: str) -> None:
    execute_no_return(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                       updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def _is_platform_enabled(name: str) -> bool:
    """读取 settings 中平台启用状态；未设置视为启用。"""
    raw = _get_setting(f"platform_{name}_enabled")
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_platform_config(name: str) -> Dict[str, Any]:
    raw = _get_setting(f"platform_{name}")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/", response_model=List[PlatformSummary])
async def list_platforms():
    """返回所有已加载平台的元信息列表。"""
    results = []
    for cls in PLATFORM_REGISTRY.list_all():
        instance = cls()
        results.append(PlatformSummary(
            name=instance.name,
            display_name=instance.display_name,
            version=instance.version,
            supported_executors=list(instance.supported_executors),
            engine_count=len(instance.get_register_engines()),
            enabled=_is_platform_enabled(instance.name),
            register_engines=[
                {
                    "id": e.id,
                    "display_name": e.display_name,
                    "description": getattr(e, "description", ""),
                    "is_recommended": getattr(e, "is_recommended", False),
                    "is_deprecated": getattr(e, "is_deprecated", False),
                }
                for e in instance.get_register_engines()
            ],
        ))
    return results


@router.get("/{name}")
async def get_platform(name: str):
    """返回单个平台详情（含 register_engines、capabilities 等）。"""
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")

    instance = cls()
    detail = platform_to_dict(instance)
    detail["config"] = _get_platform_config(name)
    detail["enabled"] = _is_platform_enabled(name)
    return detail


@router.patch("/{name}/enabled")
async def update_platform_enabled(name: str, body: PlatformEnabledUpdate):
    """启用或禁用平台。禁用的平台仍在 registry 中，但不会出现在任务创建的可选项里。"""
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")
    _set_setting(
        f"platform_{name}_enabled",
        "true" if body.enabled else "false",
    )
    return {"ok": True, "platform": name, "enabled": body.enabled}


@router.patch("/{name}/config")
async def update_platform_config(name: str, body: PlatformConfigUpdate):
    """更新平台专属配置。"""
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")
    _set_setting(
        f"platform_{name}",
        json.dumps(body.config, ensure_ascii=False),
    )
    return {"ok": True, "platform": name, "config": body.config}


@router.post("/{name}/test-run")
async def test_run(name: str, body: TestRunRequest):
    """以 count=1 发起试跑任务。

    注：多平台任务执行链路（spec task 4.4 + core/task_runtime 接插件 dispatch）
    尚未完成，此端点暂时返回 501。完成后会切换到正式的 supervisor 流程。
    """
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")

    instance = cls()

    # 校验 executor_type
    if body.executor_type not in instance.supported_executors:
        raise HTTPException(
            status_code=400,
            detail=f"平台 '{name}' 不支持执行器 '{body.executor_type}'，可用: {instance.supported_executors}",
        )

    # 校验 engine_id
    try:
        instance.get_engine(body.engine_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raise HTTPException(
        status_code=501,
        detail=(
            "平台试跑尚未接入任务执行链路 "
            "(tasks 表 schema 与多平台字段尚未合并, 参见 spec task 2.1/4.4)。"
        ),
    )

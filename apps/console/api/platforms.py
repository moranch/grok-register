"""
平台管理路由。

对应 Requirement 1 AC1-AC10：多平台插件体系。
- GET / — 返回所有已加载平台的元信息列表
- GET /{name} — 返回单个平台详情（含 register_engines）
- PATCH /{name}/config — 更新平台专属配置
- POST /{name}/test-run — 以 count=1 发起试跑任务
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel

from core.registry import PLATFORM_REGISTRY
from core.config_store import ConfigStore, platform_to_dict
from data import dao

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


# ─── 辅助 ─────────────────────────────────────────────────────────────────────


def _is_platform_enabled(name: str) -> bool:
    """读取 settings 中平台启用状态；未设置视为启用（只要被 registry 发现就默认打开）。"""
    raw = dao.get_all_settings().get(f"platform_{name}_enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


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

    # 附加平台专属配置
    settings = dao.get_all_settings()
    platform_config_raw = settings.get(f"platform_{name}", "{}")
    try:
        platform_config = json.loads(platform_config_raw) if isinstance(platform_config_raw, str) else platform_config_raw
    except (json.JSONDecodeError, TypeError):
        platform_config = {}

    detail["config"] = platform_config
    detail["enabled"] = _is_platform_enabled(name)
    return detail


@router.patch("/{name}/enabled")
async def update_platform_enabled(name: str, body: PlatformEnabledUpdate):
    """启用或禁用平台。禁用的平台仍在 registry 中，但不会出现在任务创建的可选项里。"""
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")
    dao.upsert_setting(f"platform_{name}_enabled", "true" if body.enabled else "false")
    return {"ok": True, "platform": name, "enabled": body.enabled}


@router.patch("/{name}/config")
async def update_platform_config(name: str, body: PlatformConfigUpdate):
    """更新平台专属配置。"""
    cls = PLATFORM_REGISTRY.get(name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"平台 '{name}' 未找到")

    config_json = json.dumps(body.config, ensure_ascii=False)
    dao.upsert_setting(f"platform_{name}", config_json)

    return {"ok": True, "platform": name, "config": body.config}


@router.post("/{name}/test-run")
async def test_run(name: str, body: TestRunRequest):
    """以 count=1 发起试跑任务。"""
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

    # 创建试跑任务
    task = dao.save_task({
        "name": f"[试跑] {instance.display_name}",
        "platform": name,
        "status": "queued",
        "executor_type": body.executor_type,
        "target_count": 1,
        "params_json": json.dumps({"engine_id": body.engine_id, "is_test_run": True}, ensure_ascii=False),
    })

    return {"ok": True, "task_id": task.id, "message": f"试跑任务已创建 (ID={task.id})"}

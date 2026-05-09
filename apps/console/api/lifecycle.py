"""
生命周期路由。

对应 Requirement 8 AC1-AC6：账号生命周期管理。
- GET /status — Worker 状态
- POST /check — 立即触发检测
- POST /toggle — 启用/禁用 Worker
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/lifecycle", tags=["lifecycle"])


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


class ToggleRequest(BaseModel):
    """启用/禁用 Worker 请求。"""
    enabled: bool


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/status")
async def get_status():
    """获取 LifecycleWorker 状态。"""
    from api.main_app import lifecycle_worker

    if lifecycle_worker is None:
        return {
            "enabled": False,
            "running": False,
            "message": "Worker 未初始化",
        }

    state = lifecycle_worker.state
    return {
        "enabled": state.enabled,
        "running": state.running,
        "check_hours": state.check_hours,
        "refresh_hours": state.refresh_hours,
        "last_check_at": state.last_check_at,
        "last_refresh_at": state.last_refresh_at,
        "last_result": state.last_result,
        "last_error": state.last_error,
        "checked_count": state.checked_count,
        "refreshed_count": state.refreshed_count,
        "warned_count": state.warned_count,
    }


@router.post("/check")
async def trigger_check():
    """立即触发一次检测（无视周期）。"""
    from api.main_app import lifecycle_worker

    if lifecycle_worker is None:
        raise HTTPException(status_code=503, detail="Worker 未初始化")

    if lifecycle_worker.state.running:
        raise HTTPException(status_code=409, detail="Worker 正在执行检测中，请稍后再试")

    result = await lifecycle_worker.trigger_check_now()
    return {"ok": True, "result": result}


@router.post("/toggle")
async def toggle_worker(body: ToggleRequest):
    """启用/禁用 LifecycleWorker。"""
    from api.main_app import lifecycle_worker

    if lifecycle_worker is None:
        raise HTTPException(status_code=503, detail="Worker 未初始化")

    lifecycle_worker.state.enabled = body.enabled
    return {
        "ok": True,
        "enabled": lifecycle_worker.state.enabled,
        "message": f"Worker 已{'启用' if body.enabled else '禁用'}",
    }

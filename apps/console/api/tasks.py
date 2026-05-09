"""
注册任务路由。

对应 Requirement 2 AC1-AC9：注册任务管理。
- POST /register — 创建注册任务
- GET / — 任务列表
- GET /{task_id} — 任务详情
- GET /{task_id}/stream — SSE 实时日志流
- POST /{task_id}/stop — 停止任务
- POST /{task_id}/skip-current — 跳过当前轮
- DELETE /{task_id} — 删除任务
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.registry import PLATFORM_REGISTRY
from core.task_runtime import TaskRuntime, TaskStatus
from data import dao

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 全局 TaskRuntime 实例
task_runtime = TaskRuntime(max_concurrent_tasks=3)


# ─── 请求/响应模型 ────────────────────────────────────────────────────────────


class RegisterTaskRequest(BaseModel):
    """创建注册任务请求。"""
    platform: str
    count: int = Field(ge=1, le=100, default=1)
    executor_type: str = "protocol"
    engine_id: str = "default"
    name: str = ""
    config: Dict[str, Any] = {}
    params: Dict[str, Any] = {}


class TaskResponse(BaseModel):
    """任务响应。"""
    id: int
    name: str
    platform: str
    status: str
    executor_type: str
    target_count: int
    completed_count: int
    success_count: int
    failure_count: int
    skipped_count: int
    last_error: str
    created_at: str
    updated_at: str


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.post("/register")
async def create_register_task(body: RegisterTaskRequest):
    """创建注册任务。"""
    # 校验平台
    cls = PLATFORM_REGISTRY.get(body.platform)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"平台 '{body.platform}' 未注册")

    instance = cls()

    # 校验 executor_type
    if body.executor_type not in instance.supported_executors:
        raise HTTPException(
            status_code=400,
            detail=f"平台 '{body.platform}' 不支持执行器 '{body.executor_type}'，可用: {instance.supported_executors}",
        )

    # 校验 engine_id
    try:
        instance.get_engine(body.engine_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 生成任务名称
    task_name = body.name or f"{instance.display_name} x{body.count}"

    # 保存到数据库
    task = dao.save_task({
        "name": task_name,
        "platform": body.platform,
        "status": "queued",
        "executor_type": body.executor_type,
        "target_count": body.count,
        "config_json": json.dumps(body.config, ensure_ascii=False),
        "params_json": json.dumps({
            "engine_id": body.engine_id,
            **body.params,
        }, ensure_ascii=False),
    })

    # 在 TaskRuntime 中创建运行时状态
    task_runtime.create_task(
        task_id=str(task.id),
        platform=body.platform,
        engine_id=body.engine_id,
        executor_type=body.executor_type,
        target_count=body.count,
    )

    return {"ok": True, "task_id": task.id, "name": task_name}


@router.get("/")
async def list_tasks(
    platform: str = Query(default="", description="按平台过滤"),
    status: str = Query(default="", description="按状态过滤"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """获取任务列表。"""
    tasks = dao.list_tasks(platform=platform, status=status, limit=limit)
    return [
        {
            "id": t.id,
            "name": t.name,
            "platform": t.platform,
            "status": t.status,
            "executor_type": t.executor_type,
            "target_count": t.target_count,
            "completed_count": t.completed_count,
            "success_count": t.success_count,
            "failure_count": t.failure_count,
            "skipped_count": t.skipped_count,
            "last_error": t.last_error,
            "created_at": t.created_at,
            "updated_at": t.updated_at,
        }
        for t in tasks
    ]


@router.get("/{task_id}")
async def get_task(task_id: int):
    """获取任务详情。"""
    tasks = dao.list_tasks(limit=9999)
    task = next((t for t in tasks if t.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    # 合并运行时状态
    runtime_state = task_runtime.get_state(str(task_id))
    runtime_info = {}
    if runtime_state:
        runtime_info = {
            "runtime_status": runtime_state.status.value,
            "runtime_success": runtime_state.success_count,
            "runtime_failure": runtime_state.failure_count,
        }

    return {
        "id": task.id,
        "name": task.name,
        "platform": task.platform,
        "status": task.status,
        "executor_type": task.executor_type,
        "target_count": task.target_count,
        "completed_count": task.completed_count,
        "success_count": task.success_count,
        "failure_count": task.failure_count,
        "skipped_count": task.skipped_count,
        "last_error": task.last_error,
        "config_json": task.config_json,
        "params_json": task.params_json,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        **runtime_info,
    }


@router.get("/{task_id}/stream")
async def task_stream(task_id: int, since: int = Query(default=0, ge=0)):
    """SSE 实时日志流。"""
    event_bus = task_runtime.get_event_bus(str(task_id))
    if event_bus is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 无活跃事件流")

    async def event_generator():
        async for event in event_bus.subscribe(since=since):
            yield event.to_sse_string()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{task_id}/stop")
async def stop_task(task_id: int):
    """停止任务。"""
    success = task_runtime.request_stop(str(task_id))
    if not success:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或无法停止")

    dao.update_task(task_id, {"status": "stopping"})
    return {"ok": True, "task_id": task_id, "message": "已发送停止信号"}


@router.post("/{task_id}/skip-current")
async def skip_current(task_id: int):
    """跳过当前轮。"""
    success = task_runtime.request_skip(str(task_id))
    if not success:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或无法跳过")

    return {"ok": True, "task_id": task_id, "message": "已发送跳过信号"}


@router.delete("/{task_id}")
async def delete_task(task_id: int):
    """删除任务。"""
    # 检查任务是否在运行中
    runtime_state = task_runtime.get_state(str(task_id))
    if runtime_state and runtime_state.status in (TaskStatus.RUNNING, TaskStatus.STOPPING):
        raise HTTPException(status_code=400, detail="无法删除运行中的任务，请先停止")

    result = dao.update_task(task_id, {"status": "deleted"})
    if result is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    return {"ok": True, "task_id": task_id, "message": "任务已删除"}

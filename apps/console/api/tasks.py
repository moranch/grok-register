"""
注册任务路由（覆盖旧实现，等价于 app.py 原 `/api/tasks/*` 端点）。

- GET    /api/tasks               列表
- POST   /api/tasks                创建
- GET    /api/tasks/{id}           详情
- GET    /api/tasks/{id}/logs      轮询日志
- POST   /api/tasks/{id}/stop      停止
- DELETE /api/tasks/{id}           删除（未运行才允许）
- GET    /api/tasks/{id}/stream    SSE 实时日志
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ._shared import (
    SOURCE_PROJECT,
    SOURCE_VENV_PYTHON,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_QUEUED,
    STATUS_STOPPED,
    TASKS_DIR,
    TaskCreate,
    build_task_config,
    check_auth,
    delete_task_files,
    execute,
    execute_no_return,
    fetch_all,
    fetch_one,
    now_iso,
    read_log_lines,
    serialize_task,
    task_row,
)
from ._supervisor import supervisor

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(request: Request) -> dict[str, Any]:
    check_auth(request)
    rows = fetch_all("SELECT * FROM tasks ORDER BY id DESC")
    return {"tasks": [serialize_task(row) for row in rows]}


@router.post("")
def create_task(request: Request, payload: TaskCreate) -> dict[str, Any]:
    check_auth(request)

    # 平台校验：默认 grok；非 grok 必须在 PLATFORM_REGISTRY 中
    platform_name = (payload.platform or "grok").strip().lower()
    engine_id = (payload.engine_id or "").strip()
    if platform_name != "grok":
        try:
            from core.registry import PLATFORM_REGISTRY
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"多平台 registry 不可用: {exc}",
            )
        cls = PLATFORM_REGISTRY.get(platform_name)
        if cls is None:
            raise HTTPException(
                status_code=400,
                detail=f"未知平台: {platform_name}",
            )
        instance = cls()
        engines = {e.id for e in instance.get_register_engines()}
        if not engine_id:
            raise HTTPException(
                status_code=400,
                detail=f"平台 '{platform_name}' 必须指定 engine_id，可选: {sorted(engines)}",
            )
        if engine_id not in engines:
            raise HTTPException(
                status_code=400,
                detail=f"平台 '{platform_name}' 不支持 engine_id '{engine_id}'，可选: {sorted(engines)}",
            )

    # grok 保持原有前置检查；其它平台 supervisor 会在分派阶段报错
    if platform_name == "grok":
        if not SOURCE_PROJECT.exists():
            raise HTTPException(status_code=500, detail=f"Source project not found: {SOURCE_PROJECT}")
        if not SOURCE_VENV_PYTHON.exists():
            raise HTTPException(status_code=500, detail=f"Python not found: {SOURCE_VENV_PYTHON}")

    task_config = build_task_config(payload)
    executor_type = str(task_config.get("executor", "") or payload.executor or "")
    params = {
        "platform": platform_name,
        "engine_id": engine_id,
        "extra": payload.extra or {},
    }
    created_at = now_iso()
    task_id = execute(
        """
        INSERT INTO tasks (
            name, status, target_count, notes, config_json, task_dir, console_path, created_at,
            platform, executor_type, engine_id, params_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            STATUS_QUEUED,
            payload.count,
            payload.notes.strip(),
            json.dumps(task_config, ensure_ascii=False),
            str(TASKS_DIR / "pending"),
            str(TASKS_DIR / "pending.log"),
            created_at,
            platform_name,
            executor_type,
            engine_id,
            json.dumps(params, ensure_ascii=False),
        ),
    )
    task_dir = TASKS_DIR / f"task_{task_id}"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True, exist_ok=True)
    execute_no_return(
        "UPDATE tasks SET task_dir = ?, console_path = ? WHERE id = ?",
        (str(task_dir), str(console_path), task_id),
    )
    return {"task": serialize_task(task_row(task_id))}


@router.get("/{task_id}")
def get_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    return {"task": serialize_task(task_row(task_id))}


@router.get("/{task_id}/logs")
def get_task_logs(
    request: Request,
    task_id: int,
    limit: int = Query(200, ge=20, le=1000),
) -> dict[str, Any]:
    check_auth(request)
    row = task_row(task_id)
    console_path = Path(row["console_path"])
    return {"lines": read_log_lines(console_path, limit=limit)}


@router.post("/{task_id}/stop")
def stop_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    supervisor.stop_task(task_id)
    return {"ok": True}


@router.delete("/{task_id}")
def delete_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    row = task_row(task_id)
    managed = supervisor._processes.get(task_id)
    if managed and managed.process.poll() is None:
        raise HTTPException(status_code=409, detail="Task is still running")
    delete_task_files(row)
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


@router.get("/{task_id}/stream")
def api_task_stream(request: Request, task_id: int):
    """SSE 实时推送 task console 日志。与 /logs 轮询接口并存。"""
    check_auth(request)
    row = task_row(task_id)
    console_path = Path(row["console_path"])

    def _event_gen():
        last_size = 0
        if console_path.exists():
            initial = read_log_lines(console_path, limit=200)
            for line in initial:
                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
            try:
                last_size = console_path.stat().st_size
            except Exception:
                last_size = 0
        last_ping = time.time()
        while True:
            try:
                if console_path.exists():
                    size = console_path.stat().st_size
                    if size > last_size:
                        with console_path.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            chunk = f.read()
                        last_size = size
                        for line in chunk.splitlines():
                            yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    elif size < last_size:
                        last_size = 0
                if time.time() - last_ping >= 15:
                    yield ": ping\n\n"
                    last_ping = time.time()
                time.sleep(1.0)
                status_row = fetch_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                if status_row and status_row["status"] in {
                    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_STOPPED,
                }:
                    time.sleep(1.5)
                    if console_path.exists():
                        size = console_path.stat().st_size
                        if size > last_size:
                            with console_path.open("r", encoding="utf-8", errors="replace") as f:
                                f.seek(last_size)
                                chunk = f.read()
                            for line in chunk.splitlines():
                                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'status': status_row['status']})}\n\n"
                    return
            except GeneratorExit:
                return
            except Exception:
                time.sleep(2.0)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )

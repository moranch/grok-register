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

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ._shared import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_STOPPED,
    TaskCreate,
    check_auth,
    delete_task_files,
    execute_no_return,
    fetch_all,
    fetch_one,
    read_log_lines,
    serialize_task,
    task_row,
)
from ._supervisor import supervisor
from ._task_runtime import enqueue_registration_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
def list_tasks(request: Request) -> dict[str, Any]:
    check_auth(request)
    rows = fetch_all("SELECT * FROM tasks ORDER BY id DESC")
    return {"tasks": [serialize_task(row) for row in rows]}


@router.post("")
def create_task(request: Request, payload: TaskCreate) -> dict[str, Any]:
    check_auth(request)
    return {"task": enqueue_registration_task(payload)}


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

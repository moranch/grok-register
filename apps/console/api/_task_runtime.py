"""Shared registration-task validation and queueing."""
from __future__ import annotations

import json

from fastapi import HTTPException

from ._shared import (
    SOURCE_PROJECT,
    SOURCE_VENV_PYTHON,
    STATUS_QUEUED,
    TASKS_DIR,
    TaskCreate,
    build_task_config,
    execute,
    execute_no_return,
    now_iso,
    serialize_task,
    task_row,
)


def enqueue_registration_task(payload: TaskCreate) -> dict:
    platform_name = (payload.platform or "grok").strip().lower()
    engine_id = (payload.engine_id or "").strip()

    if platform_name != "grok":
        try:
            from core.registry import PLATFORM_REGISTRY
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"多平台 registry 不可用: {exc}") from exc
        platform_class = PLATFORM_REGISTRY.get(platform_name)
        if platform_class is None:
            raise HTTPException(status_code=400, detail=f"未知平台: {platform_name}")
        engines = {engine.id for engine in platform_class().get_register_engines()}
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
            "initializing",
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
        "UPDATE tasks SET status = ?, task_dir = ?, console_path = ? WHERE id = ?",
        (STATUS_QUEUED, str(task_dir), str(console_path), task_id),
    )
    return serialize_task(task_row(task_id))

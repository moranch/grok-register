"""
生命周期路由（覆盖旧实现，等价于 app.py 原 `/api/lifecycle/*` 端点）。

- GET  /api/lifecycle/status
- POST /api/lifecycle/check
- POST /api/lifecycle/toggle    启用/禁用后台 worker
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ._shared import (
    SystemSettings,
    check_auth,
    execute_no_return,
    merged_defaults,
    now_iso,
    read_settings,
    write_settings,
)
from ._lifecycle_runtime import lifecycle_check_accounts, lifecycle_state


class LifecycleToggle(BaseModel):
    """启用/禁用生命周期 worker 的请求体。

    - enabled 缺省时按当前状态反转
    - check_hours 可选，用于同时调整巡检周期
    """

    enabled: bool | None = None
    check_hours: int | None = None


router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


@router.get("/status")
def api_lifecycle_status(request: Request) -> dict[str, Any]:
    check_auth(request)
    defaults = merged_defaults()
    return {
        "enabled": bool(defaults.get("lifecycle_enabled", False)),
        "check_hours": int(defaults.get("lifecycle_check_hours", 6) or 6),
        **lifecycle_state,
    }


@router.post("/check")
def api_lifecycle_check(request: Request) -> dict[str, Any]:
    """手动触发一次有效性检测。"""
    check_auth(request)
    result = lifecycle_check_accounts()
    lifecycle_state["last_check_at"] = now_iso()
    lifecycle_state["last_result"] = result.get("message", "")
    execute_no_return(
        "UPDATE accounts SET last_checked_at = ? WHERE status = 'active'",
        (now_iso(),),
    )
    return {**result, "checked_at": now_iso()}


@router.post("/toggle")
def api_lifecycle_toggle(request: Request, payload: LifecycleToggle | None = None) -> dict[str, Any]:
    """启用/禁用生命周期 worker（可选同时调整巡检周期）。

    仅改动 `lifecycle_enabled` / `lifecycle_check_hours` 两个字段；其余系统配置保持原值，
    避免 /api/settings 的整体覆盖语义把已配置项抹掉。
    """
    check_auth(request)

    payload = payload or LifecycleToggle()
    defaults = merged_defaults()

    # 构造完整 SystemSettings：先用当前已保存值铺底，再用 merged_defaults 兜底，
    # 最后只覆盖 lifecycle 两个字段。
    saved = read_settings()
    base: dict[str, Any] = {}
    for field, _info in SystemSettings.model_fields.items():
        if field in saved:
            base[field] = saved[field]
        elif field == "api_endpoint":
            base[field] = (defaults.get("api") or {}).get("endpoint", "") or ""
        elif field == "api_token":
            base[field] = (defaults.get("api") or {}).get("token", "") or ""
        elif field == "api_append":
            base[field] = bool((defaults.get("api") or {}).get("append", True))
        elif field in defaults:
            base[field] = defaults[field]

    current_enabled = bool(defaults.get("lifecycle_enabled", False))
    new_enabled = (not current_enabled) if payload.enabled is None else bool(payload.enabled)
    base["lifecycle_enabled"] = new_enabled

    if payload.check_hours is not None:
        base["lifecycle_check_hours"] = max(1, int(payload.check_hours))

    write_settings(SystemSettings(**base))

    # 禁用时清空 "running" 状态显示；后台线程 ≤60s 内通过 merged_defaults 感知变更
    if not new_enabled:
        lifecycle_state["running"] = False

    return {
        "enabled": new_enabled,
        "check_hours": int(base.get("lifecycle_check_hours", 6) or 6),
        "changed_at": now_iso(),
        **lifecycle_state,
    }

"""
全局配置路由（覆盖旧实现，等价于 app.py 原 `/api/settings`）。

- GET  /api/settings
- POST /api/settings
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ._shared import (
    SystemSettings,
    check_auth,
    merged_defaults,
    read_settings,
    write_settings,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"settings": read_settings(), "defaults": merged_defaults()}


@router.post("")
def save_settings(request: Request, payload: SystemSettings) -> dict[str, Any]:
    check_auth(request)
    saved = write_settings(payload)
    return {"settings": saved, "defaults": merged_defaults()}

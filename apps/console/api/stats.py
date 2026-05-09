"""
统计路由（覆盖旧实现，等价于 app.py 原 `/api/stats/*` 端点）。

- GET /api/stats/overview
- GET /api/stats/errors
- GET /api/stats/by-proxy
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ._shared import check_auth, stats_by_proxy, stats_errors, stats_overview

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview")
def api_stats_overview(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    check_auth(request)
    return stats_overview(days)


@router.get("/errors")
def api_stats_errors(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    check_auth(request)
    return {"items": stats_errors(days)}


@router.get("/by-proxy")
def api_stats_by_proxy(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"items": stats_by_proxy()}

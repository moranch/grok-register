"""
统计路由。

对应 Requirement 11 AC1-AC3：数据统计与分析。
- GET /overview — 全局概览
- GET /errors — 错误 Top N
- GET /by-proxy — 按代理统计
- GET /by-platform — 按平台统计
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Query
from sqlmodel import select, func

from data import dao
from data.models import RegisterEventModel, ProxyModel

router = APIRouter(prefix="/stats", tags=["stats"])


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/overview")
async def get_overview(days: int = Query(default=7, ge=1, le=365)):
    """全局统计概览。"""
    return dao.get_stats_overview(days=days)


@router.get("/errors")
async def get_errors(
    days: int = Query(default=7, ge=1, le=365),
    top_n: int = Query(default=10, ge=1, le=100),
):
    """错误 Top N 聚合。"""
    return dao.get_stats_errors(days=days, top_n=top_n)


@router.get("/by-proxy")
async def get_by_proxy():
    """按代理统计成功率。"""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    with dao.get_session() as s:
        rows = s.exec(
            select(
                RegisterEventModel.proxy_url,
                RegisterEventModel.kind,
                func.count(),
            )
            .where(RegisterEventModel.created_at >= cutoff)
            .where(RegisterEventModel.kind.in_(["success", "failure"]))
            .where(RegisterEventModel.proxy_url != "")
            .group_by(RegisterEventModel.proxy_url, RegisterEventModel.kind)
        ).all()

    # 聚合
    proxies: Dict[str, Dict[str, int]] = {}
    for proxy_url, kind, count in rows:
        if proxy_url not in proxies:
            proxies[proxy_url] = {"success": 0, "failure": 0}
        proxies[proxy_url][kind] = count

    results = []
    for url, stats in proxies.items():
        total = stats["success"] + stats["failure"]
        rate = (stats["success"] / total * 100) if total > 0 else 0.0
        results.append({
            "proxy_url": url,
            "success": stats["success"],
            "failure": stats["failure"],
            "total": total,
            "success_rate": round(rate, 2),
        })

    # 按总数降序排列
    results.sort(key=lambda x: x["total"], reverse=True)
    return results


@router.get("/by-platform")
async def get_by_platform():
    """按平台统计成功率。"""
    return dao.get_stats_by_platform()

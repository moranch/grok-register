"""
代理池路由。

对应 Requirement 6 AC1-AC5：代理池管理。
- GET / — 列表
- POST / — 新增
- PATCH /{id} — 更新（含 reset_stats）
- DELETE /{id} — 删除
- POST /bulk-import — 批量导入
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from data import dao
from data.models import ProxyModel

router = APIRouter(prefix="/proxies", tags=["proxies"])


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


class ProxyCreateRequest(BaseModel):
    """新增代理请求。"""
    url: str
    label: str = ""
    enabled: bool = True


class ProxyUpdateRequest(BaseModel):
    """更新代理请求。"""
    url: Optional[str] = None
    label: Optional[str] = None
    enabled: Optional[bool] = None
    reset_stats: bool = False


class BulkImportRequest(BaseModel):
    """批量导入代理请求。"""
    urls: str  # 换行分隔的 URL 列表


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/")
async def list_proxies():
    """获取所有代理列表。"""
    proxies = dao.list_proxies()
    return [
        {
            "id": p.id,
            "url": p.url,
            "label": p.label,
            "enabled": p.enabled,
            "success_count": p.success_count,
            "failure_count": p.failure_count,
            "consecutive_failures": p.consecutive_failures,
            "success_rate": round(
                p.success_count / max(p.success_count + p.failure_count, 1) * 100, 2
            ),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in proxies
    ]


@router.post("/")
async def create_proxy(body: ProxyCreateRequest):
    """新增代理。"""
    if not body.url.strip():
        raise HTTPException(status_code=400, detail="url 不能为空")

    proxy = dao.save_proxy({
        "url": body.url.strip(),
        "label": body.label,
        "enabled": body.enabled,
    })

    return {"ok": True, "id": proxy.id, "url": proxy.url}


@router.patch("/{proxy_id}")
async def update_proxy(proxy_id: int, body: ProxyUpdateRequest):
    """更新代理（含 reset_stats）。"""
    with dao.get_session() as s:
        model = s.get(ProxyModel, proxy_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"代理 {proxy_id} 不存在")

        if body.url is not None:
            model.url = body.url.strip()
        if body.label is not None:
            model.label = body.label
        if body.enabled is not None:
            model.enabled = body.enabled

        if body.reset_stats:
            model.success_count = 0
            model.failure_count = 0
            model.consecutive_failures = 0

        model.updated_at = datetime.now(timezone.utc).isoformat()
        s.add(model)
        s.commit()
        s.refresh(model)

    return {"ok": True, "id": model.id}


@router.delete("/{proxy_id}")
async def delete_proxy(proxy_id: int):
    """删除代理。"""
    with dao.get_session() as s:
        model = s.get(ProxyModel, proxy_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"代理 {proxy_id} 不存在")
        s.delete(model)
        s.commit()

    return {"ok": True, "id": proxy_id}


@router.post("/bulk-import")
async def bulk_import(body: BulkImportRequest):
    """批量导入代理。"""
    urls = [u.strip() for u in body.urls.strip().split("\n") if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="urls 不能为空")

    # 去重：获取已有的 URL
    existing_proxies = dao.list_proxies()
    existing_urls = {p.url for p in existing_proxies}

    imported = 0
    skipped = 0
    for url in urls:
        if url in existing_urls:
            skipped += 1
            continue
        dao.save_proxy({"url": url, "label": "", "enabled": True})
        existing_urls.add(url)
        imported += 1

    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "total_input": len(urls),
    }

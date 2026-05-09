"""
账号资产路由。

对应 Requirement 4 AC1-AC5, Requirement 7 AC1-AC5, Requirement 16 AC1-AC3。
- GET / — 分页列表（支持多维过滤）
- GET /summary — 汇总统计
- GET /{id} — 账号详情
- POST /{id}/check — 手动有效性检测
- POST /{id}/refresh — 手动刷新 token
- DELETE / — 批量删除
- GET /export — 批量导出
- POST /batch-sync-status — 批量状态同步
- POST /backfill-remote-auth — 补传远端 auth-file
- GET /sync-jobs/{job_id}/stream — 同步任务 SSE
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.registry import PLATFORM_REGISTRY
from data import dao

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ─── 请求/响应模型 ────────────────────────────────────────────────────────────


class BatchDeleteRequest(BaseModel):
    """批量删除请求。"""
    ids: List[int]


class BatchSyncRequest(BaseModel):
    """批量状态同步请求。"""
    platform: str = ""
    filter_lifecycle: str = ""
    filter_validity: str = ""


class BackfillRequest(BaseModel):
    """补传远端 auth-file 请求。"""
    platform: str = ""
    account_ids: List[int] = []


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/")
async def list_accounts(
    platform: str = Query(default="", description="按平台过滤"),
    lifecycle_status: str = Query(default="", description="按生命周期状态过滤"),
    plan_state: str = Query(default="", description="按套餐状态过滤"),
    validity_status: str = Query(default="", description="按有效性状态过滤"),
    keyword: str = Query(default="", description="关键词搜索（email/sso）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    """分页查询账号列表。"""
    offset = (page - 1) * page_size
    accounts, total = dao.list_accounts(
        platform=platform,
        lifecycle_status=lifecycle_status,
        plan_state=plan_state,
        validity_status=validity_status,
        keyword=keyword,
        limit=page_size,
        offset=offset,
    )

    items = []
    for a in accounts:
        items.append({
            "id": a.id,
            "platform": a.platform,
            "email": a.email,
            "password": a.password,
            "sso": a.sso[:20] + "..." if len(a.sso) > 20 else a.sso,
            "user_id": a.user_id,
            "proxy_url": a.proxy_url,
            "lifecycle_status": a.lifecycle_status,
            "plan_state": a.plan_state,
            "validity_status": a.validity_status,
            "last_checked_at": a.last_checked_at,
            "task_id": a.task_id,
            "created_at": a.created_at,
            "updated_at": a.updated_at,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/summary")
async def get_summary():
    """账号汇总统计。"""
    return dao.get_account_summary()


@router.get("/export")
async def export_accounts(
    fmt: str = Query(default="json", description="导出格式: json/csv/sso"),
    platform: str = Query(default="", description="按平台过滤"),
):
    """批量导出账号。"""
    accounts, _ = dao.list_accounts(platform=platform, limit=99999)

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "platform", "email", "password", "sso", "user_id", "lifecycle_status", "plan_state", "validity_status", "created_at"])
        for a in accounts:
            writer.writerow([a.id, a.platform, a.email, a.password, a.sso, a.user_id, a.lifecycle_status, a.plan_state, a.validity_status, a.created_at])
        content = output.getvalue()
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=accounts.csv"},
        )

    elif fmt == "sso":
        lines = []
        for a in accounts:
            if a.sso:
                lines.append(a.sso)
        content = "\n".join(lines)
        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": "attachment; filename=accounts_sso.txt"},
        )

    else:
        # JSON 格式
        data = []
        for a in accounts:
            extra = {}
            try:
                extra = json.loads(a.extra_json) if a.extra_json else {}
            except (json.JSONDecodeError, TypeError):
                pass
            data.append({
                "id": a.id,
                "platform": a.platform,
                "email": a.email,
                "password": a.password,
                "sso": a.sso,
                "user_id": a.user_id,
                "lifecycle_status": a.lifecycle_status,
                "plan_state": a.plan_state,
                "validity_status": a.validity_status,
                "extra": extra,
                "created_at": a.created_at,
            })
        return data


@router.get("/{account_id}")
async def get_account(account_id: int):
    """获取账号详情。"""
    accounts, _ = dao.list_accounts(limit=99999)
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")

    extra = {}
    try:
        extra = json.loads(account.extra_json) if account.extra_json else {}
    except (json.JSONDecodeError, TypeError):
        pass

    exporter_status = {}
    try:
        exporter_status = json.loads(account.exporter_status_json) if account.exporter_status_json else {}
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "id": account.id,
        "platform": account.platform,
        "email": account.email,
        "password": account.password,
        "sso": account.sso,
        "user_id": account.user_id,
        "proxy_url": account.proxy_url,
        "lifecycle_status": account.lifecycle_status,
        "plan_state": account.plan_state,
        "validity_status": account.validity_status,
        "extra": extra,
        "exporter_status": exporter_status,
        "last_error": account.last_error,
        "last_checked_at": account.last_checked_at,
        "task_id": account.task_id,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


@router.post("/{account_id}/check")
async def check_account(account_id: int):
    """手动有效性检测。"""
    accounts, _ = dao.list_accounts(limit=99999)
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")

    cls = PLATFORM_REGISTRY.get(account.platform)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"平台 '{account.platform}' 未注册")

    instance = cls()
    account_dict = {
        "id": account.id,
        "platform": account.platform,
        "email": account.email,
        "sso": account.sso,
        "extra_json": account.extra_json,
    }

    try:
        is_valid = instance.check_validity(account_dict)
    except Exception as e:
        dao.update_account(account_id, {"last_error": str(e), "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")})
        raise HTTPException(status_code=500, detail=f"检测失败: {e}")

    new_status = "valid" if is_valid else "invalid"
    dao.update_account(account_id, {
        "validity_status": new_status,
        "last_checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_error": "",
    })

    return {"ok": True, "account_id": account_id, "validity_status": new_status}


@router.post("/{account_id}/refresh")
async def refresh_account(account_id: int):
    """手动刷新 token。"""
    accounts, _ = dao.list_accounts(limit=99999)
    account = next((a for a in accounts if a.id == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")

    cls = PLATFORM_REGISTRY.get(account.platform)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"平台 '{account.platform}' 未注册")

    instance = cls()
    if not instance.capabilities.supports_refresh:
        raise HTTPException(status_code=400, detail=f"平台 '{account.platform}' 不支持 token 刷新")

    account_dict = {
        "id": account.id,
        "platform": account.platform,
        "email": account.email,
        "sso": account.sso,
        "extra_json": account.extra_json,
    }

    try:
        result = instance.refresh_token(account_dict)
    except Exception as e:
        dao.update_account(account_id, {"last_error": str(e)})
        raise HTTPException(status_code=500, detail=f"刷新失败: {e}")

    if result:
        # 合并新 token 到 extra_json
        extra = {}
        try:
            extra = json.loads(account.extra_json) if account.extra_json else {}
        except (json.JSONDecodeError, TypeError):
            pass
        extra.update(result)
        dao.update_account(account_id, {
            "extra_json": json.dumps(extra, ensure_ascii=False),
            "last_error": "",
        })

    return {"ok": True, "account_id": account_id, "refreshed": result is not None}


@router.delete("/")
async def batch_delete(body: BatchDeleteRequest):
    """批量删除账号。"""
    if not body.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")

    deleted = dao.delete_accounts(body.ids)
    return {"ok": True, "deleted": deleted}


@router.post("/batch-sync-status")
async def batch_sync_status(body: BatchSyncRequest):
    """批量状态同步。"""
    if not body.platform:
        raise HTTPException(status_code=400, detail="platform 不能为空")

    cls = PLATFORM_REGISTRY.get(body.platform)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"平台 '{body.platform}' 未注册")

    instance = cls()
    if not instance.capabilities.supports_batch_status_sync:
        raise HTTPException(status_code=400, detail=f"平台 '{body.platform}' 不支持批量状态同步")

    # 创建同步任务记录
    from data.models import SyncJobModel
    from sqlmodel import Session
    with dao.get_session() as s:
        job = SyncJobModel(
            kind="batch_status_sync",
            platform=body.platform,
            status="queued",
            filter_json=json.dumps({"lifecycle": body.filter_lifecycle, "validity": body.filter_validity}, ensure_ascii=False),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

    return {"ok": True, "job_id": job.id, "message": "批量同步任务已创建"}


@router.post("/backfill-remote-auth")
async def backfill_remote_auth(body: BackfillRequest):
    """补传远端 auth-file。"""
    if not body.platform:
        raise HTTPException(status_code=400, detail="platform 不能为空")

    cls = PLATFORM_REGISTRY.get(body.platform)
    if cls is None:
        raise HTTPException(status_code=400, detail=f"平台 '{body.platform}' 未注册")

    instance = cls()
    if not instance.capabilities.supports_remote_auth_file:
        raise HTTPException(status_code=400, detail=f"平台 '{body.platform}' 不支持远端 auth-file 补传")

    # 创建同步任务记录
    from data.models import SyncJobModel
    with dao.get_session() as s:
        job = SyncJobModel(
            kind="backfill_remote_auth",
            platform=body.platform,
            status="queued",
            total=len(body.account_ids) if body.account_ids else 0,
            filter_json=json.dumps({"account_ids": body.account_ids}, ensure_ascii=False),
        )
        s.add(job)
        s.commit()
        s.refresh(job)

    return {"ok": True, "job_id": job.id, "message": "补传任务已创建"}


@router.get("/sync-jobs/{job_id}/stream")
async def sync_job_stream(job_id: int):
    """同步任务 SSE 实时进度流。"""
    from data.models import SyncJobModel

    async def event_generator():
        while True:
            with dao.get_session() as s:
                job = s.get(SyncJobModel, job_id)
                if job is None:
                    yield f"data: {json.dumps({'error': '任务不存在'})}\n\n"
                    break

                payload = {
                    "job_id": job.id,
                    "status": job.status,
                    "total": job.total,
                    "current": job.current,
                    "ok_count": job.ok_count,
                    "fail_count": job.fail_count,
                    "error": job.error,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

                if job.status in ("completed", "failed"):
                    break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

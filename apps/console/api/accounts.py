"""
账号资产路由（覆盖旧实现，等价于 app.py 原 `/api/accounts/*` 端点）。

- GET    /api/accounts             列表
- GET    /api/accounts/summary     资产总览
- PATCH  /api/accounts/{id}        更新
- DELETE /api/accounts/{id}        删除
- GET    /api/accounts/export      导出（json/csv/sso）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from ._shared import (
    AccountUpdate,
    account_asset_summary,
    account_delete,
    account_list,
    account_update,
    check_auth,
    export_accounts,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
def api_accounts(
    request: Request, limit: int = Query(500, ge=1, le=5000)
) -> dict[str, Any]:
    check_auth(request)
    return {"items": account_list(limit)}


@router.get("/summary")
def api_accounts_summary(request: Request) -> dict[str, Any]:
    """账户资产总览：总数、生命周期 / 套餐 / 有效性分布。"""
    check_auth(request)
    return account_asset_summary()


@router.get("/export")
def api_accounts_export(
    request: Request,
    fmt: str = Query("json", pattern="^(json|csv|sso)$"),
) -> Response:
    check_auth(request)
    content, media_type, filename = export_accounts(fmt)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{account_id}")
def api_account_update(
    request: Request, account_id: int, payload: AccountUpdate
) -> dict[str, Any]:
    check_auth(request)
    row = account_update(
        account_id,
        lifecycle_status=payload.lifecycle_status,
        plan_state=payload.plan_state,
        validity_status=payload.validity_status,
        notes=payload.notes,
        last_error=payload.last_error,
        sso=payload.sso,
        email=payload.email,
        password=payload.password,
    )
    return {"account": row}


@router.delete("/{account_id}")
def api_account_delete(request: Request, account_id: int) -> dict[str, Any]:
    check_auth(request)
    account_delete(account_id)
    return {"ok": True}

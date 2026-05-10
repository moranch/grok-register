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

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ._shared import (
    AccountUpdate,
    account_asset_summary,
    account_delete,
    account_list,
    account_update,
    check_auth,
    execute_no_return,
    export_accounts,
    fetch_one,
    now_iso,
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


@router.post("/{account_id}/query-state")
def api_account_query_state(request: Request, account_id: int) -> dict[str, Any]:
    """调用平台的 query_state action 查询账号状态/套餐/额度，结果写回 DB。"""
    check_auth(request)
    from _vendor_aar.infrastructure.platform_runtime import PlatformRuntime
    from _vendor_aar.domain.actions import ActionExecutionCommand

    row = fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")

    platform = row["platform"] or "grok"

    # grok 没有 query_state（没有套餐/额度 API），直接返回
    if platform == "grok":
        return {"ok": False, "error": "Grok 平台不支持查询状态（无套餐/额度 API）"}

    runtime = PlatformRuntime()
    # 先查 vendor DB 里有没有这个 account（vendor 用自己的 account_manager.db）
    from core._vendor_aar.db import engine as _vendor_engine, AccountModel as _VendorAccount
    from sqlmodel import Session as _Session, select as _select
    with _Session(_vendor_engine) as session:
        vendor_account = session.exec(
            _select(_VendorAccount).where(
                _VendorAccount.platform == platform,
                _VendorAccount.email == row["email"],
            )
        ).first()
        if not vendor_account:
            # vendor DB 里没有这个账号，先创建一条
            import json as _json
            extra = _json.loads(row["extra_json"] or "{}")
            vendor_account = _VendorAccount(
                platform=platform,
                email=row["email"],
                password=row["password"] or "",
                user_id="",
            )
            session.add(vendor_account)
            session.commit()
            session.refresh(vendor_account)
            # 同步 credentials 到 vendor graph
            from core._vendor_aar.base_platform import Account, AccountStatus
            account_obj = Account(
                platform=platform,
                email=row["email"],
                password=row["password"] or "",
                token=row["sso"] or "",
                extra=extra,
            )
            from core._vendor_aar.account_graph import sync_platform_account_graph
            sync_platform_account_graph(session, vendor_account, account_obj)
            session.commit()

        vendor_account_id = int(vendor_account.id)

    # 调用 execute_action
    cmd = ActionExecutionCommand(
        platform=platform,
        account_id=vendor_account_id,
        action_id="query_state",
        params={},
    )
    try:
        result = runtime.execute_action(cmd)
    except NotImplementedError as exc:
        return {"ok": False, "error": f"该平台不支持查询状态: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"查询失败: {exc}"}

    if result.ok and isinstance(result.data, dict):
        # 把结果写回我们的 accounts 表
        import json as _json
        overview = result.data.get("account_overview") or result.data
        existing_extra = _json.loads(row["extra_json"] or "{}")
        existing_extra["account_overview"] = overview
        extra_json = _json.dumps(existing_extra, ensure_ascii=False, default=str)

        # 推导 plan_state
        from core._vendor_aar.account_graph import _derive_plan_state, _derive_validity_status
        lifecycle = row["lifecycle_status"] or "registered"
        plan_state = _derive_plan_state(lifecycle, overview, 0) or "unknown"
        validity = _derive_validity_status(lifecycle, overview)
        if validity == "unknown" and result.data.get("valid") is not None:
            validity = "valid" if result.data["valid"] else "invalid"

        execute_no_return(
            """UPDATE accounts SET extra_json=?, plan_state=?, validity_status=?, last_checked_at=? WHERE id=?""",
            (extra_json, plan_state, validity, now_iso(), account_id),
        )
        return {"ok": True, "data": result.data, "plan_state": plan_state, "validity_status": validity}

    return {"ok": False, "error": result.error or "查询失败"}

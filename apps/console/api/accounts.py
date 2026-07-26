"""
账号资产路由（覆盖旧实现，等价于 app.py 原 `/api/accounts/*` 端点）。

- GET    /api/accounts             列表
- GET    /api/accounts/summary     资产总览
- PATCH  /api/accounts/{id}        更新
- DELETE /api/accounts/{id}        删除
- GET    /api/accounts/export      导出（json/csv/sso）
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import requests

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ._shared import (
    AccountUpdate,
    AccountDeliveryCreate,
    DOWNLOAD_GATE_INTERNAL_TOKEN,
    DOWNLOAD_GATE_INTERNAL_URL,
    DOWNLOAD_GATE_PUBLIC_URL,
    account_delivery_document,
    account_asset_summary,
    account_delete,
    account_list,
    account_list_by_ids,
    account_update,
    check_auth,
    execute_no_return,
    export_accounts,
    fetch_all,
    fetch_one,
    now_iso,
)

from . import _delivery_runtime


router = APIRouter(tags=["accounts"])


class InternalDeliveryReserve(BaseModel):
    card_key: str = Field(..., min_length=1, max_length=200)
    platform: str = Field("grok", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    required_model: str = Field("", max_length=100)


class InternalDeliveryCommit(BaseModel):
    card_key: str = Field(..., min_length=1, max_length=200)
    lease_id: str = Field(..., min_length=1, max_length=100)
    lease_token: str = Field(..., min_length=1, max_length=200)
    bundle_id: str = Field("", max_length=200)


class Sub2ApiImportRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    group_id: int = Field(0, ge=0)
    force: bool = False


class Sub2ApiOAuthStartRequest(BaseModel):
    email: str = Field("", max_length=320)


class Sub2ApiOAuthCompleteRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=500)
    state: str = Field(..., min_length=1, max_length=1000)
    callback: str = Field(..., min_length=1, max_length=5000)
    email: str = Field("", max_length=320)
    group_id: int = Field(0, ge=0)


class Sub2ApiConfigUpdate(BaseModel):
    enabled: bool = False
    base_url: str = Field("", max_length=1000)
    auth_mode: str = Field("api_key", pattern="^(api_key|password)$")
    api_key: str = Field("", max_length=2000)
    admin_email: str = Field("", max_length=320)
    admin_password: str = Field("", max_length=2000)
    group_id: int = Field(0, ge=0)
    auto_import: bool = False
    sso_fallback: bool = True
    retries: int = Field(2, ge=0, le=5)
    workers: int = Field(2, ge=1, le=8)
    timeout: int = Field(45, ge=5, le=300)
    verify_tls: bool = True


def _sub2api_client_config() -> dict[str, Any]:
    from ._sub2api_runtime import sub2api_import_runtime

    config = sub2api_import_runtime.config()
    extra = dict(config.get("extra") or {})
    if config.get("endpoint"):
        extra["base_url"] = str(config["endpoint"])
    for key in (
        "group_id",
        "auto_import",
        "sso_fallback",
        "retries",
        "workers",
        "oauth_base_url",
    ):
        extra.pop(key, None)
    return extra


def _raise_delivery_error(exc: Exception) -> None:
    if isinstance(exc, _delivery_runtime.DeliveryUnauthorized):
        raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"})
    if isinstance(exc, _delivery_runtime.DeliveryNotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, _delivery_runtime.DeliveryConflict):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, _delivery_runtime.DeliveryUnavailable):
        status = 409 if str(exc) == "no active account is available" else 503
        raise HTTPException(status_code=status, detail=str(exc))
    raise exc


def _delivery_filename(account: dict[str, Any]) -> str:
    email = str(account.get("email") or "account")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", email).strip("._") or "account"
    platform = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        str(account.get("platform") or "grok").strip().lower(),
    ).strip("-") or "account"
    return f"{platform}-account-{int(account['id'])}-{slug[:80]}.json"


def _manual_delivery_response(
    result: dict[str, Any],
    title: str,
    file_count: int,
    platform: str = "grok",
) -> dict[str, Any]:
    key = str(result.get("key") or "")
    return {
        "ok": True,
        "bundle_id": result.get("bundle_id"),
        "key": key,
        "claim_url": f"{DOWNLOAD_GATE_PUBLIC_URL}/?key={quote(key, safe='')}",
        "file_count": int(result.get("file_count") or file_count),
        "platform": str(result.get("platform") or platform),
        "title": result.get("title") or title,
    }


_INTERNAL_ACCOUNT_INVENTORY_STATES = {
    "all",
    "ready",
    "unverified",
    "invalid",
    "delivered",
    "leased",
    "active",
    "inactive",
}


def _internal_account_like_pattern(value: str) -> str:
    """Return a literal LIKE pattern instead of treating user input as wildcards."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _internal_account_list(
    *,
    search: str = "",
    status: str = "all",
    platform: str = "grok",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Build the credential-free account inventory consumed by DownloadGate.

    The SQL deliberately projects only display/status fields.  Raw ``sso``,
    ``password``, token values and ``extra_json`` never leave SQLite.
    """
    search = str(search or "").strip().lower()
    status = str(status or "all").strip().lower()
    platform = str(platform or "grok").strip().lower()
    if status not in _INTERNAL_ACCOUNT_INVENTORY_STATES:
        raise HTTPException(status_code=422, detail="invalid account inventory status")
    if platform == "all":
        platform = ""
    elif not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", platform):
        raise HTTPException(status_code=422, detail="invalid account platform")
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))

    # Keep historical DownloadGate packages reconciled exactly like the delivery
    # stock calculation before classifying an account as available.
    _delivery_runtime.import_download_gate_history()

    try:
        ttl_minutes = max(1, int(_delivery_runtime._delivery_prevalidation_ttl_minutes()))
    except Exception:
        ttl_minutes = 60
    verification_cutoff = (datetime.now() - timedelta(minutes=ttl_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    pattern = _internal_account_like_pattern(search) if search else ""
    base_params: tuple[Any, ...] = (
        platform,
        platform,
        search,
        pattern,
        pattern,
        verification_cutoff,
        verification_cutoff,
    )
    cte = """
        WITH account_source AS (
            SELECT
                a.id,
                COALESCE(a.email, '') AS email,
                COALESCE(a.status, '') AS status,
                COALESCE(a.platform, 'grok') AS platform,
                COALESCE(a.lifecycle_status, 'registered') AS lifecycle_status,
                COALESCE(a.validity_status, 'unknown') AS validity_status,
                COALESCE(a.plan_state, 'unknown') AS plan_state,
                COALESCE(a.created_at, '') AS created_at,
                COALESCE(a.last_checked_at, '') AS last_checked_at,
                COALESCE(a.last_error, '') AS account_last_error,
                CASE
                    WHEN COALESCE(a.platform, 'grok') = 'grok'
                    THEN COALESCE(a.sso, '') <> ''
                    ELSE COALESCE(a.sso, '') <> '' OR COALESCE(a.password, '') <> ''
                END AS identity_ready,
                CASE
                    WHEN json_valid(a.extra_json) THEN a.extra_json
                    ELSE '{}'
                END AS safe_extra,
                EXISTS (
                    SELECT 1
                    FROM account_delivery_consumptions consumption
                    WHERE consumption.account_id = a.id
                ) AS delivered,
                EXISTS (
                    SELECT 1
                    FROM account_delivery_leases lease
                    WHERE lease.account_id = a.id
                      AND lease.state IN ('probing', 'ready', 'packing')
                ) AS leased
            FROM accounts a
            WHERE (? = '' OR a.platform = ?)
              AND (
                    ? = ''
                    OR CAST(a.id AS TEXT) LIKE ? ESCAPE '\\'
                    OR LOWER(a.email) LIKE ? ESCAPE '\\'
              )
        ),
        projected AS (
            SELECT
                id,
                email,
                status,
                platform,
                lifecycle_status,
                validity_status,
                plan_state,
                created_at,
                last_checked_at,
                COALESCE(json_extract(safe_extra, '$.cpa.status'), '') AS cpa_status,
                CASE
                    WHEN json_extract(safe_extra, '$.cpa.credential_ready') = 1 THEN 1
                    ELSE 0
                END AS credential_ready,
                CASE
                    WHEN COALESCE(
                        json_extract(safe_extra, '$.cpa.probe.account_alive'),
                        json_extract(safe_extra, '$.cpa.probe.ok')
                    ) = 1 THEN 1
                    ELSE 0
                END AS account_alive,
                COALESCE(
                    json_extract(safe_extra, '$.cpa.probe_checked_at'),
                    json_extract(safe_extra, '$.cpa.updated_at'),
                    ''
                ) AS probe_checked_at,
                COALESCE(
                    json_extract(safe_extra, '$.cpa.probe.probe_kind'),
                    ''
                ) AS probe_kind,
                COALESCE(
                    NULLIF(json_extract(safe_extra, '$.cpa.failure_kind'), ''),
                    NULLIF(json_extract(safe_extra, '$.cpa.probe.failure_kind'), ''),
                    ''
                ) AS failure_kind,
                COALESCE(
                    NULLIF(json_extract(safe_extra, '$.cpa.error'), ''),
                    NULLIF(json_extract(safe_extra, '$.cpa.probe_error'), ''),
                    NULLIF(json_extract(safe_extra, '$.cpa.probe.error'), ''),
                    NULLIF(account_last_error, ''),
                    ''
                ) AS last_error,
                CASE
                    WHEN TRIM(COALESCE(json_extract(safe_extra, '$.access_token'), '')) <> ''
                     AND TRIM(COALESCE(json_extract(safe_extra, '$.refresh_token'), '')) <> ''
                    THEN 1 ELSE 0
                END AS token_pair_ready,
                CASE
                    WHEN COALESCE(
                        json_extract(safe_extra, '$.cpa.probe.probe_kind'), ''
                    ) IN ('account_identity', 'account_response')
                    THEN 1
                    WHEN COALESCE(
                        json_extract(safe_extra, '$.cpa.probe.probe_kind'), ''
                    ) = 'account_session'
                     AND json_extract(safe_extra, '$.cpa.credential_ready') = 1
                    THEN 1
                    ELSE 0
                END AS account_probe_ready,
                identity_ready,
                delivered,
                leased,
                CASE
                    WHEN status <> 'active'
                      OR lifecycle_status IN ('expired', 'invalid')
                      OR validity_status = 'invalid'
                    THEN 1 ELSE 0
                END AS invalid_account
            FROM account_source
        ),
        classified AS (
            SELECT
                *,
                CASE
                    WHEN platform = 'grok'
                     AND token_pair_ready = 1
                     AND account_probe_ready = 1
                     AND account_alive = 1
                     AND datetime(SUBSTR(REPLACE(probe_checked_at, 'T', ' '), 1, 19))
                         >= datetime(?)
                    THEN 1 ELSE 0
                END AS recently_verified,
                CASE
                    WHEN delivered = 1 THEN 'delivered'
                    WHEN leased = 1 THEN 'leased'
                    WHEN invalid_account = 1 THEN 'invalid'
                    WHEN identity_ready = 0 THEN 'unverified'
                    WHEN platform <> 'grok' THEN 'ready'
                    WHEN token_pair_ready = 1
                     AND account_probe_ready = 1
                     AND account_alive = 1
                     AND datetime(SUBSTR(REPLACE(probe_checked_at, 'T', ' '), 1, 19))
                         >= datetime(?)
                    THEN 'ready'
                    ELSE 'unverified'
                END AS inventory_status
            FROM projected
        )
    """
    if status in {"ready", "unverified", "invalid", "delivered", "leased"}:
        filter_sql = "inventory_status = ?"
        filter_params: tuple[Any, ...] = (status,)
    elif status == "active":
        filter_sql = "status = 'active'"
        filter_params = ()
    elif status == "inactive":
        filter_sql = "status <> 'active'"
        filter_params = ()
    else:
        filter_sql = "1 = 1"
        filter_params = ()

    summary_row = fetch_one(
        cte
        + """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(inventory_status = 'ready'), 0) AS ready,
            COALESCE(SUM(inventory_status = 'unverified'), 0) AS unverified,
            COALESCE(SUM(inventory_status = 'invalid'), 0) AS invalid,
            COALESCE(SUM(inventory_status = 'delivered'), 0) AS delivered,
            COALESCE(SUM(inventory_status = 'leased'), 0) AS leased,
            COALESCE(SUM(status = 'active'), 0) AS active,
            COALESCE(SUM(status <> 'active'), 0) AS inactive
        FROM classified
        """,
        base_params,
    )
    summary_values = {
        key: int(summary_row[key] if summary_row else 0)
        for key in (
            "total",
            "ready",
            "unverified",
            "invalid",
            "delivered",
            "leased",
            "active",
            "inactive",
        )
    }
    filtered_total = summary_values["total" if status == "all" else status]
    offset = (page - 1) * page_size
    rows = fetch_all(
        cte
        + f"""
        SELECT
            id,
            email,
            status,
            platform,
            lifecycle_status,
            validity_status,
            plan_state,
            created_at,
            last_checked_at,
            cpa_status,
            credential_ready,
            account_alive,
            probe_checked_at,
            probe_kind,
            failure_kind,
            last_error,
            delivered,
            leased,
            recently_verified,
            inventory_status
        FROM classified
        WHERE {filter_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        base_params + filter_params + (page_size, offset),
    )
    boolean_fields = {
        "credential_ready",
        "account_alive",
        "delivered",
        "leased",
        "recently_verified",
    }
    items: list[dict[str, Any]] = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        for key in boolean_fields:
            item[key] = bool(item[key])
        items.append(item)
    summary = {
        key: summary_values[key]
        for key in ("total", "ready", "unverified", "invalid", "delivered", "leased")
    }
    return {
        "items": items,
        "summary": summary,
        "total": filtered_total,
        "page": page,
        "page_size": page_size,
        "pages": (filtered_total + page_size - 1) // page_size,
        "verification_ttl_minutes": ttl_minutes,
        "filters": {
            "search": search,
            "status": status,
            "platform": platform or "all",
        },
    }


@router.get("/api/accounts")
def api_accounts(
    request: Request, limit: int = Query(500, ge=1, le=5000)
) -> dict[str, Any]:
    check_auth(request)
    return {"items": account_list(limit)}


@router.get("/api/internal/accounts")
def api_internal_accounts(
    request: Request,
    search: str = Query("", max_length=320),
    status: str = Query(
        "all",
        pattern="^(all|ready|unverified|invalid|delivered|leased|active|inactive)$",
    ),
    platform: str = Query("grok", max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    try:
        _delivery_runtime.check_internal_bearer(request.headers.get("Authorization", ""))
    except Exception as exc:
        _raise_delivery_error(exc)
        raise
    return _internal_account_list(
        search=search,
        status=status,
        platform=platform,
        page=page,
        page_size=page_size,
    )


@router.get("/api/accounts/summary")
def api_accounts_summary(request: Request) -> dict[str, Any]:
    """账户资产总览：总数、生命周期 / 套餐 / 有效性分布。"""
    check_auth(request)
    return account_asset_summary()


@router.get("/api/accounts/export")
def api_accounts_export(
    request: Request,
    fmt: str = Query("json", pattern="^(json|backup|csv|sso)$"),
) -> Response:
    check_auth(request)
    content, media_type, filename = export_accounts(fmt)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/accounts/delivery")
def api_accounts_delivery(
    request: Request,
    payload: AccountDeliveryCreate,
) -> dict[str, Any]:
    check_auth(request)
    requested_ids = list(dict.fromkeys(payload.account_ids))
    accounts = account_list_by_ids(requested_ids)
    found_ids = {int(account["id"]) for account in accounts}
    missing_ids = [account_id for account_id in requested_ids if account_id not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=404,
            detail={"message": "部分账户不存在", "missing_account_ids": missing_ids},
        )
    if not DOWNLOAD_GATE_INTERNAL_TOKEN:
        raise HTTPException(status_code=503, detail="DownloadGate 内部 API Token 未配置")

    platforms = {
        str(account.get("platform") or "grok").strip().lower()
        for account in accounts
    }
    if len(platforms) != 1:
        raise HTTPException(status_code=409, detail="一次交付不能混合多个目标平台")
    platform = next(iter(platforms))
    title = (payload.title or "").strip() or (
        f"{platform} 账号交付 {len(accounts)} 个 - {datetime.now():%Y-%m-%d %H:%M}"
    )
    try:
        delivery_order = _delivery_runtime.prepare_selected_request(requested_ids, title)
    except Exception as exc:
        _raise_delivery_error(exc)
        raise

    delivery_order_id = int(delivery_order["order_id"])
    delivery_card_key = str(delivery_order["card_key"])
    files = []
    documents: dict[int, dict[str, Any]] = {}
    for account in accounts:
        document = account_delivery_document(account)
        documents[int(account["id"])] = document
        files.append(
            {
                "filename": _delivery_filename(account),
                "data": document,
            }
        )
    recovered = _delivery_runtime.recover_selected(delivery_order_id, documents)
    if recovered:
        return _manual_delivery_response(recovered, title, len(files), platform)
    if delivery_order.get("reused"):
        detail = "此前打包请求结果尚未确认，账号仍被安全保留；请稍后重试以恢复原卡密"
        _delivery_runtime.abort_selected(delivery_order_id, detail)
        raise HTTPException(status_code=409, detail=detail)
    try:
        response = requests.post(
            f"{DOWNLOAD_GATE_INTERNAL_URL}/api/internal/bundles",
            headers={
                "Authorization": f"Bearer {DOWNLOAD_GATE_INTERNAL_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "title": title,
                "pack_mode": payload.pack_mode,
                "key": delivery_card_key,
                "files": files,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        recovered = _delivery_runtime.recover_selected(delivery_order_id, documents)
        if recovered:
            return _manual_delivery_response(recovered, title, len(files), platform)
        _delivery_runtime.abort_selected(delivery_order_id, str(exc))
        raise HTTPException(
            status_code=502,
            detail=(
                "DownloadGate 响应未确认，账号已安全保留且不会再次分配；"
                f"请稍后重试恢复原卡密: {exc}"
            ),
        ) from exc

    try:
        result = response.json()
    except ValueError:
        result = {"error": response.text[:500] or "DownloadGate 返回了无效响应"}
    if not response.ok:
        detail = result.get("error") if isinstance(result, dict) else "DownloadGate 打包失败"
        recovered = _delivery_runtime.recover_selected(delivery_order_id, documents)
        if recovered:
            return _manual_delivery_response(recovered, title, len(files), platform)
        _delivery_runtime.abort_selected(delivery_order_id, str(detail or "DownloadGate 打包失败"))
        raise HTTPException(
            status_code=502,
            detail=(
                "DownloadGate 打包结果未确认，账号已安全保留且不会再次分配；"
                f"请稍后重试恢复原卡密: {detail or '未知错误'}"
            ),
        )

    key = str(result.get("key") or "")
    if key != delivery_card_key:
        recovered = _delivery_runtime.recover_selected(delivery_order_id, documents)
        if recovered:
            return _manual_delivery_response(recovered, title, len(files), platform)
        detail = "DownloadGate 返回了不匹配的卡密，账号已安全保留等待恢复"
        _delivery_runtime.abort_selected(delivery_order_id, detail)
        raise HTTPException(status_code=502, detail=detail)
    try:
        _delivery_runtime.commit_selected(
            delivery_order_id,
            card_key=key,
            bundle_id=str(result.get("bundle_id") or ""),
            documents=documents,
        )
    except Exception as exc:
        _raise_delivery_error(exc)
        raise
    return _manual_delivery_response(result, title, len(files), platform)


@router.post("/api/internal/account-deliveries/reserve")
def api_internal_account_delivery_reserve(
    request: Request,
    payload: InternalDeliveryReserve,
) -> dict[str, Any]:
    try:
        _delivery_runtime.check_internal_bearer(request.headers.get("Authorization", ""))
        return _delivery_runtime.reserve(
            payload.card_key,
            payload.required_model,
            payload.platform,
        )
    except Exception as exc:
        _raise_delivery_error(exc)
        raise


@router.post("/api/internal/account-deliveries/commit")
def api_internal_account_delivery_commit(
    request: Request,
    payload: InternalDeliveryCommit,
) -> dict[str, Any]:
    try:
        _delivery_runtime.check_internal_bearer(request.headers.get("Authorization", ""))
        return _delivery_runtime.commit(
            payload.card_key,
            payload.lease_id,
            payload.lease_token,
            payload.bundle_id,
        )
    except Exception as exc:
        _raise_delivery_error(exc)
        raise


@router.get("/api/internal/account-deliveries/by-card/{card_key}")
def api_internal_account_delivery_by_card(
    request: Request,
    card_key: str,
) -> dict[str, Any]:
    try:
        _delivery_runtime.check_internal_bearer(request.headers.get("Authorization", ""))
        return _delivery_runtime.by_card(card_key)
    except Exception as exc:
        _raise_delivery_error(exc)
        raise


@router.post("/api/accounts/cpa/backfill")
def api_accounts_cpa_backfill(
    request: Request,
    limit: int = Query(0, ge=0, le=5000),
    force: bool = Query(False),
) -> dict[str, Any]:
    """将缺少 refresh_token 的 Grok 存量账号加入后台 OAuth 补全队列。"""
    check_auth(request)
    from ._cpa_runtime import cpa_mint_runtime

    return {"ok": True, "job": cpa_mint_runtime.enqueue_backfill(limit=limit, force=force)}


@router.get("/api/accounts/cpa/jobs/{job_id}")
def api_accounts_cpa_job(request: Request, job_id: str) -> dict[str, Any]:
    check_auth(request)
    from ._cpa_runtime import cpa_mint_runtime

    job = cpa_mint_runtime.job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="CPA backfill job not found")
    return {"job": job}


@router.get("/api/accounts/sub2api/groups")
def api_accounts_sub2api_groups(request: Request) -> dict[str, Any]:
    check_auth(request)
    from core.sub2api_client import list_groups

    result = list_groups(**_sub2api_client_config())
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/api/accounts/sub2api/config")
def api_accounts_sub2api_config(request: Request) -> dict[str, Any]:
    check_auth(request)
    from ._sub2api_runtime import sub2api_import_runtime

    config = sub2api_import_runtime.config()
    extra = dict(config.get("extra") or {})
    return {
        "enabled": bool(config.get("enabled")),
        "base_url": str(config.get("endpoint") or extra.get("base_url") or ""),
        "auth_mode": str(extra.get("auth_mode") or "api_key"),
        "api_key": "",
        "has_api_key": bool(str(extra.get("api_key") or "")),
        "admin_email": str(extra.get("admin_email") or ""),
        "admin_password": "",
        "has_admin_password": bool(str(extra.get("admin_password") or "")),
        "group_id": int(extra.get("group_id") or 0),
        "auto_import": bool(extra.get("auto_import", False)),
        "sso_fallback": bool(extra.get("sso_fallback", True)),
        "retries": int(extra.get("retries") or 2),
        "workers": int(extra.get("workers") or 2),
        "timeout": int(extra.get("timeout") or 45),
        "verify_tls": bool(extra.get("verify_tls", True)),
    }


@router.patch("/api/accounts/sub2api/config")
def api_accounts_sub2api_config_update(
    request: Request, payload: Sub2ApiConfigUpdate
) -> dict[str, Any]:
    check_auth(request)
    from ._sub2api_runtime import sub2api_import_runtime

    previous = sub2api_import_runtime.config()
    old_extra = dict(previous.get("extra") or {})
    api_key = payload.api_key.strip() or str(old_extra.get("api_key") or "")
    admin_password = payload.admin_password or str(
        old_extra.get("admin_password") or ""
    )
    extra = {
        **old_extra,
        "base_url": payload.base_url.strip().rstrip("/"),
        "auth_mode": payload.auth_mode,
        "api_key": api_key,
        "admin_email": payload.admin_email.strip(),
        "admin_password": admin_password,
        "group_id": payload.group_id,
        "auto_import": payload.auto_import,
        "sso_fallback": payload.sso_fallback,
        "retries": payload.retries,
        "workers": payload.workers,
        "timeout": payload.timeout,
        "verify_tls": payload.verify_tls,
    }
    config = {
        "enabled": payload.enabled,
        "endpoint": payload.base_url.strip().rstrip("/"),
        "api_append": True,
        "template": "",
        "extra": extra,
    }
    execute_no_return(
        """
        INSERT INTO settings(key, value, updated_at) VALUES('exporter_sub2api', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (json.dumps(config, ensure_ascii=False), now_iso()),
    )
    return {"ok": True, **api_accounts_sub2api_config(request)}


@router.post("/api/accounts/sub2api/import")
def api_accounts_sub2api_import(
    request: Request, payload: Sub2ApiImportRequest
) -> dict[str, Any]:
    check_auth(request)
    if not payload.account_ids:
        raise HTTPException(status_code=400, detail="account_ids 不能为空")
    from ._sub2api_runtime import sub2api_import_runtime

    return {
        "ok": True,
        "job": sub2api_import_runtime.enqueue_many(
            payload.account_ids,
            group_id=payload.group_id,
            force=payload.force,
        ),
    }


@router.post("/api/accounts/sub2api/backfill")
def api_accounts_sub2api_backfill(
    request: Request,
    limit: int = Query(0, ge=0, le=5000),
    group_id: int = Query(0, ge=0),
    force: bool = Query(False),
) -> dict[str, Any]:
    check_auth(request)
    from ._sub2api_runtime import sub2api_import_runtime

    return {
        "ok": True,
        "job": sub2api_import_runtime.enqueue_backfill(
            limit=limit, group_id=group_id, force=force
        ),
    }


@router.get("/api/accounts/sub2api/jobs/{job_id}")
def api_accounts_sub2api_job(request: Request, job_id: str) -> dict[str, Any]:
    check_auth(request)
    from ._sub2api_runtime import sub2api_import_runtime

    job = sub2api_import_runtime.job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Sub2API import job not found")
    return {"job": job}


@router.post("/api/accounts/sub2api/oauth/start")
def api_accounts_sub2api_oauth_start(
    request: Request, payload: Sub2ApiOAuthStartRequest
) -> dict[str, Any]:
    check_auth(request)
    from core.sub2api_client import start_oauth

    result = start_oauth(**_sub2api_client_config())
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    result["email"] = payload.email
    return result


@router.post("/api/accounts/sub2api/oauth/complete")
def api_accounts_sub2api_oauth_complete(
    request: Request, payload: Sub2ApiOAuthCompleteRequest
) -> dict[str, Any]:
    check_auth(request)
    from core.sub2api_client import complete_oauth

    result = complete_oauth(
        session_id=payload.session_id,
        state=payload.state,
        callback=payload.callback,
        email=payload.email,
        group_id=payload.group_id,
        **_sub2api_client_config(),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result


@router.post("/api/accounts/{account_id}/mint-cpa")
def api_account_mint_cpa(
    request: Request,
    account_id: int,
    force: bool = Query(False),
) -> dict[str, Any]:
    check_auth(request)
    row = fetch_one("SELECT id, platform, sso FROM accounts WHERE id = ?", (account_id,))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")
    if row["platform"] != "grok" or not row["sso"]:
        raise HTTPException(status_code=400, detail="仅支持有 SSO 的 Grok 账号")
    from ._cpa_runtime import cpa_mint_runtime

    queued = cpa_mint_runtime.enqueue(account_id, force=force)
    return {"ok": queued, "queued": queued, "account_id": account_id}


@router.patch("/api/accounts/{account_id}")
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
        session_token=payload.session_token,
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        id_token=payload.id_token,
    )
    return {"account": row}


@router.delete("/api/accounts/{account_id}")
def api_account_delete(request: Request, account_id: int) -> dict[str, Any]:
    check_auth(request)
    account_delete(account_id)
    return {"ok": True}


@router.post("/api/accounts/{account_id}/query-state")
def api_account_query_state(request: Request, account_id: int) -> dict[str, Any]:
    """调用平台的 query_state action 查询账号状态/套餐/额度，结果写回 DB。"""
    check_auth(request)
    from _vendor_aar.infrastructure.platform_runtime import PlatformRuntime
    from _vendor_aar.domain.actions import ActionExecutionCommand

    row = fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")

    platform = row["platform"] or "grok"

    # grok / openblocklabs 等没有 query_state API 的平台直接返回
    _no_query_platforms = {"grok", "openblocklabs", "cerebras", "blink", "anything"}
    if platform in _no_query_platforms:
        return {"ok": False, "error": f"{platform} 平台不支持查询状态"}

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

        # 不用 unknown 覆盖已有的有效值
        existing_plan = row["plan_state"] or "unknown"
        existing_validity = row["validity_status"] or "unknown"
        if plan_state == "unknown" and existing_plan != "unknown":
            plan_state = existing_plan
        if validity == "unknown" and existing_validity != "unknown":
            validity = existing_validity

        execute_no_return(
            """UPDATE accounts SET extra_json=?, plan_state=?, validity_status=?, last_checked_at=? WHERE id=?""",
            (extra_json, plan_state, validity, now_iso(), account_id),
        )
        return {"ok": True, "data": result.data, "plan_state": plan_state, "validity_status": validity}

    return {"ok": False, "error": result.error or "查询失败"}

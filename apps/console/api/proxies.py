"""
代理池路由（覆盖旧实现，等价于 app.py 原 `/api/proxies/*` 端点）。

- GET    /api/proxies
- POST   /api/proxies
- PATCH  /api/proxies/{id}
- DELETE /api/proxies/{id}
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ._shared import (
    ProxyItem,
    ProxyUpdate,
    check_auth,
    proxy_add,
    proxy_delete,
    proxy_list,
    proxy_update,
)

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


@router.get("")
def api_list_proxies(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"proxies": proxy_list()}


@router.post("")
def api_add_proxy(request: Request, payload: ProxyItem) -> dict[str, Any]:
    check_auth(request)
    return {"proxy": proxy_add(payload.url, payload.label, payload.enabled)}


@router.patch("/{proxy_id}")
def api_update_proxy(request: Request, proxy_id: int, payload: ProxyUpdate) -> dict[str, Any]:
    check_auth(request)
    return {"proxy": proxy_update(proxy_id, payload.label, payload.enabled, payload.reset_stats)}


@router.delete("/{proxy_id}")
def api_delete_proxy(request: Request, proxy_id: int) -> dict[str, Any]:
    check_auth(request)
    proxy_delete(proxy_id)
    return {"ok": True}

"""Automatic account-inventory replenishment controls."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ._delivery_runtime import DeliveryUnauthorized, DeliveryUnavailable, check_internal_bearer
from ._inventory_runtime import auto_replenish_runtime
from ._shared import check_auth


router = APIRouter(prefix="/api/inventory", tags=["inventory"])
internal_router = APIRouter(prefix="/api/internal/inventory", tags=["internal-inventory"])


class AutoReplenishUpdate(BaseModel):
    enabled: bool | None = None
    threshold: int | None = Field(None, ge=1, le=100000)
    replenish_count: int | None = Field(None, ge=1, le=5000)
    check_interval_seconds: int | None = Field(None, ge=10, le=3600)
    cooldown_seconds: int | None = Field(None, ge=0, le=86400)


def _apply_update(payload: AutoReplenishUpdate) -> dict[str, Any]:
    config = auto_replenish_runtime.save_config(payload.model_dump(exclude_none=True))
    result = auto_replenish_runtime.check_now(force=True) if config["enabled"] else None
    return {"config": config, "check": result, "status": auto_replenish_runtime.snapshot()}


def _check_internal_request(request: Request) -> None:
    try:
        check_internal_bearer(request.headers.get("Authorization", ""))
    except DeliveryUnauthorized as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except DeliveryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/auto-replenish")
def get_auto_replenish(request: Request) -> dict[str, Any]:
    check_auth(request)
    return auto_replenish_runtime.snapshot()


@router.post("/auto-replenish")
def update_auto_replenish(request: Request, payload: AutoReplenishUpdate) -> dict[str, Any]:
    check_auth(request)
    return _apply_update(payload)


@router.post("/auto-replenish/check")
def check_auto_replenish(request: Request) -> dict[str, Any]:
    check_auth(request)
    return auto_replenish_runtime.check_now()


@internal_router.get("/auto-replenish")
def get_internal_auto_replenish(request: Request) -> dict[str, Any]:
    _check_internal_request(request)
    return auto_replenish_runtime.snapshot()


@internal_router.post("/auto-replenish")
def update_internal_auto_replenish(
    request: Request,
    payload: AutoReplenishUpdate,
) -> dict[str, Any]:
    _check_internal_request(request)
    return _apply_update(payload)

"""
导出器路由。

对应 Requirement 10 AC1-AC8：Exporter 管理。
- GET / — 已加载 Exporter 列表
- PATCH /{id}/config — 更新 Exporter 配置
- POST /{id}/test — 测试推送
- POST /{id}/push — 手动推送指定账号
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.registry import EXPORTER_REGISTRY
from core.base_exporter import BaseExporter, ExporterConfig, get_exporter, list_exporters
from data import dao

router = APIRouter(prefix="/exporters", tags=["exporters"])


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


class ExporterConfigUpdate(BaseModel):
    """更新 Exporter 配置请求。"""
    enabled: bool = True
    endpoint: str = ""
    api_append: bool = True
    template: str = ""
    extra: Dict[str, Any] = {}


class ExporterTestRequest(BaseModel):
    """测试推送请求。"""
    test_data: Dict[str, Any] = {}


class ExporterPushRequest(BaseModel):
    """手动推送请求。"""
    account_ids: List[int]


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/")
async def list_all_exporters():
    """返回已加载 Exporter 列表（含配置状态）。"""
    exporters_meta = list_exporters()
    settings = dao.get_all_settings()

    results = []
    for meta in exporters_meta:
        exporter_id = meta["id"]
        config_raw = settings.get(f"exporter_{exporter_id}", "{}")
        config = {}
        try:
            config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
        except (json.JSONDecodeError, TypeError):
            pass

        results.append({
            "id": exporter_id,
            "display_name": meta.get("display_name", ""),
            "description": meta.get("description", ""),
            "config_schema": meta.get("config_schema", {}),
            "enabled": config.get("enabled", False),
            "config": config,
        })

    return results


@router.patch("/{exporter_id}/config")
async def update_exporter_config(exporter_id: str, body: ExporterConfigUpdate):
    """更新 Exporter 配置。"""
    if not EXPORTER_REGISTRY.exists(exporter_id):
        raise HTTPException(status_code=404, detail=f"Exporter '{exporter_id}' 未注册")

    config_data = {
        "enabled": body.enabled,
        "endpoint": body.endpoint,
        "api_append": body.api_append,
        "template": body.template,
        "extra": body.extra,
    }
    dao.upsert_setting(f"exporter_{exporter_id}", json.dumps(config_data, ensure_ascii=False))

    return {"ok": True, "exporter_id": exporter_id, "config": config_data}


@router.post("/{exporter_id}/test")
async def test_exporter(exporter_id: str, body: ExporterTestRequest):
    """测试推送（使用测试数据）。"""
    exporter = get_exporter(exporter_id)
    if exporter is None:
        raise HTTPException(status_code=404, detail=f"Exporter '{exporter_id}' 未注册")

    # 加载配置
    settings = dao.get_all_settings()
    config_raw = settings.get(f"exporter_{exporter_id}", "{}")
    config_dict = {}
    try:
        config_dict = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except (json.JSONDecodeError, TypeError):
        pass

    config = ExporterConfig(
        exporter_id=exporter_id,
        enabled=config_dict.get("enabled", False),
        endpoint=config_dict.get("endpoint", ""),
        api_append=config_dict.get("api_append", True),
        template=config_dict.get("template", ""),
        extra=config_dict.get("extra", {}),
    )

    # 使用测试数据或默认测试数据
    test_data = body.test_data or {
        "platform": "test",
        "email": "test@example.com",
        "password": "test123",
        "sso": "test_token_xxx",
    }

    try:
        result = exporter.push(test_data, config)
        return {
            "ok": result.success,
            "exporter_id": exporter_id,
            "message": result.message,
            "data": result.data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"测试推送失败: {e}")


@router.post("/{exporter_id}/push")
async def push_accounts(exporter_id: str, body: ExporterPushRequest):
    """手动推送指定账号到 Exporter。"""
    exporter = get_exporter(exporter_id)
    if exporter is None:
        raise HTTPException(status_code=404, detail=f"Exporter '{exporter_id}' 未注册")

    if not body.account_ids:
        raise HTTPException(status_code=400, detail="account_ids 不能为空")

    # 加载配置
    settings = dao.get_all_settings()
    config_raw = settings.get(f"exporter_{exporter_id}", "{}")
    config_dict = {}
    try:
        config_dict = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except (json.JSONDecodeError, TypeError):
        pass

    config = ExporterConfig(
        exporter_id=exporter_id,
        enabled=config_dict.get("enabled", True),
        endpoint=config_dict.get("endpoint", ""),
        api_append=config_dict.get("api_append", True),
        template=config_dict.get("template", ""),
        extra=config_dict.get("extra", {}),
    )

    # 逐个推送
    results = {"success": 0, "failed": 0, "errors": []}
    accounts, _ = dao.list_accounts(limit=99999)
    account_map = {a.id: a for a in accounts}

    for aid in body.account_ids:
        account = account_map.get(aid)
        if account is None:
            results["errors"].append({"account_id": aid, "error": "账号不存在"})
            results["failed"] += 1
            continue

        extra = {}
        try:
            extra = json.loads(account.extra_json) if account.extra_json else {}
        except (json.JSONDecodeError, TypeError):
            pass

        account_data = {
            "platform": account.platform,
            "email": account.email,
            "password": account.password,
            "sso": account.sso,
            "user_id": account.user_id,
            "extra": extra,
        }

        try:
            push_result = exporter.push(account_data, config)
            if push_result.success:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append({"account_id": aid, "error": push_result.message})
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"account_id": aid, "error": str(e)})

    return {"ok": True, "exporter_id": exporter_id, **results}

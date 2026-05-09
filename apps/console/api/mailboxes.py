"""
邮箱 Provider 路由。

对应 Requirement 3 AC1-AC5：邮箱 Provider 管理。
- GET / — 列表
- POST / — 新增
- PATCH /{id} — 更新
- DELETE /{id} — 删除
- POST /{id}/test — 连通性探测
- GET /{id}/domains — 可用域名列表
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.registry import MAILBOX_REGISTRY
from data import dao
from data.models import MailboxProviderModel

router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


# ─── 请求/响应模型 ────────────────────────────────────────────────────────────


class MailboxCreateRequest(BaseModel):
    """新增邮箱 Provider 请求。"""
    name: str
    provider_type: str = "tmail"
    enabled: bool = True
    config: Dict[str, Any] = {}


class MailboxUpdateRequest(BaseModel):
    """更新邮箱 Provider 请求。"""
    name: Optional[str] = None
    provider_type: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/")
async def list_mailboxes():
    """获取所有邮箱 Provider 列表。"""
    providers = dao.list_mailbox_providers()
    results = []
    for p in providers:
        config = {}
        try:
            config = json.loads(p.config_json) if p.config_json else {}
        except (json.JSONDecodeError, TypeError):
            pass
        results.append({
            "id": p.id,
            "name": p.name,
            "provider_type": p.provider_type,
            "enabled": p.enabled,
            "config": config,
            "success_count": p.success_count,
            "failure_count": p.failure_count,
            "consecutive_failures": p.consecutive_failures,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        })
    return results


@router.post("/")
async def create_mailbox(body: MailboxCreateRequest):
    """新增邮箱 Provider。"""
    # 校验 provider_type 是否已注册
    if not MAILBOX_REGISTRY.exists(body.provider_type):
        # 允许自定义类型，仅警告
        pass

    with dao.get_session() as s:
        model = MailboxProviderModel(
            name=body.name,
            provider_type=body.provider_type,
            enabled=body.enabled,
            config_json=json.dumps(body.config, ensure_ascii=False),
        )
        s.add(model)
        s.commit()
        s.refresh(model)

    return {"ok": True, "id": model.id, "name": model.name}


@router.patch("/{provider_id}")
async def update_mailbox(provider_id: int, body: MailboxUpdateRequest):
    """更新邮箱 Provider。"""
    with dao.get_session() as s:
        model = s.get(MailboxProviderModel, provider_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"邮箱 Provider {provider_id} 不存在")

        if body.name is not None:
            model.name = body.name
        if body.provider_type is not None:
            model.provider_type = body.provider_type
        if body.enabled is not None:
            model.enabled = body.enabled
        if body.config is not None:
            model.config_json = json.dumps(body.config, ensure_ascii=False)

        model.updated_at = datetime.now(timezone.utc).isoformat()
        s.add(model)
        s.commit()
        s.refresh(model)

    return {"ok": True, "id": model.id}


@router.delete("/{provider_id}")
async def delete_mailbox(provider_id: int):
    """删除邮箱 Provider。"""
    with dao.get_session() as s:
        model = s.get(MailboxProviderModel, provider_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"邮箱 Provider {provider_id} 不存在")
        s.delete(model)
        s.commit()

    return {"ok": True, "id": provider_id}


@router.post("/{provider_id}/test")
async def test_mailbox(provider_id: int):
    """连通性探测。"""
    with dao.get_session() as s:
        model = s.get(MailboxProviderModel, provider_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"邮箱 Provider {provider_id} 不存在")

    # 获取对应的 Provider 类
    provider_cls = MAILBOX_REGISTRY.get(model.provider_type)
    if provider_cls is None:
        raise HTTPException(status_code=400, detail=f"Provider 类型 '{model.provider_type}' 未注册")

    config = {}
    try:
        config = json.loads(model.config_json) if model.config_json else {}
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        instance = provider_cls()
        if hasattr(instance, "test_connection"):
            result = instance.test_connection(config)
            return {"ok": True, "result": result}
        else:
            return {"ok": True, "result": "Provider 未实现 test_connection 方法，跳过"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连通性探测失败: {e}")


@router.get("/{provider_id}/domains")
async def get_domains(provider_id: int):
    """获取可用域名列表。"""
    with dao.get_session() as s:
        model = s.get(MailboxProviderModel, provider_id)
        if model is None:
            raise HTTPException(status_code=404, detail=f"邮箱 Provider {provider_id} 不存在")

    provider_cls = MAILBOX_REGISTRY.get(model.provider_type)
    if provider_cls is None:
        raise HTTPException(status_code=400, detail=f"Provider 类型 '{model.provider_type}' 未注册")

    config = {}
    try:
        config = json.loads(model.config_json) if model.config_json else {}
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        instance = provider_cls()
        if hasattr(instance, "list_domains"):
            domains = instance.list_domains(config)
            return {"ok": True, "domains": domains}
        else:
            return {"ok": True, "domains": []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取域名列表失败: {e}")

"""
验证码路由。

对应 Requirement 5 AC1-AC5：验证码 Provider + Strategy 管理。
- GET /providers — 已注册的 provider 列表
- GET /strategies — 已注册的 strategy 列表
- POST /test — 测试指定 provider 的余额/连通性
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.registry import CAPTCHA_REGISTRY, STRATEGY_REGISTRY

router = APIRouter(prefix="/captcha", tags=["captcha"])


# ─── 请求模型 ─────────────────────────────────────────────────────────────────


class CaptchaTestRequest(BaseModel):
    """测试验证码 Provider 请求。"""
    provider_name: str
    config: Dict[str, Any] = {}


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.get("/providers")
async def list_providers():
    """返回已注册的验证码 Provider 列表。"""
    results = []
    for cls in CAPTCHA_REGISTRY.list_all():
        instance = cls()
        info = {
            "name": getattr(instance, "name", ""),
            "display_name": getattr(instance, "display_name", getattr(instance, "name", "")),
            "description": getattr(instance, "description", ""),
            "supported_types": getattr(instance, "supported_types", []),
        }
        # 附加配置 schema（如果有）
        if hasattr(instance, "config_schema"):
            info["config_schema"] = instance.config_schema
        results.append(info)
    return results


@router.get("/strategies")
async def list_strategies():
    """返回已注册的验证码 Strategy 列表。"""
    results = []
    for cls in STRATEGY_REGISTRY.list_all():
        instance = cls()
        info = {
            "name": getattr(instance, "name", ""),
            "display_name": getattr(instance, "display_name", getattr(instance, "name", "")),
            "description": getattr(instance, "description", ""),
            "priority_order": getattr(instance, "priority_order", []),
        }
        results.append(info)
    return results


@router.post("/test")
async def test_provider(body: CaptchaTestRequest):
    """测试指定验证码 Provider 的余额/连通性。"""
    cls = CAPTCHA_REGISTRY.get(body.provider_name)
    if cls is None:
        raise HTTPException(status_code=404, detail=f"验证码 Provider '{body.provider_name}' 未注册")

    instance = cls()

    # 尝试调用 test / check_balance 方法
    try:
        if hasattr(instance, "test_connection"):
            result = instance.test_connection(body.config)
            return {"ok": True, "provider": body.provider_name, "result": result}
        elif hasattr(instance, "check_balance"):
            balance = instance.check_balance(body.config)
            return {"ok": True, "provider": body.provider_name, "balance": balance}
        else:
            return {"ok": True, "provider": body.provider_name, "result": "Provider 未实现测试方法"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"测试失败: {e}")

"""
认证路由。

对应 Requirement 14 AC3：Bearer auth 认证机制。
- POST /login — 校验密码，返回 token
- GET /verify — 校验 Bearer token 有效性
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

CONSOLE_PASSWORD = os.getenv("GROK_REGISTER_CONSOLE_PASSWORD", "")


# ─── 请求/响应模型 ────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    """登录请求体。"""
    password: str


class LoginResponse(BaseModel):
    """登录响应体。"""
    ok: bool
    token: str = ""
    message: str = ""


class VerifyResponse(BaseModel):
    """验证响应体。"""
    ok: bool
    message: str = ""


# ─── 路由 ─────────────────────────────────────────────────────────────────────


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest):
    """
    登录接口：校验密码并返回 token。

    如果未设置密码（环境变量为空），则任何密码都通过。
    """
    if not CONSOLE_PASSWORD:
        return LoginResponse(ok=True, token="no-auth-required", message="未设置密码，免认证模式")

    if body.password != CONSOLE_PASSWORD:
        raise HTTPException(status_code=401, detail="密码错误")

    return LoginResponse(ok=True, token=CONSOLE_PASSWORD)


@router.get("/verify", response_model=VerifyResponse)
async def verify(authorization: str = Header(default="")):
    """
    验证 Bearer token 是否有效。

    前端可在页面加载时调用此接口确认登录状态。
    """
    if not CONSOLE_PASSWORD:
        return VerifyResponse(ok=True, message="免认证模式")

    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供认证令牌")

    token = authorization[7:]
    if token != CONSOLE_PASSWORD:
        raise HTTPException(status_code=401, detail="认证令牌无效")

    return VerifyResponse(ok=True, message="认证有效")

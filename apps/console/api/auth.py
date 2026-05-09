"""
认证路由（覆盖旧实现）。

对应 app.py 旧端点：
- POST /api/login
- GET  /api/auth/debug
- GET  /api/auth/status
- GET  /login（兼容旧链接，HTML 重定向）

复用 app.py 原逻辑，以 check_auth 进行 Bearer / Cookie 双通道认证。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ._shared import CONSOLE_PASSWORD

# 两个 router 分别挂载不同前缀，app.py 里 include 时处理
router = APIRouter(tags=["auth"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """兼容旧链接：统一重定向到新前端 /sign-in 路由。"""
    return HTMLResponse(status_code=302, headers={"Location": "/sign-in"})


@router.post("/api/login")
def api_login(payload: dict) -> dict[str, Any]:
    """
    登录接口（保留旧契约）。
    请求体：{"password": "xxx"}
    未启用密码时直接 success。
    """
    if not CONSOLE_PASSWORD:
        return {"success": True}
    password = str(payload.get("password", "")).strip()
    if password == CONSOLE_PASSWORD:
        return {"success": True}
    import logging
    logging.getLogger("uvicorn").warning(
        "[api_login] password mismatch: input_len=%d, expected_len=%d, "
        "input_first_char=%r, expected_first_char=%r",
        len(password),
        len(CONSOLE_PASSWORD),
        password[:1] if password else "",
        CONSOLE_PASSWORD[:1] if CONSOLE_PASSWORD else "",
    )
    raise HTTPException(status_code=401, detail="Invalid password")


@auth_router.get("/debug")
def api_auth_debug(request: Request) -> dict[str, Any]:
    """临时调试接口：返回后端实际使用的密码长度和首尾字符。不暴露明文。"""
    pw = CONSOLE_PASSWORD or ""
    return {
        "password_configured": bool(pw),
        "password_length": len(pw),
        "password_first_char": pw[:1] if pw else None,
        "password_last_char": pw[-1:] if pw else None,
        "env_var_name": "GROK_REGISTER_CONSOLE_PASSWORD",
    }


@auth_router.get("/status")
def api_auth_status(request: Request) -> dict[str, Any]:
    """检查当前会话是否已认证。前端启动时用来判断是否跳登录页。"""
    if not CONSOLE_PASSWORD:
        return {"auth_required": False, "authenticated": True}
    auth = request.headers.get("Authorization", "")
    cookie = request.cookies.get("console_password", "")
    authenticated = auth == f"Bearer {CONSOLE_PASSWORD}" or cookie == CONSOLE_PASSWORD
    return {"auth_required": True, "authenticated": authenticated}

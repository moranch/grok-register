"""
前端入口与 SPA fallback 路由（从 app.py 抽出）。

- GET /          返回前端 index.html
- GET /{path}    SPA fallback（/api 路径除外）

注意：SPA fallback 必须放在所有 /api/* 路由之后挂载。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ._shared import WEBUI_DIR

root_router = APIRouter(tags=["frontend"])
spa_router = APIRouter(tags=["frontend"])


@root_router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """返回前端 index.html。登录重定向由前端自己处理（通过 /api/auth/status 检查）。"""
    new_frontend = WEBUI_DIR / "index.html"
    if new_frontend.exists():
        return HTMLResponse(content=new_frontend.read_text(encoding="utf-8"))
    return HTMLResponse(
        status_code=503,
        content=(
            "<h1>前端资源未就绪</h1>"
            f"<p>未在 <code>{WEBUI_DIR}/index.html</code> 发现前端产物，"
            "请检查 Dockerfile 中 <code>grok-register-ui</code> 的构建步骤是否成功。</p>"
        ),
    )


@spa_router.get("/{full_path:path}", response_class=HTMLResponse)
def spa_fallback(full_path: str) -> HTMLResponse:
    """SPA fallback：把所有未匹配到的 GET 请求都指向 index.html，让前端路由接管。"""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail=f"API 路由不存在: /{full_path}")
    new_frontend = WEBUI_DIR / "index.html"
    if new_frontend.exists():
        return HTMLResponse(content=new_frontend.read_text(encoding="utf-8"))
    return HTMLResponse(
        status_code=503,
        content=(
            "<h1>前端资源未就绪</h1>"
            f"<p>未在 <code>{WEBUI_DIR}/index.html</code> 发现前端产物，"
            "请检查 Dockerfile 中 <code>grok-register-ui</code> 的构建步骤是否成功。</p>"
        ),
    )

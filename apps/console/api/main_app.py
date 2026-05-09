"""
FastAPI 应用入口。

对应 Requirement 14 AC1-AC5：Web Console 后端服务。

- lifespan 中初始化数据库、加载所有注册表、启动 LifecycleWorker
- 挂载所有 APIRouter
- 静态文件托管
- Bearer auth middleware
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.auth import router as auth_router
from api.platforms import router as platforms_router
from api.tasks import router as tasks_router
from api.accounts import router as accounts_router
from api.mailboxes import router as mailboxes_router
from api.captcha import router as captcha_router
from api.proxies import router as proxies_router
from api.exporters import router as exporters_router
from api.lifecycle import router as lifecycle_router
from api.stats import router as stats_router
from api.settings import router as settings_router, system_router
from core.lifecycle import LifecycleWorker
from core.registry import load_all, PLATFORM_REGISTRY
from data import dao
from data.migrations import migrate

logger = logging.getLogger(__name__)

# 全局引用，供其他模块访问
lifecycle_worker: LifecycleWorker | None = None
lifecycle_task: asyncio.Task | None = None

DB_PATH = os.getenv("DATABASE_PATH", "data/console.db")
CONSOLE_PASSWORD = os.getenv("GROK_REGISTER_CONSOLE_PASSWORD", "")
WEBUI_DIR = os.getenv("WEBUI_DIR", "/opt/webui")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。"""
    global lifecycle_worker, lifecycle_task

    # 1. 执行数据库迁移
    logger.info("[Startup] 执行数据库迁移...")
    migrate.run_migrations(DB_PATH)

    # 2. 初始化 SQLModel 引擎
    logger.info("[Startup] 初始化数据库连接...")
    dao.init_db()

    # 3. 加载所有插件（platforms / providers / exporters）
    logger.info("[Startup] 加载插件注册表...")
    try:
        import platforms as platforms_pkg
        load_all(platforms_pkg)
    except ImportError:
        logger.warning("[Startup] platforms 包未找到，跳过平台插件加载")

    try:
        import providers as providers_pkg
        load_all(providers_pkg)
    except ImportError:
        logger.warning("[Startup] providers 包未找到，跳过 Provider 插件加载")

    try:
        import exporters as exporters_pkg
        load_all(exporters_pkg)
    except ImportError:
        logger.warning("[Startup] exporters 包未找到，跳过 Exporter 插件加载")

    # 4. 启动 LifecycleWorker
    logger.info("[Startup] 启动 LifecycleWorker...")
    lifecycle_worker = LifecycleWorker(
        check_hours=6,
        refresh_hours=12,
        get_accounts=_get_lifecycle_accounts,
        get_platform=_get_platform_instance,
        update_account=_update_account_for_lifecycle,
        write_event=dao.write_event,
    )
    lifecycle_task = asyncio.create_task(lifecycle_worker.start())

    logger.info("[Startup] 应用启动完成")
    yield

    # 5. 停止 LifecycleWorker
    logger.info("[Shutdown] 停止 LifecycleWorker...")
    if lifecycle_worker:
        await lifecycle_worker.stop()
    if lifecycle_task and not lifecycle_task.done():
        lifecycle_task.cancel()
        try:
            await lifecycle_task
        except asyncio.CancelledError:
            pass

    logger.info("[Shutdown] 应用已关闭")


def _get_lifecycle_accounts():
    """获取需要生命周期检测的账号列表。"""
    accounts, _ = dao.list_accounts(lifecycle_status="active", limit=9999)
    result = []
    for a in accounts:
        result.append({
            "id": a.id,
            "platform": a.platform,
            "email": a.email,
            "sso": a.sso,
            "lifecycle_status": a.lifecycle_status,
            "extra_json": a.extra_json,
        })
    return result


def _get_platform_instance(platform_name: str):
    """根据平台名获取 BasePlatform 实例。"""
    cls = PLATFORM_REGISTRY.get(platform_name)
    if cls is None:
        return None
    return cls()


def _update_account_for_lifecycle(account_id: int, updates: dict):
    """更新账号字段（供 LifecycleWorker 回调）。"""
    dao.update_account(account_id, updates)


# ─── 创建 FastAPI 实例 ────────────────────────────────────────────────────────

app = FastAPI(
    title="Multi-Platform Account Registrar",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Auth 中间件 ──────────────────────────────────────────────────────────────


@app.middleware("http")
async def auth_middleware(request: Request, call_next) -> Response:
    """Bearer token 认证中间件。"""
    if not CONSOLE_PASSWORD:
        return await call_next(request)

    path = request.url.path

    # 排除不需要认证的路径
    if not path.startswith("/api"):
        return await call_next(request)
    if path in ("/api/auth/login", "/api/health/ping"):
        return await call_next(request)

    # 校验 Authorization header
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "未提供认证令牌"})

    token = auth_header[7:]
    if token != CONSOLE_PASSWORD:
        return JSONResponse(status_code=401, content={"detail": "认证令牌无效"})

    return await call_next(request)


# ─── 挂载路由 ─────────────────────────────────────────────────────────────────

app.include_router(auth_router, prefix="/api")
app.include_router(platforms_router, prefix="/api")
app.include_router(tasks_router, prefix="/api")
app.include_router(accounts_router, prefix="/api")
app.include_router(mailboxes_router, prefix="/api")
app.include_router(captcha_router, prefix="/api")
app.include_router(proxies_router, prefix="/api")
app.include_router(exporters_router, prefix="/api")
app.include_router(lifecycle_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(system_router, prefix="/api")


# ─── 健康检查 ─────────────────────────────────────────────────────────────────


@app.get("/api/health/ping")
async def health_ping():
    """健康检查端点。"""
    return {"status": "ok", "version": "2.0.0"}


# ─── 静态文件托管 ─────────────────────────────────────────────────────────────

if Path(WEBUI_DIR).is_dir():
    app.mount("/", StaticFiles(directory=WEBUI_DIR, html=True), name="webui")


# ─── 入口 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("api.main_app:app", host=host, port=port, reload=False)

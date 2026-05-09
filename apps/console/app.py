"""
Grok Register Console - FastAPI 主入口。

本文件仅负责：
1. 创建 FastAPI 实例 + lifespan（初始化 DB、启动 supervisor / lifecycle 线程、加载插件）
2. 挂载静态资源 + SPA fallback
3. include 各个 api/*.py 子 router

所有业务路由、Pydantic 模型、数据库辅助函数、后台 worker 已下沉到 apps/console/api/ 模块。
保持行为等价：docker restart 后所有 HTTP 端点、SSE 流、后台线程与重构前一致。
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# 确保 api/ 与 core/ / platforms/ / providers/ / exporters/ 都在 sys.path 上
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from api._shared import WEBUI_DIR, init_db, seed_mailbox_from_defaults
from api._supervisor import supervisor
from api._lifecycle_runtime import start_lifecycle_thread


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动阶段按顺序初始化，关闭阶段逆序清理。"""
    # 1. 数据库建表 + 种子数据
    init_db()
    # 同时确保 vendor 的 SQLModel 表存在，并 seed 内置 provider definitions
    # （captcha / mailbox / proxy 的驱动清单都在 vendor 的 _BUILTIN_DEFINITIONS 里）
    try:
        from core._vendor_aar.db import init_db as _vendor_init_db
        _vendor_init_db()
    except Exception as exc:
        print(f"[WARN] vendor init_db 失败（provider_definitions 可能未 seed）: {exc}")
    try:
        seed_mailbox_from_defaults(force=False)
    except Exception:
        pass

    # 2. 任务 supervisor
    supervisor.start()

    # 3. 生命周期后台线程
    start_lifecycle_thread()

    # 4. 多平台插件加载（按 design 顺序：platform → mailbox → captcha → strategy → exporter）
    try:
        from core.registry import (
            PLATFORM_REGISTRY,
            MAILBOX_REGISTRY,
            CAPTCHA_REGISTRY,
            STRATEGY_REGISTRY,
            EXPORTER_REGISTRY,
            load_all,
        )
        import platforms as _platforms_pkg
        load_all(_platforms_pkg)
        import providers as _providers_pkg
        load_all(_providers_pkg)
        import exporters as _exporters_pkg
        load_all(_exporters_pkg)
        print(
            f"[OK] 多平台插件已加载: "
            f"platforms={PLATFORM_REGISTRY.list_names()}, "
            f"mailbox={MAILBOX_REGISTRY.list_names()}, "
            f"captcha={CAPTCHA_REGISTRY.list_names()}, "
            f"strategy={STRATEGY_REGISTRY.list_names()}, "
            f"exporters={EXPORTER_REGISTRY.list_names()}"
        )
    except Exception as exc:
        # 插件加载失败不阻塞应用启动（保持与旧行为一致）
        print(f"[WARN] 多平台插件加载失败（不影响旧功能）: {exc}")

    try:
        yield
    finally:
        supervisor.stop()


app = FastAPI(title="Grok Register Console", lifespan=lifespan)

# ─── 挂载静态资源 ─────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")

# ─── 挂载 api/*.py 子路由 ─────────────────────────────────────────────
# 遵循原端点路径不变的约束：
# - 路由模块内带有 `/api/...` 完整前缀时直接 include（auth / tasks / proxies / ...）
# - 插件系统旧子 router（platforms / exporters / captcha）仅声明短前缀，由此处加 /api
from api.auth import auth_router as _auth_sub_router
from api.auth import router as _auth_root_router
from api.tasks import router as _tasks_router
from api.proxies import router as _proxies_router
from api.mailboxes import router as _mailboxes_router
from api.accounts import router as _accounts_router
from api.stats import router as _stats_router
from api.lifecycle import router as _lifecycle_router
from api.settings import router as _settings_router
from api.system import router as _system_router
from api.frontend import root_router as _frontend_root_router
from api.frontend import spa_router as _frontend_spa_router

# 1) /login（HTML 重定向）+ /api/login
app.include_router(_auth_root_router)
# 2) /api/auth/*
app.include_router(_auth_sub_router, prefix="/api")

# 3) 各资源 api/*.py（其 router 本身已带 `/api/...` 完整前缀）
app.include_router(_tasks_router)
app.include_router(_proxies_router)
app.include_router(_mailboxes_router)
app.include_router(_accounts_router)
app.include_router(_stats_router)
app.include_router(_lifecycle_router)
app.include_router(_settings_router)
app.include_router(_system_router)

# 4) 插件系统子 router（短前缀 + 本处补 /api）
try:
    from api.platforms import router as _platforms_router
    app.include_router(_platforms_router, prefix="/api")
except Exception as exc:
    print(f"[WARN] /api/platforms 挂载失败: {exc}")
try:
    from api.exporters import router as _exporters_router
    app.include_router(_exporters_router, prefix="/api")
except Exception as exc:
    print(f"[WARN] /api/exporters 挂载失败: {exc}")
try:
    from api.captcha import router as _captcha_router
    app.include_router(_captcha_router, prefix="/api")
except Exception as exc:
    print(f"[WARN] /api/captcha 挂载失败: {exc}")

# 5) 根路径 + SPA fallback（必须最后挂载）
app.include_router(_frontend_root_router)
app.include_router(_frontend_spa_router)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("GROK_REGISTER_CONSOLE_HOST", "127.0.0.1")
    port = int(os.getenv("GROK_REGISTER_CONSOLE_PORT", "18600"))
    uvicorn.run("app:app", host=host, port=port, reload=False)

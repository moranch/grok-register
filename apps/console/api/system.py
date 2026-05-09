"""
系统路由（从旧 app.py 抽出）。

对应 app.py 旧端点：
- GET  /api/meta
- GET  /api/health
- GET  /api/system/info
- POST /api/system/cleanup
- GET  /api/system/export-config
- POST /api/system/import-config
"""
from __future__ import annotations

import json
import platform as _platform
import sys as _sys
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from ._shared import (
    CONSOLE_PASSWORD,
    DB_PATH,
    MAX_CONCURRENT_TASKS,
    MailboxItem,
    RUNTIME_DIR,
    SOURCE_PROJECT,
    SOURCE_VENV_PYTHON,
    SystemSettings,
    TASKS_DIR,
    WEBUI_DIR,
    check_auth,
    delete_task_files,
    execute_no_return,
    fetch_all,
    fetch_one,
    mailbox_add,
    mailbox_list,
    merged_defaults,
    now_iso,
    proxy_add,
    proxy_list,
    read_settings,
    run_health_checks,
    write_settings,
)

router = APIRouter(tags=["system"])


@router.get("/api/meta")
def api_meta(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {
        "defaults": merged_defaults(),
        "settings": read_settings(),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@router.get("/api/health")
def api_health(request: Request) -> dict[str, Any]:
    check_auth(request)
    return run_health_checks()


@router.get("/api/system/info")
def api_system_info(request: Request) -> dict[str, Any]:
    """系统信息：应用版本 / python 版本 / 运行目录 / 已处理事件数 / 账号总数 等。"""
    check_auth(request)

    def _one(sql: str) -> int:
        r = fetch_one(sql)
        return int(r["c"]) if r else 0

    db_size = 0
    try:
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except Exception:
        db_size = 0

    tasks_size = 0
    try:
        if TASKS_DIR.exists():
            for child in TASKS_DIR.rglob("*"):
                try:
                    if child.is_file():
                        tasks_size += child.stat().st_size
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "app_name": "Grok Register Console",
        "app_version": "1.1.0",
        "python_version": _sys.version.split()[0],
        "platform": _platform.platform(),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "runtime_dir": str(RUNTIME_DIR),
        "tasks_dir": str(TASKS_DIR),
        "db_path": str(DB_PATH),
        "db_size_bytes": db_size,
        "tasks_size_bytes": tasks_size,
        "auth_required": bool(CONSOLE_PASSWORD),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
        "webui_dir": str(WEBUI_DIR),
        "counts": {
            "tasks": _one("SELECT COUNT(*) AS c FROM tasks"),
            "events": _one("SELECT COUNT(*) AS c FROM register_events"),
            "accounts": _one("SELECT COUNT(*) AS c FROM accounts"),
            "proxies": _one("SELECT COUNT(*) AS c FROM proxies"),
            "mailboxes": _one("SELECT COUNT(*) AS c FROM mailbox_providers"),
        },
    }


@router.post("/api/system/cleanup")
def api_system_cleanup(
    request: Request,
    target: str = Query("events", pattern="^(events|finished_tasks|all_tasks|accounts)$"),
    days: int = Query(30, ge=1, le=3650),
) -> dict[str, Any]:
    """配置中心"清理"按钮：按 target 清不同数据集合。"""
    check_auth(request)
    cutoff_date = f"DATE('now', '-{max(1, days)} days')"
    deleted = 0
    if target == "events":
        before = fetch_one(
            f"SELECT COUNT(*) AS c FROM register_events WHERE DATE(created_at) < {cutoff_date}"
        )
        deleted = int(before["c"]) if before else 0
        execute_no_return(
            f"DELETE FROM register_events WHERE DATE(created_at) < {cutoff_date}"
        )
    elif target == "finished_tasks":
        rows = fetch_all(
            f"""
            SELECT * FROM tasks
            WHERE status IN ('completed', 'failed', 'stopped', 'partial')
            AND DATE(created_at) < {cutoff_date}
            """
        )
        for row in rows:
            delete_task_files(row)
            execute_no_return("DELETE FROM tasks WHERE id = ?", (int(row["id"]),))
            deleted += 1
    elif target == "all_tasks":
        rows = fetch_all("SELECT * FROM tasks WHERE status != 'running'")
        for row in rows:
            delete_task_files(row)
            execute_no_return("DELETE FROM tasks WHERE id = ?", (int(row["id"]),))
            deleted += 1
    elif target == "accounts":
        before = fetch_one("SELECT COUNT(*) AS c FROM accounts")
        deleted = int(before["c"]) if before else 0
        execute_no_return("DELETE FROM accounts")
    return {"ok": True, "target": target, "deleted": deleted}


@router.get("/api/system/export-config")
def api_system_export_config(request: Request) -> Response:
    """导出完整配置快照：system settings + 代理池 + 邮箱池。"""
    check_auth(request)
    payload = {
        "exported_at": now_iso(),
        "app_version": "1.1.0",
        "settings": read_settings(),
        "proxies": proxy_list(),
        "mailboxes": mailbox_list(),
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="grok-config-{ts}.json"'
        },
    )


@router.post("/api/system/import-config")
async def api_system_import_config(request: Request) -> dict[str, Any]:
    """导入配置快照。settings 整段覆盖；proxies / mailboxes 按 URL / name 增量。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    imported = {"settings": False, "proxies": 0, "mailboxes": 0}

    raw_settings = body.get("settings") or {}
    if isinstance(raw_settings, dict) and raw_settings:
        try:
            parsed = SystemSettings(**{
                k: v for k, v in raw_settings.items() if k in SystemSettings.model_fields
            })
            write_settings(parsed)
            imported["settings"] = True
        except Exception:
            pass

    for p in body.get("proxies") or []:
        try:
            if not isinstance(p, dict):
                continue
            existing = fetch_one("SELECT * FROM proxies WHERE url = ?", (p.get("url", ""),))
            if existing:
                continue
            if p.get("url"):
                proxy_add(
                    p["url"], str(p.get("label") or ""), bool(p.get("enabled", True))
                )
                imported["proxies"] += 1
        except Exception:
            pass

    for m in body.get("mailboxes") or []:
        try:
            if not isinstance(m, dict):
                continue
            existing = fetch_one(
                "SELECT * FROM mailbox_providers WHERE name = ?", (m.get("name", ""),)
            )
            if existing:
                continue
            mailbox_add(MailboxItem(
                name=str(m.get("name") or ""),
                provider_type=str(m.get("provider_type") or "tmail"),
                api_base=str(m.get("api_base") or ""),
                admin_password=str(m.get("admin_password") or ""),
                domain=str(m.get("domain") or ""),
                site_password=str(m.get("site_password") or ""),
                enabled=bool(m.get("enabled", True)),
            ))
            imported["mailboxes"] += 1
        except Exception:
            pass

    return {"ok": True, "imported": imported}

"""
公共模块：常量、数据库工具、Pydantic 模型、辅助函数、Supervisor 类。

所有 api/*.py 路由文件通过 `from api._shared import *` 导入本模块。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field


# ================================================================
# 常量
# ================================================================

APP_DIR = Path(__file__).resolve().parent.parent  # apps/console/
REPO_ROOT = APP_DIR.parents[1]
RUNTIME_DIR = APP_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
DB_PATH = RUNTIME_DIR / "console.db"

SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
SOURCE_VENV_PYTHON = Path(
    os.getenv("GROK_REGISTER_PYTHON", str(SOURCE_PROJECT / ".venv" / "bin" / "python"))
).expanduser()
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1")))
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))

PROJECT_FILES = (
    "DrissionPage_example.py",
    "email_register.py",
    "grok_oauth_device.py",
)
PROJECT_DIRS = ("turnstilePatch",)
PROJECT_AUX_FILES = ((APP_DIR / "core" / "hotmail_pool.py", "hotmail_pool.py"),)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

LINE_RE_ROUND = re.compile(r"开始第\s*(\d+)\s*轮注册")
LINE_RE_SUCCESS = re.compile(
    r"注册成功\s*\|\s*email=([^|\s]+)(?:\s*\|\s*password=([^|\s]*))?"
)
LINE_RE_ERROR = re.compile(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)")
LINE_RE_TEMP_EMAIL = re.compile(r"临时邮箱创建成功:\s*([^\s]+)")
LINE_RE_FILLED_EMAIL = re.compile(r"已填写邮箱并点击注册:\s*([^\s]+)")
LINE_RE_PUSH = re.compile(r"SSO token 已推送到 API")

CONSOLE_PASSWORD = os.getenv("GROK_REGISTER_CONSOLE_PASSWORD", "")
DOWNLOAD_GATE_INTERNAL_URL = os.getenv(
    "DOWNLOAD_GATE_INTERNAL_URL", "http://download-gate:18787"
).rstrip("/")
DOWNLOAD_GATE_INTERNAL_TOKEN = os.getenv("DOWNLOAD_GATE_INTERNAL_TOKEN", "").strip()
DOWNLOAD_GATE_PUBLIC_URL = os.getenv(
    "DOWNLOAD_GATE_PUBLIC_URL", "http://localhost:18787"
).rstrip("/")
CPA_AUTH_DIR = Path(
    os.getenv("GROK_REGISTER_CPA_AUTH_DIR", str(REPO_ROOT / "runtime" / "cpa-auth"))
).expanduser().resolve()
SUB_AUTH_DIR = Path(
    os.getenv("GROK_REGISTER_SUB_AUTH_DIR", str(REPO_ROOT / "runtime" / "sub-auth"))
).expanduser().resolve()
GROK2API_AUTH_DIR = Path(
    os.getenv(
        "GROK_REGISTER_GROK2API_AUTH_DIR",
        str(REPO_ROOT / "runtime" / "grok2api-auth"),
    )
).expanduser().resolve()


# ================================================================
# 数据库工具
# ================================================================

db_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db_lock, closing(get_conn()) as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, closing(get_conn()) as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, closing(get_conn()) as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, closing(get_conn()) as conn:
        conn.execute(query, params)
        conn.commit()


# ================================================================
# 认证
# ================================================================

def check_auth(request: Request):
    if not CONSOLE_PASSWORD:
        return
    auth = request.headers.get("Authorization", "")
    cookie = request.cookies.get("console_password", "")
    # SSE EventSource 不支持自定义 header，允许 query 参数认证
    query_token = request.query_params.get("token", "")
    if (
        auth == f"Bearer {CONSOLE_PASSWORD}"
        or cookie == CONSOLE_PASSWORD
        or query_token == CONSOLE_PASSWORD
    ):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


# ================================================================
# Pydantic 模型
# ================================================================

class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    count: int = Field(50, ge=1, le=5000)
    # 多平台扩展（默认 grok 以保持与旧表单的兼容）
    platform: str = Field("grok", min_length=1, max_length=64)
    engine_id: str | None = None
    extra: dict[str, Any] | None = None
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    api_endpoint: str | None = None
    api_token: str | None = None
    api_append: bool | None = None
    debug_mode: bool | None = None
    executor: str | None = None
    notes: str = ""


class SystemSettings(BaseModel):
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_site_password: str = ""
    api_endpoint: str = ""
    api_token: str = ""
    api_append: bool = True
    debug_mode: bool = False
    executor: str = "headless"
    lifecycle_enabled: bool = False
    lifecycle_check_hours: int = 6
    round_interval_sec: int = 2
    round_timeout_sec: int = 180
    max_concurrent_tasks: int = 1
    circuit_break_fail_threshold: int = 0
    captcha_provider: str = "none"
    captcha_api_key: str = ""
    log_level: str = "info"
    collect_error_samples: bool = True


class ProxyItem(BaseModel):
    url: str = Field(..., min_length=1)
    label: str = ""
    enabled: bool = True


class ProxyUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    reset_stats: bool | None = None


class MailboxItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    provider_type: str = Field(
        "tmail",
        pattern="^(tmail|duckmail|moemail|hotmail|outlookmail|laoudo|cloudflare_worker|freemail|testmail|tempmail_lol|duckduckgo|custom)$",
    )
    api_base: str = Field(..., min_length=1)
    admin_password: str = ""
    domain: str = ""
    site_password: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class MailboxUpdate(BaseModel):
    name: str | None = None
    provider_type: str | None = Field(None, pattern="^(tmail|duckmail|moemail|hotmail|outlookmail|custom)$")
    api_base: str | None = None
    admin_password: str | None = None
    domain: str | None = None
    site_password: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    reset_stats: bool | None = None


class AccountUpdate(BaseModel):
    lifecycle_status: str | None = Field(
        None, pattern="^(registered|trial|subscribed|expired|invalid)$"
    )
    plan_state: str | None = Field(
        None, pattern="^(free|trial|pro|team|unknown)$"
    )
    validity_status: str | None = Field(None, pattern="^(valid|invalid|unknown)$")
    notes: str | None = None
    last_error: str | None = None
    sso: str | None = None
    email: str | None = None
    password: str | None = None
    session_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None


class AccountDeliveryCreate(BaseModel):
    account_ids: list[int] = Field(..., min_length=1, max_length=500)
    title: str | None = Field(None, max_length=80)
    pack_mode: Literal["bundle"] = "bundle"


# ================================================================
# 辅助函数：配置
# ================================================================

def load_source_defaults() -> dict[str, Any]:
    config_path = SOURCE_PROJECT / "config.json"
    if config_path.exists():
        base = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        example_path = SOURCE_PROJECT / "config.example.json"
        if example_path.exists():
            base = json.loads(example_path.read_text(encoding="utf-8"))
        else:
            base = {
                "run": {"count": 50},
                "proxy": "",
                "browser_proxy": "",
                "temp_mail_api_base": "",
                "temp_mail_admin_password": "",
                "temp_mail_domain": "",
                "temp_mail_site_password": "",
                "api": {"endpoint": "", "token": "", "append": True},
            }

    env_count = os.getenv("GROK_REGISTER_DEFAULT_RUN_COUNT", "").strip()
    if env_count:
        try:
            base.setdefault("run", {})["count"] = max(1, int(env_count))
        except ValueError:
            pass

    env_map = {
        "proxy": "GROK_REGISTER_DEFAULT_PROXY",
        "browser_proxy": "GROK_REGISTER_DEFAULT_BROWSER_PROXY",
        "temp_mail_api_base": "GROK_REGISTER_DEFAULT_TEMP_MAIL_API_BASE",
        "temp_mail_admin_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_ADMIN_PASSWORD",
        "temp_mail_domain": "GROK_REGISTER_DEFAULT_TEMP_MAIL_DOMAIN",
        "temp_mail_site_password": "GROK_REGISTER_DEFAULT_TEMP_MAIL_SITE_PASSWORD",
    }
    for key, env_name in env_map.items():
        value = os.getenv(env_name)
        if value:
            base[key] = value

    api_base = dict(base.get("api") or {})
    api_env_map = {
        "endpoint": "GROK_REGISTER_DEFAULT_API_ENDPOINT",
        "token": "GROK_REGISTER_DEFAULT_API_TOKEN",
    }
    for key, env_name in api_env_map.items():
        value = os.getenv(env_name)
        if value:
            api_base[key] = value
    append_env = os.getenv("GROK_REGISTER_DEFAULT_API_APPEND")
    if append_env is not None:
        api_base["append"] = append_env.strip().lower() in {"1", "true", "yes", "on"}
    base["api"] = api_base

    base.setdefault("debug_mode", False)
    debug_env = os.getenv("GROK_REGISTER_DEFAULT_DEBUG_MODE")
    if debug_env is not None:
        base["debug_mode"] = debug_env.strip().lower() in {"1", "true", "yes", "on"}
    return base


def read_settings() -> dict[str, Any]:
    row = fetch_one("SELECT value FROM settings WHERE key = ?", ("system",))
    if not row:
        return {}
    try:
        data = json.loads(row["value"])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(settings: SystemSettings) -> dict[str, Any]:
    data = settings.model_dump()
    execute(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("system", json.dumps(data, ensure_ascii=False), now_iso()),
    )
    return data


def merged_defaults() -> dict[str, Any]:
    base = load_source_defaults()
    saved = read_settings()
    if saved.get("proxy") is not None:
        base["proxy"] = str(saved.get("proxy", ""))
    if saved.get("browser_proxy") is not None:
        base["browser_proxy"] = str(saved.get("browser_proxy", ""))
    for key in ("temp_mail_api_base", "temp_mail_admin_password", "temp_mail_domain", "temp_mail_site_password"):
        if key in saved:
            base[key] = str(saved.get(key, ""))
    api_base = dict(base.get("api") or {})
    saved_endpoint = str(saved.get("api_endpoint", "") or "").strip()
    saved_token = str(saved.get("api_token", "") or "").strip()
    if saved_endpoint:
        api_base["endpoint"] = saved_endpoint
    if saved_token:
        api_base["token"] = saved_token

    # 旧 system settings 可能保存了空 endpoint。启用 grok2api Exporter 时，
    # 复用同一份端点/口令，避免健康检查与注册任务配置出现两套真相。
    if not str(api_base.get("endpoint", "") or "").strip():
        exporter_row = fetch_one("SELECT value FROM settings WHERE key = ?", ("exporter_grok2api",))
        if exporter_row:
            try:
                exporter_config = json.loads(exporter_row["value"] or "{}")
            except Exception:
                exporter_config = {}
            if exporter_config.get("enabled"):
                api_base["endpoint"] = str(exporter_config.get("endpoint") or "").strip()
                exporter_extra = exporter_config.get("extra") or {}
                if not str(api_base.get("token", "") or "").strip():
                    api_base["token"] = str(exporter_extra.get("auth_token") or "").strip()
    if "api_append" in saved:
        api_base["append"] = bool(saved.get("api_append", True))
    base["api"] = api_base
    if "debug_mode" in saved:
        base["debug_mode"] = bool(saved.get("debug_mode", False))
    if "executor" in saved:
        base["executor"] = str(saved.get("executor") or "headless")
    else:
        base.setdefault("executor", "headless")
    if "lifecycle_enabled" in saved:
        base["lifecycle_enabled"] = bool(saved.get("lifecycle_enabled", False))
    else:
        base.setdefault("lifecycle_enabled", False)
    if "lifecycle_check_hours" in saved:
        base["lifecycle_check_hours"] = int(saved.get("lifecycle_check_hours") or 6)
    else:
        base.setdefault("lifecycle_check_hours", 6)

    _PASSTHROUGH_KEYS = (
        ("round_interval_sec", int, 2),
        ("round_timeout_sec", int, 180),
        ("max_concurrent_tasks", int, 1),
        ("circuit_break_fail_threshold", int, 0),
        ("captcha_provider", str, "none"),
        ("captcha_api_key", str, ""),
        ("log_level", str, "info"),
        ("collect_error_samples", bool, True),
    )
    for key, cast, default in _PASSTHROUGH_KEYS:
        if key in saved:
            try:
                base[key] = cast(saved.get(key) if saved.get(key) is not None else default)
            except Exception:
                base[key] = default
        else:
            base.setdefault(key, default)
    return base


def build_task_config(payload: TaskCreate) -> dict[str, Any]:
    defaults = merged_defaults()
    api_defaults = dict(defaults.get("api") or {})
    return {
        "run": {"count": int(payload.count)},
        "proxy": defaults.get("proxy", "") if payload.proxy is None else payload.proxy.strip(),
        "browser_proxy": defaults.get("browser_proxy", "") if payload.browser_proxy is None else payload.browser_proxy.strip(),
        "temp_mail_api_base": defaults.get("temp_mail_api_base", "") if payload.temp_mail_api_base is None else payload.temp_mail_api_base.strip(),
        "temp_mail_admin_password": defaults.get("temp_mail_admin_password", "") if payload.temp_mail_admin_password is None else payload.temp_mail_admin_password.strip(),
        "temp_mail_domain": defaults.get("temp_mail_domain", "") if payload.temp_mail_domain is None else payload.temp_mail_domain.strip(),
        "temp_mail_site_password": defaults.get("temp_mail_site_password", "") if payload.temp_mail_site_password is None else payload.temp_mail_site_password.strip(),
        "api": {
            "endpoint": api_defaults.get("endpoint", "") if payload.api_endpoint is None else payload.api_endpoint.strip(),
            "token": api_defaults.get("token", "") if payload.api_token is None else payload.api_token.strip(),
            "append": api_defaults.get("append", True) if payload.api_append is None else bool(payload.api_append),
        },
        "debug_mode": bool(defaults.get("debug_mode", False)) if payload.debug_mode is None else bool(payload.debug_mode),
        "executor": str(defaults.get("executor", "headless")) if not payload.executor else str(payload.executor),
    }


# ================================================================
# 辅助函数：任务
# ================================================================

def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
    # sqlite3.Row 不支持 .get()，使用 keys() 检查存在性（向后兼容未迁移的 db）
    _keys = set(row.keys())

    def _col(name: str, default: Any = "") -> Any:
        return row[name] if name in _keys else default

    params_raw = _col("params_json", "{}") or "{}"
    try:
        params = json.loads(params_raw)
    except Exception:
        params = {}

    return {
        "id": int(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "target_count": int(row["target_count"]),
        "completed_count": int(row["completed_count"]),
        "failed_count": int(row["failed_count"]),
        "current_round": int(row["current_round"]),
        "current_phase": row["current_phase"] or "",
        "last_email": row["last_email"] or "",
        "last_error": row["last_error"] or "",
        "last_log_at": row["last_log_at"] or "",
        "notes": row["notes"] or "",
        "config": json.loads(row["config_json"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
        "pid": row["pid"],
        # 多平台字段
        "platform": _col("platform", "grok") or "grok",
        "executor_type": _col("executor_type", "") or "",
        "engine_id": _col("engine_id", "") or "",
        "params": params,
    }


def read_log_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-limit:]


def parse_console_state(console_path: Path) -> dict[str, Any]:
    state = {
        "completed_count": 0,
        "failed_count": 0,
        "current_round": 0,
        "current_phase": "",
        "last_email": "",
        "last_error": "",
        "last_log_at": now_iso(),
    }
    if not console_path.exists():
        return state

    lines = console_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return state

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if m := LINE_RE_ROUND.search(line):
            state["current_round"] = int(m.group(1))
            state["current_phase"] = "starting_round"
        if m := LINE_RE_SUCCESS.search(line):
            state["completed_count"] += 1
            state["last_email"] = m.group(1)
            state["current_phase"] = "success"
        if m := LINE_RE_ERROR.search(line):
            state["failed_count"] += 1
            state["last_error"] = m.group(2).strip()
            state["current_phase"] = "error"
        if m := LINE_RE_TEMP_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "mailbox_created"
        if m := LINE_RE_FILLED_EMAIL.search(line):
            state["last_email"] = m.group(1)
            state["current_phase"] = "email_submitted"
        if "提取到验证码" in line:
            state["current_phase"] = "otp_received"
        if "最终注册页" in line:
            state["current_phase"] = "profile_page"
        if "Turnstile 响应已同步" in line:
            state["current_phase"] = "turnstile_solved"
        if "已填写注册资料并点击完成注册" in line:
            state["current_phase"] = "submitting_profile"
        if LINE_RE_PUSH.search(line):
            state["current_phase"] = "pushed_to_api"
        # 更新 last_log_at
        interesting = (
            "开始第", "临时邮箱创建成功", "已填写邮箱并点击注册",
            "提取到验证码", "已填写验证码", "最终注册页", "Turnstile",
            "已填写注册资料并点击完成注册", "注册成功", "[Error]", "已推送到 API",
        )
        if any(token in line for token in interesting):
            state["last_log_at"] = now_iso()
    return state


def task_row(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def delete_task_files(row: sqlite3.Row) -> None:
    task_dir = Path(row["task_dir"])
    if task_dir.exists() and task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)


def copy_source_to_task_dir(task_dir: Path, task_config: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    for file_name in PROJECT_FILES:
        shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
    for source, target_name in PROJECT_AUX_FILES:
        shutil.copy2(source, task_dir / target_name)
    for dir_name in PROJECT_DIRS:
        src = SOURCE_PROJECT / dir_name
        dst = task_dir / dir_name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "sso").mkdir(exist_ok=True)
    (task_dir / "config.json").write_text(
        json.dumps(task_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ================================================================
# 辅助函数：代理池
# ================================================================

def _proxy_from_row(row: sqlite3.Row) -> dict[str, Any]:
    total = int(row["success_count"]) + int(row["failure_count"])
    success_rate = (int(row["success_count"]) / total * 100.0) if total else 0.0
    return {
        "id": int(row["id"]),
        "url": row["url"],
        "label": row["label"] or "",
        "enabled": bool(row["enabled"]),
        "success_count": int(row["success_count"]),
        "failure_count": int(row["failure_count"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "success_rate": round(success_rate, 2),
        "last_used_at": row["last_used_at"] or "",
        "created_at": row["created_at"],
    }


def proxy_list(include_disabled: bool = True) -> list[dict[str, Any]]:
    if include_disabled:
        rows = fetch_all("SELECT * FROM proxies ORDER BY id ASC")
    else:
        rows = fetch_all("SELECT * FROM proxies WHERE enabled = 1 ORDER BY id ASC")
    return [_proxy_from_row(r) for r in rows]


def proxy_add(url: str, label: str = "", enabled: bool = True) -> dict[str, Any]:
    url = url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="proxy url required")
    existing = fetch_one("SELECT * FROM proxies WHERE url = ?", (url,))
    if existing:
        raise HTTPException(status_code=409, detail="proxy url already exists")
    execute(
        """
        INSERT INTO proxies (url, label, enabled, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (url, label, 1 if enabled else 0, now_iso()),
    )
    row = fetch_one("SELECT * FROM proxies WHERE url = ?", (url,))
    assert row is not None
    return _proxy_from_row(row)


def proxy_update(proxy_id: int, label: str | None, enabled: bool | None, reset_stats: bool | None) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM proxies WHERE id = ?", (proxy_id,))
    if not row:
        raise HTTPException(status_code=404, detail="proxy not found")
    new_label = row["label"] if label is None else label
    new_enabled = int(row["enabled"]) if enabled is None else (1 if enabled else 0)
    if reset_stats:
        execute_no_return(
            """
            UPDATE proxies
            SET label = ?, enabled = ?, success_count = 0, failure_count = 0,
                consecutive_failures = 0
            WHERE id = ?
            """,
            (new_label, new_enabled, proxy_id),
        )
    else:
        execute_no_return(
            "UPDATE proxies SET label = ?, enabled = ? WHERE id = ?",
            (new_label, new_enabled, proxy_id),
        )
    row = fetch_one("SELECT * FROM proxies WHERE id = ?", (proxy_id,))
    assert row is not None
    return _proxy_from_row(row)


def proxy_delete(proxy_id: int) -> None:
    execute_no_return("DELETE FROM proxies WHERE id = ?", (proxy_id,))


def proxy_pick_best() -> dict[str, Any] | None:
    rows = fetch_all("SELECT * FROM proxies WHERE enabled = 1")
    if not rows:
        return None
    alive = []
    for r in rows:
        if int(r["consecutive_failures"]) >= 5:
            execute_no_return("UPDATE proxies SET enabled = 0 WHERE id = ?", (int(r["id"]),))
            continue
        alive.append(r)
    if not alive:
        return None
    import random as _random
    weights = []
    for r in alive:
        s = int(r["success_count"])
        f = int(r["failure_count"])
        weights.append((s + 1) / (s + f + 2))
    chosen = _random.choices(alive, weights=weights, k=1)[0]
    execute_no_return("UPDATE proxies SET last_used_at = ? WHERE id = ?", (now_iso(), int(chosen["id"])))
    return _proxy_from_row(chosen)


def proxy_report_success(url: str) -> None:
    url = (url or "").strip()
    if not url:
        return
    execute_no_return(
        """
        UPDATE proxies
        SET success_count = success_count + 1, consecutive_failures = 0, last_used_at = ?
        WHERE url = ?
        """,
        (now_iso(), url),
    )


def proxy_report_failure(url: str) -> None:
    url = (url or "").strip()
    if not url:
        return
    execute_no_return(
        """
        UPDATE proxies
        SET failure_count = failure_count + 1,
            consecutive_failures = consecutive_failures + 1,
            last_used_at = ?
        WHERE url = ?
        """,
        (now_iso(), url),
    )


# ================================================================
# WEBUI_DIR（前端静态资源目录）
# ================================================================

WEBUI_DIR = Path(os.getenv("WEBUI_DIR", "")).expanduser() if os.getenv("WEBUI_DIR") else (APP_DIR / "static")
if not WEBUI_DIR.exists():
    try:
        WEBUI_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# ================================================================
# 数据库初始化
# ================================================================

def init_db() -> None:
    """初始化数据库结构：创建 settings / tasks / proxies / register_events / accounts / mailbox_providers 表。"""
    ensure_dirs()
    with db_lock, closing(get_conn()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                current_round INTEGER NOT NULL DEFAULT 0,
                current_phase TEXT,
                last_email TEXT,
                last_error TEXT,
                last_log_at TEXT,
                notes TEXT,
                config_json TEXT NOT NULL,
                task_dir TEXT NOT NULL,
                console_path TEXT NOT NULL,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                exit_code INTEGER,
                platform TEXT NOT NULL DEFAULT 'grok',
                executor_type TEXT NOT NULL DEFAULT '',
                engine_id TEXT NOT NULL DEFAULT '',
                params_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS register_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                ok INTEGER NOT NULL,
                email TEXT,
                proxy_url TEXT,
                error_kind TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_created_at
                ON register_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_proxy
                ON register_events(proxy_url);

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                sso TEXT NOT NULL,
                password TEXT NOT NULL DEFAULT '',
                task_id INTEGER,
                proxy_url TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                lifecycle_status TEXT NOT NULL DEFAULT 'registered',
                plan_state TEXT NOT NULL DEFAULT 'unknown',
                validity_status TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'grok',
                extra_json TEXT NOT NULL DEFAULT '{}',
                exporter_status_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(email, sso)
            );

            CREATE TABLE IF NOT EXISTS delivery_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_key TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL DEFAULT 'grok',
                required_model TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'pending',
                source TEXT NOT NULL DEFAULT 'dynamic',
                title TEXT NOT NULL DEFAULT '',
                bundle_id TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                committed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS account_delivery_leases (
                id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                lease_token TEXT NOT NULL,
                state TEXT NOT NULL,
                probe_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY(order_id) REFERENCES delivery_orders(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );

            CREATE TABLE IF NOT EXISTS account_delivery_consumptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                lease_id TEXT,
                account_id INTEGER NOT NULL UNIQUE,
                card_key TEXT NOT NULL,
                bundle_id TEXT NOT NULL DEFAULT '',
                document_json TEXT NOT NULL,
                consumed_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES delivery_orders(id),
                FOREIGN KEY(lease_id) REFERENCES account_delivery_leases(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_consumptions_card
                ON account_delivery_consumptions(card_key);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_active_account
                ON account_delivery_leases(account_id)
                WHERE state IN ('probing', 'ready', 'packing');
            CREATE INDEX IF NOT EXISTS idx_delivery_leases_order
                ON account_delivery_leases(order_id);

            CREATE TABLE IF NOT EXISTS mailbox_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'tmail',
                api_base TEXT NOT NULL,
                admin_password TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL DEFAULT '',
                site_password TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        # 兼容迁移：给旧的 accounts 表补新字段（不存在时才加）
        _existing_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        _migrations = [
            ("password", "TEXT NOT NULL DEFAULT ''"),
            ("lifecycle_status", "TEXT NOT NULL DEFAULT 'registered'"),
            ("plan_state", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("validity_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("notes", "TEXT NOT NULL DEFAULT ''"),
            # 多平台扩展
            ("platform", "TEXT NOT NULL DEFAULT 'grok'"),
            ("extra_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("exporter_status_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col, col_type in _migrations:
            if col not in _existing_cols:
                try:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass

        _mailbox_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(mailbox_providers)").fetchall()
        }
        if "config_json" not in _mailbox_cols:
            conn.execute("ALTER TABLE mailbox_providers ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")

        # 兼容迁移：给旧的 tasks 表补多平台字段
        _task_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        _task_migrations = [
            ("platform", "TEXT NOT NULL DEFAULT 'grok'"),
            ("executor_type", "TEXT NOT NULL DEFAULT ''"),
            ("engine_id", "TEXT NOT NULL DEFAULT ''"),
            ("params_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col, col_type in _task_migrations:
            if col not in _task_cols:
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass

        # 交付卡密必须绑定目标平台。旧订单保持 Grok 语义，避免升级后串池。
        _delivery_order_cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(delivery_orders)").fetchall()
        }
        if "platform" not in _delivery_order_cols:
            try:
                conn.execute(
                    "ALTER TABLE delivery_orders "
                    "ADD COLUMN platform TEXT NOT NULL DEFAULT 'grok'"
                )
            except sqlite3.OperationalError:
                pass
        # 老的手动交付订单可从租约账号恢复平台；历史动态卡保持 Grok 默认值。
        conn.execute(
            """
            UPDATE delivery_orders
            SET platform = COALESCE(
                (
                    SELECT a.platform
                    FROM account_delivery_leases l
                    JOIN accounts a ON a.id = l.account_id
                    WHERE l.order_id = delivery_orders.id
                    ORDER BY l.created_at, l.account_id
                    LIMIT 1
                ),
                platform,
                'grok'
            )
            WHERE source = 'manual'
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_delivery_orders_platform_state "
            "ON delivery_orders(platform, state)"
        )

        # DownloadGate 的动态交付依赖 Grok SSO 先换取 CPA OAuth 凭据。
        # 新部署默认启用本地 CPA 产物目录，这样注册成功后后台队列会立即
        # 生成 access/refresh token，而不是等到用户取件时才发现 token 为空。
        # INSERT OR IGNORE 保留用户已经在 UI 中保存的启用/禁用和远程端点配置。
        cpa_auto_mint = os.getenv("GROK_REGISTER_CPA_AUTO_MINT", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        default_cpa_config = {
            "enabled": cpa_auto_mint,
            "endpoint": "",
            "api_append": True,
            "template": "",
            "extra": {
                "auth_dir": str(CPA_AUTH_DIR),
                "sub_auth_dir": str(SUB_AUTH_DIR),
                "proxy": str(
                    os.getenv("GROK_REGISTER_CPA_PROXY")
                    or os.getenv("GROK_REGISTER_DEFAULT_PROXY")
                    or ""
                ).strip(),
                "base_url": "https://cli-chat-proxy.grok.com/v1",
                "timeout": 90,
                "identity_timeout": 12,
                "verify_tls": True,
                "probe": True,
                "probe_required": True,
                "auto_mint": cpa_auto_mint,
                "prevalidate_enabled": cpa_auto_mint,
                "prevalidate_ttl_minutes": 60,
                "prevalidate_batch_size": 10,
                "prevalidate_scan_seconds": 30,
            },
        }
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            (
                "exporter_cpa",
                json.dumps(default_cpa_config, ensure_ascii=False),
                now_iso(),
            ),
        )
        conn.commit()


# ================================================================
# 网络 / 健康检查
# ================================================================

def _mask_proxy(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.netloc:
        return proxy_url
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _request_with_optional_proxy(
    url: str,
    proxy_url: str = "",
    method: str = "GET",
    timeout: int = 15,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    return requests.request(
        method,
        url,
        timeout=timeout,
        headers=headers,
        proxies=proxies,
        allow_redirects=True,
    )


def _build_health_item(
    key: str,
    label: str,
    ok: bool,
    summary: str,
    detail: str,
    target: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": ok,
        "summary": summary,
        "detail": detail,
        "target": target,
        "checked_at": now_iso(),
    }


def run_health_checks() -> dict[str, Any]:
    """运行 WARP / grok2api / temp_mail / x.ai 四项健康检查。"""
    defaults = merged_defaults()
    items: list[dict[str, Any]] = []

    browser_proxy = str(defaults.get("browser_proxy", "") or "").strip()
    request_proxy = str(defaults.get("proxy", "") or "").strip()
    api_conf = dict(defaults.get("api") or {})
    api_endpoint = str(api_conf.get("endpoint", "") or "").strip()
    temp_mail_api_base = str(defaults.get("temp_mail_api_base", "") or "").strip()

    warp_target = browser_proxy or request_proxy
    if not warp_target:
        items.append(
            _build_health_item(
                "warp",
                "WARP / Proxy",
                False,
                "未配置代理出口",
                "当前系统默认配置里没有 `browser_proxy` 或 `proxy`，无法检查前置网络出口。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxy_url=warp_target,
                timeout=20,
            )
            body = response.text
            ip_match = re.search(r"(?m)^ip=(.+)$", body)
            loc_match = re.search(r"(?m)^loc=(.+)$", body)
            warp_match = re.search(r"(?m)^warp=(.+)$", body)
            ip = ip_match.group(1).strip() if ip_match else "unknown"
            loc = loc_match.group(1).strip() if loc_match else "unknown"
            warp_state = warp_match.group(1).strip() if warp_match else "unknown"
            ok = response.status_code == 200
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    ok,
                    f"HTTP {response.status_code} | IP {ip} | LOC {loc}",
                    f"通过代理 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 成功，warp={warp_state}。",
                    _mask_proxy(warp_target),
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "warp",
                    "WARP / Proxy",
                    False,
                    "代理出口不可达",
                    f"通过 `{_mask_proxy(warp_target)}` 访问 Cloudflare trace 失败：{exc}",
                    _mask_proxy(warp_target),
                )
            )

    if not api_endpoint:
        items.append(
            _build_health_item(
                "grok2api",
                "grok2api Sink",
                False,
                "未配置 token sink",
                "当前系统默认配置里没有 `api.endpoint`，注册成功后不会自动入池。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(api_endpoint, timeout=15)
            ok = response.status_code in {200, 401, 403, 405}
            items.append(
                _build_health_item(
                    "grok2api",
                    "grok2api Sink",
                    ok,
                    f"HTTP {response.status_code}",
                    "接口已可达。即使返回 401/403，也说明服务本身在线，只是需要正确的管理口令。",
                    api_endpoint,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "grok2api",
                    "grok2api Sink",
                    False,
                    "接口不可达",
                    f"访问 `{api_endpoint}` 失败：{exc}",
                    api_endpoint,
                )
            )

    if not temp_mail_api_base:
        items.append(
            _build_health_item(
                "temp_mail",
                "Temp Mail API",
                False,
                "未配置临时邮箱 API",
                "当前系统默认配置里没有 `temp_mail_api_base`，注册流程会在创建邮箱阶段直接失败。",
                "-",
            )
        )
    else:
        try:
            response = _request_with_optional_proxy(
                temp_mail_api_base,
                proxy_url=request_proxy,
                timeout=15,
            )
            ok = response.status_code < 500
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    ok,
                    f"HTTP {response.status_code}",
                    "接口地址可达。这里只做基础连通性检查，不会真的创建邮箱地址。",
                    temp_mail_api_base,
                )
            )
        except Exception as exc:
            items.append(
                _build_health_item(
                    "temp_mail",
                    "Temp Mail API",
                    False,
                    "接口不可达",
                    f"访问 `{temp_mail_api_base}` 失败：{exc}",
                    temp_mail_api_base,
                )
            )

    xai_proxy = browser_proxy or request_proxy
    xai_target = "https://x.ai"
    xai_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )
    try:
        response = _request_with_optional_proxy(
            xai_target,
            proxy_url=xai_proxy,
            timeout=20,
            headers={
                "User-Agent": xai_ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        ok = response.status_code in {200, 301, 302, 303, 307, 308}
        detail = (
            f"使用 `{_mask_proxy(xai_proxy)}` 访问 x.ai 返回 HTTP {response.status_code}。"
            if xai_proxy
            else f"直连访问 x.ai 返回 HTTP {response.status_code}。"
        )
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                ok,
                f"HTTP {response.status_code}",
                detail,
                xai_target,
            )
        )
    except Exception as exc:
        err_text = str(exc)
        if "0x04" in err_text or "Host unreachable" in err_text:
            hint = (
                "SOCKS 代理返回 'Host unreachable' —— 代理本身能通，"
                "但从该出口无法到达 x.ai（常见于 WARP IP 被 x.ai 拦截，"
                "或 Python requests 的 TLS 指纹被目标识别并丢弃）。"
                "真实注册流程走的是 Chrome 浏览器，不受此影响。"
            )
        elif "NameResolutionError" in err_text or "resolve" in err_text.lower():
            hint = "DNS 解析失败，请确认代理地址或容器网络可用。"
        else:
            hint = None

        detail_text = f"访问 `{xai_target}` 失败：{exc}"
        if hint:
            detail_text += f"\n\n提示：{hint}"

        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                False,
                "注册页不可达",
                detail_text,
                xai_target,
            )
        )

    return {
        "items": items,
        "checked_at": now_iso(),
    }


# ================================================================
# 注册事件 / 错误分类
# ================================================================

def classify_error(message: str) -> str:
    """把原始错误信息归并成少量可读的错误类型，便于聚合统计。"""
    if not message:
        return "unknown"
    m = message.lower()
    if "turnstile" in m:
        return "turnstile_failed"
    if "timeout" in m or "超时" in message:
        return "timeout"
    if "0x04" in m or "host unreachable" in m:
        return "proxy_host_unreachable"
    if "proxy" in m or "socks" in m or "name or service not known" in m:
        return "proxy_error"
    if "captcha" in m or "cloudflare" in m:
        return "anti_bot_challenge"
    if "email" in m and ("验证码" in message or "code" in m):
        return "email_otp_failed"
    if "429" in m or "rate limit" in m:
        return "rate_limited"
    if "403" in m or "blocked" in m or "封" in message:
        return "blocked"
    return "other"


def log_register_event(
    *,
    task_id: int | None,
    ok: bool,
    email: str = "",
    proxy_url: str = "",
    error_kind: str = "",
    error_message: str = "",
) -> None:
    execute(
        """
        INSERT INTO register_events (task_id, ok, email, proxy_url, error_kind, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (task_id, 1 if ok else 0, email, proxy_url, error_kind, error_message, now_iso()),
    )
    if proxy_url:
        if ok:
            proxy_report_success(proxy_url)
        else:
            proxy_report_failure(proxy_url)


# ================================================================
# 任务目录辅助
# ================================================================

def task_row(task_id: int) -> sqlite3.Row:
    row = fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def delete_task_files(row: sqlite3.Row) -> None:
    task_dir = Path(row["task_dir"])
    if task_dir.exists() and task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)


def copy_source_to_task_dir(task_dir: Path, task_config: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    for file_name in PROJECT_FILES:
        shutil.copy2(SOURCE_PROJECT / file_name, task_dir / file_name)
    for source, target_name in PROJECT_AUX_FILES:
        shutil.copy2(source, task_dir / target_name)
    for dir_name in PROJECT_DIRS:
        src = SOURCE_PROJECT / dir_name
        dst = task_dir / dir_name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (task_dir / "logs").mkdir(exist_ok=True)
    (task_dir / "sso").mkdir(exist_ok=True)
    (task_dir / "config.json").write_text(
        json.dumps(task_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ================================================================
# 邮箱 Provider 池（完整实现）
# ================================================================

def _mailbox_from_row(row: sqlite3.Row) -> dict[str, Any]:
    total = int(row["success_count"]) + int(row["failure_count"])
    success_rate = (int(row["success_count"]) / total * 100.0) if total else 0.0
    keys = set(row.keys())
    try:
        config = json.loads(row["config_json"] or "{}") if "config_json" in keys else {}
    except Exception:
        config = {}
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "provider_type": row["provider_type"],
        "api_base": row["api_base"],
        "admin_password": row["admin_password"] or "",
        "domain": row["domain"] or "",
        "site_password": row["site_password"] or "",
        "config": config,
        "enabled": bool(row["enabled"]),
        "success_count": int(row["success_count"]),
        "failure_count": int(row["failure_count"]),
        "consecutive_failures": int(row["consecutive_failures"]),
        "success_rate": round(success_rate, 2),
        "last_used_at": row["last_used_at"] or "",
        "created_at": row["created_at"],
    }


def mailbox_list() -> list[dict[str, Any]]:
    rows = fetch_all("SELECT * FROM mailbox_providers ORDER BY id ASC")
    return [_mailbox_from_row(r) for r in rows]


def mailbox_add(payload: "MailboxItem") -> dict[str, Any]:
    execute(
        """
        INSERT INTO mailbox_providers
            (name, provider_type, api_base, admin_password, domain, site_password, config_json, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.provider_type,
            payload.api_base.strip(),
            payload.admin_password,
            payload.domain.strip(),
            payload.site_password,
            json.dumps(payload.config or {}, ensure_ascii=False),
            1 if payload.enabled else 0,
            now_iso(),
        ),
    )
    row = fetch_one(
        "SELECT * FROM mailbox_providers ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    return _mailbox_from_row(row)


def mailbox_update(mbox_id: int, payload: "MailboxUpdate") -> dict[str, Any]:
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    new_name = row["name"] if payload.name is None else payload.name.strip()
    new_type = row["provider_type"] if payload.provider_type is None else payload.provider_type
    new_api = row["api_base"] if payload.api_base is None else payload.api_base.strip()
    new_admin = row["admin_password"] if payload.admin_password is None else payload.admin_password
    new_domain = row["domain"] if payload.domain is None else payload.domain.strip()
    new_site = row["site_password"] if payload.site_password is None else payload.site_password
    row_keys = set(row.keys())
    old_config = row["config_json"] if "config_json" in row_keys else "{}"
    new_config = old_config if payload.config is None else json.dumps(payload.config, ensure_ascii=False)
    new_enabled = int(row["enabled"]) if payload.enabled is None else (1 if payload.enabled else 0)
    if payload.reset_stats:
        execute_no_return(
            """
            UPDATE mailbox_providers
            SET name = ?, provider_type = ?, api_base = ?, admin_password = ?,
                domain = ?, site_password = ?, config_json = ?, enabled = ?,
                success_count = 0, failure_count = 0, consecutive_failures = 0
            WHERE id = ?
            """,
            (new_name, new_type, new_api, new_admin, new_domain, new_site, new_config, new_enabled, mbox_id),
        )
    else:
        execute_no_return(
            """
            UPDATE mailbox_providers
            SET name = ?, provider_type = ?, api_base = ?, admin_password = ?,
                domain = ?, site_password = ?, config_json = ?, enabled = ?
            WHERE id = ?
            """,
            (new_name, new_type, new_api, new_admin, new_domain, new_site, new_config, new_enabled, mbox_id),
        )
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    assert row is not None
    return _mailbox_from_row(row)


def mailbox_delete(mbox_id: int) -> None:
    execute_no_return("DELETE FROM mailbox_providers WHERE id = ?", (mbox_id,))


def mailbox_pick_best() -> dict[str, Any] | None:
    """按成功率加权从启用的邮箱 provider 里挑一个；连续失败 >=5 自动禁用。"""
    rows = fetch_all("SELECT * FROM mailbox_providers WHERE enabled = 1")
    if not rows:
        return None
    alive = []
    for r in rows:
        if int(r["consecutive_failures"]) >= 5:
            execute_no_return(
                "UPDATE mailbox_providers SET enabled = 0 WHERE id = ?",
                (int(r["id"]),),
            )
            continue
        alive.append(r)
    if not alive:
        return None
    import random as _random
    weights = []
    for r in alive:
        s = int(r["success_count"])
        f = int(r["failure_count"])
        weights.append((s + 1) / (s + f + 2))
    chosen = _random.choices(alive, weights=weights, k=1)[0]
    execute_no_return(
        "UPDATE mailbox_providers SET last_used_at = ? WHERE id = ?",
        (now_iso(), int(chosen["id"])),
    )
    return _mailbox_from_row(chosen)


def mailbox_report_success(mbox_id: int) -> None:
    if not mbox_id:
        return
    execute_no_return(
        """
        UPDATE mailbox_providers
        SET success_count = success_count + 1, consecutive_failures = 0, last_used_at = ?
        WHERE id = ?
        """,
        (now_iso(), mbox_id),
    )


def mailbox_report_failure(mbox_id: int) -> None:
    if not mbox_id:
        return
    execute_no_return(
        """
        UPDATE mailbox_providers
        SET failure_count = failure_count + 1,
            consecutive_failures = consecutive_failures + 1,
            last_used_at = ?
        WHERE id = ?
        """,
        (now_iso(), mbox_id),
    )


def seed_mailbox_from_defaults(force: bool = False) -> dict[str, Any] | None:
    """首次启动时把系统默认邮箱配置导入 Provider 池。"""
    if not force:
        existing = fetch_one("SELECT COUNT(*) AS c FROM mailbox_providers")
        if existing and int(existing["c"]) > 0:
            return None
    try:
        defaults = merged_defaults()
    except Exception:
        return None

    api_base = str(defaults.get("temp_mail_api_base") or "").strip()
    if not api_base:
        return None

    provider_type = str(defaults.get("temp_mail_provider") or "").strip().lower()
    if provider_type not in {"tmail", "duckmail", "moemail", "custom"}:
        if "tmail" in api_base or "mail.nnioj" in api_base:
            provider_type = "tmail"
        else:
            provider_type = "moemail"

    admin_pw = str(defaults.get("temp_mail_admin_password") or "")
    domain = str(defaults.get("temp_mail_domain") or "")
    site_pw = str(defaults.get("temp_mail_site_password") or "")

    suffix = ""
    idx = 0
    while True:
        candidate_name = f"default{suffix}"
        if not fetch_one(
            "SELECT 1 FROM mailbox_providers WHERE name = ?", (candidate_name,)
        ):
            break
        idx += 1
        suffix = f"-{idx}"

    try:
        item = MailboxItem(
            name=candidate_name,
            provider_type=provider_type,
            api_base=api_base,
            admin_password=admin_pw,
            domain=domain,
            site_password=site_pw,
            enabled=True,
        )
        return mailbox_add(item)
    except Exception:
        return None


# ================================================================
# 账号资产
# ================================================================

def _row_col(row: sqlite3.Row, name: str, fallback: Any) -> Any:
    try:
        val = row[name]
        return val if val is not None else fallback
    except (IndexError, KeyError):
        return fallback


def _derive_display_status(lifecycle: str, validity: str, plan: str) -> str:
    """对齐 vendor core._vendor_aar.account_graph._derive_display_status：
    把 lifecycle/validity/plan 综合成一个前端展示用状态。"""
    if validity == "invalid":
        return "invalid"
    if plan == "expired" or lifecycle == "expired":
        return "expired"
    if plan == "subscribed":
        return "subscribed"
    if plan == "trial":
        return "trial"
    return lifecycle or "registered"


def _account_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    lifecycle = _row_col(r, "lifecycle_status", "registered")
    plan = _row_col(r, "plan_state", "unknown")
    validity = _row_col(r, "validity_status", "unknown")
    extra_raw = _row_col(r, "extra_json", "{}")
    try:
        extra = json.loads(extra_raw) if extra_raw else {}
    except Exception:
        extra = {}

    # 从 sso + extra_json 拆出独立 token 字段
    sso = r["sso"] or ""
    tokens = {
        "session_token": "",
        "access_token": "",
        "refresh_token": "",
        "id_token": "",
    }
    platform = _row_col(r, "platform", "grok")

    # windsurf: sso 格式 "devin-session-token$eyJ..."
    if sso.startswith("devin-session-token$"):
        tokens["session_token"] = sso[len("devin-session-token$"):]
    # extra_json 里的 token 优先（更精确）
    tokens["access_token"] = str(extra.get("access_token") or extra.get("accessToken") or "")
    tokens["refresh_token"] = str(extra.get("refresh_token") or extra.get("refreshToken") or "")
    tokens["id_token"] = str(extra.get("id_token") or extra.get("idToken") or "")
    if extra.get("session_token") or extra.get("sessionToken"):
        tokens["session_token"] = str(extra.get("session_token") or extra.get("sessionToken"))
    if extra.get("auth_token") and not tokens["session_token"]:
        tokens["session_token"] = extra["auth_token"]
    # 其它平台沿用旧兼容逻辑：没有独立 access_token 时把 sso 当作 access_token。
    if not tokens["access_token"] and sso and platform != "grok" and not sso.startswith("devin-session-token$"):
        tokens["access_token"] = sso

    return {
        "id": int(r["id"]),
        "email": r["email"],
        "sso": sso,
        "password": _row_col(r, "password", ""),
        "task_id": r["task_id"],
        "proxy_url": r["proxy_url"] or "",
        "status": r["status"],
        "platform": platform,
        "lifecycle_status": lifecycle,
        "plan_state": plan,
        "validity_status": validity,
        "display_status": _derive_display_status(lifecycle, validity, plan),
        "tokens": tokens,
        "last_error": _row_col(r, "last_error", ""),
        "last_checked_at": r["last_checked_at"] or "",
        "notes": _row_col(r, "notes", ""),
        "extra_json": extra_raw,
        "exporter_status_json": _row_col(r, "exporter_status_json", "{}"),
        "created_at": r["created_at"],
    }


def account_upsert(email: str, sso: str, task_id: int | None, proxy_url: str = "") -> None:
    if not email or not sso:
        return
    execute_no_return(
        """
        INSERT OR IGNORE INTO accounts (email, sso, task_id, proxy_url, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (email.strip(), sso.strip(), task_id, proxy_url, now_iso()),
    )


def account_list(limit: int = 500) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM accounts ORDER BY id DESC LIMIT ?",
        (max(1, min(limit, 5000)),),
    )
    return [_account_row_to_dict(r) for r in rows]


def account_list_by_ids(account_ids: list[int]) -> list[dict[str, Any]]:
    ordered_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = fetch_all(
        f"SELECT * FROM accounts WHERE id IN ({placeholders})",
        tuple(ordered_ids),
    )
    accounts_by_id = {
        int(row["id"]): _account_row_to_dict(row)
        for row in rows
    }
    return [accounts_by_id[account_id] for account_id in ordered_ids if account_id in accounts_by_id]


def account_delivery_document(account: dict[str, Any]) -> dict[str, Any]:
    try:
        extra = json.loads(account.get("extra_json") or "{}")
    except Exception:
        extra = {}
    try:
        exporter_status = json.loads(account.get("exporter_status_json") or "{}")
    except Exception:
        exporter_status = {}
    tokens = account.get("tokens") if isinstance(account.get("tokens"), dict) else {}
    cpa_auth: dict[str, Any] = {}
    cpa_meta = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
    cpa_filename = Path(str(cpa_meta.get("filename") or "")).name
    if cpa_filename:
        cpa_path = CPA_AUTH_DIR / cpa_filename
        try:
            loaded = json.loads(cpa_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cpa_auth = loaded
        except (OSError, ValueError):
            cpa_auth = {}
    if not cpa_auth and tokens.get("access_token") and tokens.get("refresh_token"):
        try:
            from core.cpa_auth import token_to_cpa_record

            cpa_auth = token_to_cpa_record(
                {
                    **extra,
                    "access_token": tokens.get("access_token"),
                    "refresh_token": tokens.get("refresh_token"),
                    "id_token": tokens.get("id_token"),
                },
                str(account.get("email") or ""),
                base_url=str(extra.get("base_url") or ""),
            )
        except (ImportError, TypeError, ValueError):
            cpa_auth = {}
    external_account_id = str(cpa_auth.get("sub") or extra.get("sub") or "")
    document: dict[str, Any] = {
        "schema": "grok-register.account-delivery.v1",
        "account_id": external_account_id,
        "platform": account.get("platform") or "grok",
        "email": account.get("email") or "",
        "password": account.get("password") or "",
        "sso": account.get("sso") or "",
        "session_token": tokens.get("session_token") or "",
        "access_token": tokens.get("access_token") or "",
        "refresh_token": tokens.get("refresh_token") or "",
        "id_token": tokens.get("id_token") or "",
        "lifecycle_status": account.get("lifecycle_status") or "registered",
        "plan_state": account.get("plan_state") or "unknown",
        "validity_status": account.get("validity_status") or "unknown",
        "created_at": account.get("created_at") or "",
        "credentials": {
            "password": account.get("password") or "",
            "sso": account.get("sso") or "",
            "session_token": tokens.get("session_token") or "",
            "access_token": tokens.get("access_token") or "",
            "refresh_token": tokens.get("refresh_token") or "",
            "id_token": tokens.get("id_token") or "",
            "token_type": extra.get("token_type") or "",
            "expires_in": extra.get("expires_in"),
            "expired": extra.get("expired"),
        },
        "cpa_auth": cpa_auth,
        "account_state": {
            "source_id": account.get("id"),
            "task_id": account.get("task_id"),
            "proxy_url": account.get("proxy_url") or "",
            "status": account.get("status") or "",
            "lifecycle_status": account.get("lifecycle_status") or "registered",
            "plan_state": account.get("plan_state") or "unknown",
            "validity_status": account.get("validity_status") or "unknown",
            "last_error": account.get("last_error") or "",
            "last_checked_at": account.get("last_checked_at") or "",
            "notes": account.get("notes") or "",
            "created_at": account.get("created_at") or "",
        },
        "extra": extra,
        "exporter_status": exporter_status,
    }
    for key in (
        "base_url",
        "token_endpoint",
        "redirect_uri",
        "headers",
        "expired",
        "expires_in",
        "sub",
        "token_type",
        "cpa",
    ):
        if key in extra:
            document[key] = extra[key]
    return document


def account_update(
    account_id: int,
    *,
    lifecycle_status: str | None = None,
    plan_state: str | None = None,
    validity_status: str | None = None,
    notes: str | None = None,
    last_error: str | None = None,
    sso: str | None = None,
    email: str | None = None,
    password: str | None = None,
    session_token: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    id_token: str | None = None,
) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not row:
        raise HTTPException(status_code=404, detail="account not found")
    fields = []
    params: list[Any] = []
    if lifecycle_status is not None:
        fields.append("lifecycle_status = ?")
        params.append(lifecycle_status)
    if plan_state is not None:
        fields.append("plan_state = ?")
        params.append(plan_state)
    if validity_status is not None:
        fields.append("validity_status = ?")
        params.append(validity_status)
    if notes is not None:
        fields.append("notes = ?")
        params.append(notes)
    if last_error is not None:
        fields.append("last_error = ?")
        params.append(last_error)
    if sso is not None:
        fields.append("sso = ?")
        params.append(sso)
    if email is not None:
        fields.append("email = ?")
        params.append(email)
    if password is not None:
        fields.append("password = ?")
        params.append(password)
    token_updates = {
        "session_token": session_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
    }
    if any(value is not None for value in token_updates.values()):
        try:
            extra = json.loads(_row_col(row, "extra_json", "{}") or "{}")
        except Exception:
            extra = {}
        for key, value in token_updates.items():
            if value is not None:
                extra[key] = value
        fields.append("extra_json = ?")
        params.append(json.dumps(extra, ensure_ascii=False))
    if not fields:
        return _account_row_to_dict(row)
    params.append(account_id)
    execute_no_return(
        f"UPDATE accounts SET {', '.join(fields)}, last_checked_at = ? WHERE id = ?",
        (*params[:-1], now_iso(), params[-1]),
    )
    row = fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    assert row is not None
    return _account_row_to_dict(row)


def account_delete(account_id: int) -> None:
    execute_no_return("DELETE FROM accounts WHERE id = ?", (account_id,))


def account_asset_summary() -> dict[str, Any]:
    total = fetch_one("SELECT COUNT(*) AS c FROM accounts")
    total_c = int(total["c"]) if total else 0

    def group_count(col: str) -> dict[str, int]:
        try:
            rows = fetch_all(f"SELECT {col} AS k, COUNT(*) AS c FROM accounts GROUP BY {col}")
        except sqlite3.OperationalError:
            return {}
        return {str(r["k"] or "unknown"): int(r["c"]) for r in rows}

    return {
        "total": total_c,
        "lifecycle_status": group_count("lifecycle_status"),
        "plan_state": group_count("plan_state"),
        "validity_status": group_count("validity_status"),
    }


# ================================================================
# 统计
# ================================================================

def stats_overview(days: int = 7) -> dict[str, Any]:
    total = fetch_one("SELECT COUNT(*) AS c FROM register_events")
    ok = fetch_one("SELECT COUNT(*) AS c FROM register_events WHERE ok = 1")
    fail = fetch_one("SELECT COUNT(*) AS c FROM register_events WHERE ok = 0")
    acc = fetch_one("SELECT COUNT(*) AS c FROM accounts")
    total_c = int(total["c"]) if total else 0
    ok_c = int(ok["c"]) if ok else 0
    fail_c = int(fail["c"]) if fail else 0
    success_rate = (ok_c / total_c * 100.0) if total_c else 0.0
    recent = fetch_all(
        """
        SELECT DATE(created_at) AS day,
               SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_count,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fail_count
        FROM register_events
        WHERE DATE(created_at) >= DATE('now', ?)
        GROUP BY DATE(created_at)
        ORDER BY day ASC
        """,
        (f"-{max(1, days)} days",),
    )
    trend = [
        {"day": r["day"], "ok": int(r["ok_count"] or 0), "fail": int(r["fail_count"] or 0)}
        for r in recent
    ]
    return {
        "total_events": total_c,
        "success_count": ok_c,
        "failure_count": fail_c,
        "success_rate": round(success_rate, 2),
        "account_count": int(acc["c"]) if acc else 0,
        "trend": trend,
    }


def stats_errors(days: int = 7) -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT COALESCE(error_kind, '') AS kind, COUNT(*) AS c, MAX(error_message) AS sample
        FROM register_events
        WHERE ok = 0 AND DATE(created_at) >= DATE('now', ?)
        GROUP BY error_kind
        ORDER BY c DESC
        LIMIT 50
        """,
        (f"-{max(1, days)} days",),
    )
    return [
        {
            "kind": (r["kind"] or "unknown") or "unknown",
            "count": int(r["c"] or 0),
            "sample": r["sample"] or "",
        }
        for r in rows
    ]


def stats_by_proxy() -> list[dict[str, Any]]:
    rows = fetch_all(
        """
        SELECT COALESCE(proxy_url, '') AS url,
               SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_count,
               SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS fail_count,
               COUNT(*) AS total
        FROM register_events
        WHERE proxy_url IS NOT NULL AND proxy_url != ''
        GROUP BY proxy_url
        ORDER BY total DESC
        """
    )
    out = []
    for r in rows:
        total = int(r["total"] or 0)
        ok_c = int(r["ok_count"] or 0)
        rate = (ok_c / total * 100.0) if total else 0.0
        out.append(
            {
                "proxy_url": r["url"] or "",
                "ok": ok_c,
                "fail": int(r["fail_count"] or 0),
                "total": total,
                "success_rate": round(rate, 2),
            }
        )
    return out


# ================================================================
# 日志 -> 事件 抽取 / 账号 sso 入库
# ================================================================

def _harvest_log_events(task_id: int, console_path: Path, last_line_no: int, proxy_url: str = "") -> int:
    """增量扫 console.log，把成功/失败事件写入 register_events。"""
    if not console_path.exists():
        return last_line_no
    try:
        text = console_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return last_line_no
    lines = text.splitlines()
    if len(lines) <= last_line_no:
        return last_line_no
    new_lines = lines[last_line_no:]
    pending_email = ""
    for line in new_lines:
        if m := LINE_RE_FILLED_EMAIL.search(line):
            pending_email = m.group(1)
        if m := LINE_RE_SUCCESS.search(line):
            email = m.group(1)
            log_register_event(
                task_id=task_id, ok=True, email=email, proxy_url=proxy_url,
            )
            pending_email = email
        elif m := LINE_RE_ERROR.search(line):
            err_msg = m.group(2).strip()
            log_register_event(
                task_id=task_id, ok=False,
                proxy_url=proxy_url,
                error_kind=classify_error(err_msg),
                error_message=err_msg[:500],
            )
    return len(lines)


def record_exporter_result(
    account_id: int,
    exporter_id: str,
    *,
    success: bool,
    message: str = "",
    data: dict[str, Any] | None = None,
) -> None:
    """Persist one exporter result without overwriting concurrent exporter state."""
    with db_lock, closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT exporter_status_json FROM accounts WHERE id = ?",
            (int(account_id),),
        ).fetchone()
        if not row:
            return
        try:
            status = json.loads(row["exporter_status_json"] or "{}")
        except Exception:
            status = {}
        status[str(exporter_id)] = {
            "ok": bool(success),
            "status": "pushed" if success else "failed",
            "message": str(message or "")[:1000],
            "last_pushed_at": now_iso(),
            "data": data if isinstance(data, dict) else {},
        }
        conn.execute(
            "UPDATE accounts SET exporter_status_json = ? WHERE id = ?",
            (json.dumps(status, ensure_ascii=False), int(account_id)),
        )
        conn.commit()


def push_account_to_grok2api(account_id: int):
    """Push a stored Grok account with the configured exporter and record the result."""
    setting = fetch_one("SELECT value FROM settings WHERE key = 'exporter_grok2api'")
    if not setting:
        return None
    try:
        raw_config = json.loads(setting["value"] or "{}")
    except Exception:
        raw_config = {}
    if not isinstance(raw_config, dict) or not raw_config.get("enabled"):
        return None

    account = fetch_one(
        "SELECT id, email, sso, password, extra_json FROM accounts WHERE id = ?",
        (int(account_id),),
    )
    if not account:
        return None

    from core.base_exporter import ExporterConfig, PushResult
    from exporters.grok2api import Grok2APIExporter

    extra_config = raw_config.get("extra")
    if not isinstance(extra_config, dict):
        extra_config = {}
    try:
        account_extra = json.loads(account["extra_json"] or "{}")
    except Exception:
        account_extra = {}
    config = ExporterConfig(
        exporter_id="grok2api",
        enabled=True,
        endpoint=str(raw_config.get("endpoint") or ""),
        api_append=bool(raw_config.get("api_append", True)),
        template=str(raw_config.get("template") or ""),
        extra=extra_config,
    )
    try:
        result = Grok2APIExporter().push(
            {
                "platform": "grok",
                "email": account["email"] or "",
                "password": account["password"] or "",
                "sso": account["sso"] or "",
                "extra": account_extra if isinstance(account_extra, dict) else {},
            },
            config,
        )
    except Exception as exc:
        result = PushResult(
            success=False,
            exporter_id="grok2api",
            message=f"grok2api push raised {type(exc).__name__}: {exc}",
        )
    record_exporter_result(
        int(account["id"]),
        "grok2api",
        success=bool(result.success),
        message=result.message,
        data=result.data,
    )
    return result


def _task_oauth_events(task_id: int, task_dir: Path) -> dict[str, dict[str, Any]]:
    """Return the latest registration-side OAuth event for each email."""
    path = task_dir / "sso" / f"task_{task_id}.oauth.jsonl"
    if not path.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}
    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        email = str(event.get("email") or "").strip().lower()
        if email:
            latest[email] = event
    return latest


def _harvest_task_accounts(task_id: int, task_dir: Path, proxy_url: str = "") -> None:
    """从 sso 文件把账号入库。"""
    sso_file = task_dir / "sso" / f"task_{task_id}.txt"
    if not sso_file.exists():
        return
    try:
        content = sso_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    email_rows = fetch_all(
        "SELECT email FROM register_events WHERE task_id = ? AND ok = 1 AND email != '' ORDER BY id ASC",
        (task_id,),
    )
    emails = [r["email"] for r in email_rows if r["email"]]
    sso_list = [s.strip() for s in content.splitlines() if s.strip()]
    oauth_events = _task_oauth_events(task_id, task_dir)
    passwords: dict[str, str] = {}
    console_path = task_dir / "console.log"
    if console_path.exists():
        try:
            log_text = console_path.read_text(encoding="utf-8", errors="replace")
            passwords = {
                match.group(1): match.group(2) or ""
                for match in LINE_RE_SUCCESS.finditer(log_text)
            }
        except Exception:
            passwords = {}
    # grok 子进程跑完没有 Account 对象可读，默认值如下：
    #   platform='grok'；token 能拿到说明注册流程完整 → validity='valid'
    #   plan_state 未知；lifecycle_status='registered'
    for idx, sso in enumerate(sso_list):
        email = emails[idx] if idx < len(emails) else ""
        password = passwords.get(email, "")
        existing = fetch_one(
            "SELECT id FROM accounts WHERE email = ? AND sso = ?",
            (email, sso),
        )
        execute_no_return(
            """
            INSERT OR IGNORE INTO accounts
                (email, sso, password, task_id, proxy_url, platform, status,
                 lifecycle_status, plan_state, validity_status,
                 last_checked_at, created_at)
            VALUES (?, ?, ?, ?, ?, 'grok', 'active',
                    'registered', 'unknown', 'valid',
                    ?, ?)
            """,
            (email, sso, password, task_id, proxy_url, now_iso(), now_iso()),
        )
        if password:
            execute_no_return(
                "UPDATE accounts SET password = CASE WHEN password = '' THEN ? ELSE password END "
                "WHERE email = ? AND sso = ?",
                (password, email, sso),
            )
        saved = fetch_one(
            "SELECT id FROM accounts WHERE email = ? AND sso = ?",
            (email, sso),
        )
        if saved:
            account_id = int(saved["id"])
            if existing is None:
                try:
                    from core.cpa_auth import write_grok2api_web_record

                    write_grok2api_web_record(
                        str(GROK2API_AUTH_DIR),
                        email,
                        sso,
                    )
                except Exception as artifact_exc:
                    with console_path.open("a", encoding="utf-8") as log:
                        log.write(
                            f"[{now_iso()}] [Grok2API Auth failed] {email}: "
                            f"{type(artifact_exc).__name__}: {artifact_exc}\n"
                        )
                try:
                    push_result = push_account_to_grok2api(account_id)
                    if push_result is not None:
                        marker = "API push succeeded" if push_result.success else "API push failed"
                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(f"[{now_iso()}] [{marker}] {email}: {push_result.message}\n")
                except Exception as push_exc:
                    with console_path.open("a", encoding="utf-8") as log:
                        log.write(
                            f"[{now_iso()}] [API push failed] {email}: "
                            f"{type(push_exc).__name__}: {push_exc}\n"
                        )
            try:
                from ._cpa_runtime import cpa_mint_runtime

                event = oauth_events.get(str(email).strip().lower())
                if event:
                    event_status = str(event.get("status") or "").lower()
                    if event_status == "success":
                        ok, oauth_error = cpa_mint_runtime.import_registered_oauth_token(
                            account_id,
                            event,
                        )
                        if not ok:
                            with console_path.open("a", encoding="utf-8") as log:
                                log.write(
                                    f"[{now_iso()}] [OAuth Auth import failed] {email}: "
                                    f"{oauth_error}\n"
                                )
                    elif event_status == "denied":
                        cpa_mint_runtime.record_registered_oauth_denied(account_id, event)
                    elif event_status == "failed" and cpa_mint_runtime.is_auto_enabled():
                        cpa_mint_runtime.enqueue(account_id)
                    # pending deliberately suppresses the slower cookie-only
                    # worker until the registration browser has finished.
                elif existing is None and cpa_mint_runtime.is_auto_enabled():
                    cpa_mint_runtime.enqueue(account_id)
            except Exception:
                pass


def export_accounts(fmt: str = "json") -> tuple[str, str, str]:
    rows = account_list(5000)
    fmt = (fmt or "json").lower()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "backup":
        return (
            json.dumps(rows, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
            f"grok-accounts-backup-{ts}.json",
        )
    if fmt == "csv":
        lines = [
            "id,email,sso,task_id,proxy_url,status,lifecycle_status,plan_state,validity_status,created_at"
        ]
        for r in rows:
            lines.append(
                f"{r['id']},{r['email']},{r['sso']},{r['task_id'] or ''},"
                f"{r['proxy_url']},{r['status']},"
                f"{r.get('lifecycle_status','')},{r.get('plan_state','')},"
                f"{r.get('validity_status','')},{r['created_at']}"
            )
        return "\n".join(lines) + "\n", "text/csv; charset=utf-8", f"grok-accounts-{ts}.csv"
    if fmt == "sso":
        content = "\n".join(r["sso"] for r in rows) + "\n"
        return content, "text/plain; charset=utf-8", f"grok-sso-{ts}.txt"
    exported = []
    for row in rows:
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        compact_tokens = {
            key: value
            for key, value in tokens.items()
            if value not in (None, "", [], {})
        }
        item = {
            "email": row.get("email", ""),
            "password": row.get("password", ""),
            "sso": row.get("sso", ""),
            "platform": row.get("platform", "grok"),
            "tokens": compact_tokens,
            "created_at": row.get("created_at", ""),
        }
        exported.append(item)
    return (
        json.dumps(exported, ensure_ascii=False, indent=2),
        "application/json; charset=utf-8",
        f"grok-accounts-{ts}.json",
    )


# ================================================================
# ManagedProcess 数据类（供 TaskSupervisor 使用）
# ================================================================

@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[Any]
    log_handle: Any

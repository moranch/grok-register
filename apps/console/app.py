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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
RUNTIME_DIR = APP_DIR / "runtime"
TASKS_DIR = RUNTIME_DIR / "tasks"
DB_PATH = RUNTIME_DIR / "console.db"
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))

SOURCE_PROJECT = Path(os.getenv("GROK_REGISTER_SOURCE_DIR", str(REPO_ROOT))).resolve()
SOURCE_VENV_PYTHON = Path(
    os.getenv("GROK_REGISTER_PYTHON", str(SOURCE_PROJECT / ".venv" / "bin" / "python"))
).expanduser()
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("GROK_REGISTER_CONSOLE_MAX_CONCURRENT_TASKS", "1")))
SUPERVISOR_INTERVAL = max(1.0, float(os.getenv("GROK_REGISTER_CONSOLE_POLL_INTERVAL", "2")))

PROJECT_FILES = ("DrissionPage_example.py", "email_register.py")
PROJECT_DIRS = ("turnstilePatch",)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_STOPPING = "stopping"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

LINE_RE_ROUND = re.compile(r"开始第\s*(\d+)\s*轮注册")
LINE_RE_SUCCESS = re.compile(r"注册成功\s*\|\s*email=([^|\s]+)")
LINE_RE_ERROR = re.compile(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)")
LINE_RE_TEMP_EMAIL = re.compile(r"临时邮箱创建成功:\s*([^\s]+)")
LINE_RE_FILLED_EMAIL = re.compile(r"已填写邮箱并点击注册:\s*([^\s]+)")
LINE_RE_PUSH = re.compile(r"SSO token 已推送到 API")

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
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db_lock, get_conn() as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db_lock, get_conn() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def execute_no_return(query: str, params: tuple[Any, ...] = ()) -> None:
    with db_lock, get_conn() as conn:
        conn.execute(query, params)
        conn.commit()


def init_db() -> None:
    ensure_dirs()
    with db_lock, get_conn() as conn:
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
                exit_code INTEGER
            );

            -- 代理池：支持多条代理，按成功率加权轮询；连续失败自动禁用
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

            -- 注册事件：每次成功/失败都记录一条，用于统计与趋势图
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

            -- 账号：持久化每一次注册成功的账号（邮箱 + sso token）
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                sso TEXT NOT NULL,
                task_id INTEGER,
                proxy_url TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(email, sso)
            );

            -- 邮箱 Provider 池：支持配置多套临时邮箱接口，按成功率加权挑一个给任务用
            -- provider_type: tmail / duckmail / moemail / custom
            CREATE TABLE IF NOT EXISTS mailbox_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'tmail',
                api_base TEXT NOT NULL,
                admin_password TEXT NOT NULL DEFAULT '',
                domain TEXT NOT NULL DEFAULT '',
                site_password TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


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
        # 只要未设置或显式空字符串，都不覆盖配置文件里的默认值
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

    # 调试模式：默认关闭
    base.setdefault("debug_mode", False)
    debug_env = os.getenv("GROK_REGISTER_DEFAULT_DEBUG_MODE")
    if debug_env is not None:
        base["debug_mode"] = debug_env.strip().lower() in {"1", "true", "yes", "on"}
    return base


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
        # 针对 SOCKS5 "Host unreachable" 给出更清晰的说明
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


class TaskCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    count: int = Field(50, ge=1, le=5000)
    proxy: str | None = None
    browser_proxy: str | None = None
    temp_mail_api_base: str | None = None
    temp_mail_admin_password: str | None = None
    temp_mail_domain: str | None = None
    temp_mail_site_password: str | None = None
    api_endpoint: str | None = None
    api_token: str | None = None
    api_append: bool | None = None
    # 调试模式：True=浏览器以"有头"方式跑（Xvfb 虚拟显示器）；False=完全无头
    # 单任务可覆盖
    debug_mode: bool | None = None
    # 执行器：headless（无头，默认）/ headed（有头，对应调试模式）
    # 预留 protocol（纯协议模式，后续接入 curl_cffi + 远程 Turnstile solver）
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
    # 调试模式：默认关闭（生产场景用无头）
    debug_mode: bool = False
    # 执行器：headless(默认) / headed / protocol
    executor: str = "headless"
    # Token 自动续期：关闭（默认）/ 开启；开启后会定期调用推送接口刷新账号池
    lifecycle_enabled: bool = False
    # 有效性检测间隔（小时），默认 6
    lifecycle_check_hours: int = 6


class ProxyItem(BaseModel):
    url: str = Field(..., min_length=1)
    label: str = ""
    enabled: bool = True


class ProxyUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    reset_stats: bool | None = None


# ---- 邮箱 Provider 池 ----

class MailboxItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    provider_type: str = Field("tmail", pattern="^(tmail|duckmail|moemail|custom)$")
    api_base: str = Field(..., min_length=1)
    admin_password: str = ""
    domain: str = ""
    site_password: str = ""
    enabled: bool = True


class MailboxUpdate(BaseModel):
    name: str | None = None
    provider_type: str | None = Field(None, pattern="^(tmail|duckmail|moemail|custom)$")
    api_base: str | None = None
    admin_password: str | None = None
    domain: str | None = None
    site_password: str | None = None
    enabled: bool | None = None
    reset_stats: bool | None = None


@dataclass
class ManagedProcess:
    task_id: int
    process: subprocess.Popen[Any]
    log_handle: Any


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
    if "api_endpoint" in saved:
        api_base["endpoint"] = str(saved.get("api_endpoint", ""))
    if "api_token" in saved:
        api_base["token"] = str(saved.get("api_token", ""))
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
        # 调试模式：单任务可覆盖；未传则沿用系统默认（defaults.debug_mode）
        "debug_mode": bool(defaults.get("debug_mode", False)) if payload.debug_mode is None else bool(payload.debug_mode),
        # 执行器：headless / headed / protocol
        "executor": str(defaults.get("executor", "headless")) if not payload.executor else str(payload.executor),
    }


def serialize_task(row: sqlite3.Row) -> dict[str, Any]:
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

    interesting = (
        "开始第",
        "临时邮箱创建成功",
        "已填写邮箱并点击注册",
        "提取到验证码",
        "已填写验证码",
        "最终注册页",
        "Turnstile",
        "已填写注册资料并点击完成注册",
        "注册成功",
        "[Error]",
        "已推送到 API",
    )

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
# 代理池 / 统计 / 账号 / 生命周期 辅助函数
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
    """按成功率加权从启用的代理里挑一个；连续失败 >=5 自动禁用"""
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


# --------- 邮箱 Provider 池 ----------

def _mailbox_from_row(row: sqlite3.Row) -> dict[str, Any]:
    total = int(row["success_count"]) + int(row["failure_count"])
    success_rate = (int(row["success_count"]) / total * 100.0) if total else 0.0
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "provider_type": row["provider_type"],
        "api_base": row["api_base"],
        "admin_password": row["admin_password"] or "",
        "domain": row["domain"] or "",
        "site_password": row["site_password"] or "",
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


def mailbox_add(payload: MailboxItem) -> dict[str, Any]:
    execute(
        """
        INSERT INTO mailbox_providers
            (name, provider_type, api_base, admin_password, domain, site_password, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            payload.provider_type,
            payload.api_base.strip(),
            payload.admin_password,
            payload.domain.strip(),
            payload.site_password,
            1 if payload.enabled else 0,
            now_iso(),
        ),
    )
    row = fetch_one(
        "SELECT * FROM mailbox_providers ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    return _mailbox_from_row(row)


def mailbox_update(mbox_id: int, payload: MailboxUpdate) -> dict[str, Any]:
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    new_name = row["name"] if payload.name is None else payload.name.strip()
    new_type = row["provider_type"] if payload.provider_type is None else payload.provider_type
    new_api = row["api_base"] if payload.api_base is None else payload.api_base.strip()
    new_admin = row["admin_password"] if payload.admin_password is None else payload.admin_password
    new_domain = row["domain"] if payload.domain is None else payload.domain.strip()
    new_site = row["site_password"] if payload.site_password is None else payload.site_password
    new_enabled = int(row["enabled"]) if payload.enabled is None else (1 if payload.enabled else 0)
    if payload.reset_stats:
        execute_no_return(
            """
            UPDATE mailbox_providers
            SET name = ?, provider_type = ?, api_base = ?, admin_password = ?,
                domain = ?, site_password = ?, enabled = ?,
                success_count = 0, failure_count = 0, consecutive_failures = 0
            WHERE id = ?
            """,
            (new_name, new_type, new_api, new_admin, new_domain, new_site, new_enabled, mbox_id),
        )
    else:
        execute_no_return(
            """
            UPDATE mailbox_providers
            SET name = ?, provider_type = ?, api_base = ?, admin_password = ?,
                domain = ?, site_password = ?, enabled = ?
            WHERE id = ?
            """,
            (new_name, new_type, new_api, new_admin, new_domain, new_site, new_enabled, mbox_id),
        )
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    assert row is not None
    return _mailbox_from_row(row)


def mailbox_delete(mbox_id: int) -> None:
    execute_no_return("DELETE FROM mailbox_providers WHERE id = ?", (mbox_id,))


def mailbox_pick_best() -> dict[str, Any] | None:
    """按成功率加权从启用的邮箱 provider 里挑一个；连续失败 >=5 自动禁用"""
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


# --------- 注册事件 / 账号 ----------

def classify_error(message: str) -> str:
    """把原始错误信息归并成少量可读的错误类型，便于聚合统计"""
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
    return [
        {
            "id": int(r["id"]),
            "email": r["email"],
            "sso": r["sso"],
            "task_id": r["task_id"],
            "proxy_url": r["proxy_url"] or "",
            "status": r["status"],
            "last_checked_at": r["last_checked_at"] or "",
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# --------- 统计 ----------

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


# --------- 日志 -> 事件 抽取 ----------

def _harvest_log_events(task_id: int, console_path: Path, last_line_no: int, proxy_url: str = "") -> int:
    """增量扫 console.log，把"注册成功""第 N 轮失败"写入 register_events；同时账号入库"""
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


# --------- 账号 sso 文件入库 ----------

def _harvest_task_accounts(task_id: int, task_dir: Path, proxy_url: str = "") -> None:
    """扫 task 目录下的 sso/task_{id}.txt，把新的 sso 入库。"""
    sso_file = task_dir / "sso" / f"task_{task_id}.txt"
    if not sso_file.exists():
        return
    try:
        content = sso_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    for sso in content.splitlines():
        sso = sso.strip()
        if not sso:
            continue
        # 我们暂时不从文件里拿 email（文件里只有 sso）
        # 直接以 sso 为主键入库，email 留空由事件表关联
        execute_no_return(
            """
            INSERT OR IGNORE INTO accounts (email, sso, task_id, proxy_url, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("", sso, task_id, proxy_url, now_iso()),
        )


# --------- 账号导出 ----------

def export_accounts(fmt: str = "json") -> tuple[str, str, str]:
    rows = account_list(5000)
    fmt = (fmt or "json").lower()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "csv":
        lines = ["id,email,sso,task_id,proxy_url,status,created_at"]
        for r in rows:
            lines.append(
                f"{r['id']},{r['email']},{r['sso']},{r['task_id'] or ''},"
                f"{r['proxy_url']},{r['status']},{r['created_at']}"
            )
        return "\n".join(lines) + "\n", "text/csv; charset=utf-8", f"grok-accounts-{ts}.csv"
    if fmt == "sso":
        content = "\n".join(r["sso"] for r in rows) + "\n"
        return content, "text/plain; charset=utf-8", f"grok-sso-{ts}.txt"
    return (
        json.dumps(rows, ensure_ascii=False, indent=2),
        "application/json; charset=utf-8",
        f"grok-accounts-{ts}.json",
    )


# ================================================================
# TaskSupervisor
# ================================================================

class TaskSupervisor:
    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        # 记录每个 task 本轮使用的代理 URL（来自代理池或 config.json 默认代理）
        self._task_proxy: dict[int, str] = {}
        # 记录每个 task 本轮挑中的邮箱 provider id（来自邮箱池）
        self._task_mailbox: dict[int, int] = {}
        # 记录每个 task console.log 已扫到的行号，用于增量抽取 register_events
        self._task_log_cursor: dict[int, int] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def stop_task(self, task_id: int) -> None:
        managed = self._processes.get(task_id)
        if not managed:
            row = task_row(task_id)
            if row["status"] == STATUS_QUEUED:
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (STATUS_STOPPED, now_iso(), "Task stopped before launch.", task_id),
                )
                return
            raise HTTPException(status_code=409, detail="Task is not running")
        execute_no_return(
            "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
            (STATUS_STOPPING, "Stopping task...", STATUS_STOPPING, task_id),
        )
        try:
            os.killpg(managed.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _running_count(self) -> int:
        return len(self._processes)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_running()
                self._launch_queued()
            except Exception:
                pass
            time.sleep(SUPERVISOR_INTERVAL)

    def _launch_queued(self) -> None:
        slots = MAX_CONCURRENT_TASKS - self._running_count()
        if slots <= 0:
            return
        queued = fetch_all(
            "SELECT * FROM tasks WHERE status = ? ORDER BY id ASC LIMIT ?",
            (STATUS_QUEUED, slots),
        )
        for row in queued:
            self._start_task(row)

    def _start_task(self, row: sqlite3.Row) -> None:
        task_id = int(row["id"])
        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        task_config = json.loads(row["config_json"])

        # 代理池：如果用户配置了启用的代理，就按成功率加权挑一个并覆盖 config 的 proxy/browser_proxy
        pool_proxy = proxy_pick_best()
        picked_proxy_url = ""
        if pool_proxy:
            picked_proxy_url = pool_proxy["url"]
            task_config["proxy"] = picked_proxy_url
            task_config["browser_proxy"] = picked_proxy_url

        # 邮箱 Provider 池：同上，覆盖 config 里的 temp_mail_* 字段
        pool_mailbox = mailbox_pick_best()
        picked_mailbox_id = 0
        if pool_mailbox:
            picked_mailbox_id = int(pool_mailbox["id"])
            task_config["temp_mail_api_base"] = pool_mailbox["api_base"]
            task_config["temp_mail_admin_password"] = pool_mailbox["admin_password"]
            task_config["temp_mail_domain"] = pool_mailbox["domain"]
            task_config["temp_mail_site_password"] = pool_mailbox["site_password"]
            # 写入 temp_mail_provider 给 email_register.py 用（如果它识别该字段）
            task_config["temp_mail_provider"] = pool_mailbox["provider_type"]

        copy_source_to_task_dir(task_dir, task_config)

        output_path = task_dir / "sso" / f"task_{task_id}.txt"
        command = [
            str(SOURCE_VENV_PYTHON),
            str(task_dir / "DrissionPage_example.py"),
            "--count",
            str(int(row["target_count"])),
            "--output",
            str(output_path),
        ]
        log_handle = console_path.open("a", encoding="utf-8")
        # 构造子进程环境变量
        task_env = os.environ.copy()
        debug_mode = bool(task_config.get("debug_mode", False))
        executor = str(task_config.get("executor", "headless"))
        # 执行器优先级 > 调试模式；executor=headed 等价于 debug_mode=True
        if executor == "headed":
            debug_mode = True
        elif executor == "headless":
            debug_mode = False
        task_env["GROK_DEBUG_MODE"] = "1" if debug_mode else "0"
        task_env["GROK_EXECUTOR"] = executor
        # 记录本轮使用的代理，方便日志收集 + 失败归因（写到子进程环境变量，
        # 也写到本进程的内存映射里，供 _refresh_running 时统计使用）
        if picked_proxy_url:
            task_env["GROK_TASK_PROXY_URL"] = picked_proxy_url
            self._task_proxy[task_id] = picked_proxy_url
        else:
            # 从 config.json 回落的默认代理
            fallback_proxy = str(task_config.get("browser_proxy") or task_config.get("proxy") or "")
            if fallback_proxy:
                self._task_proxy[task_id] = fallback_proxy
        # 记录本轮挑中的邮箱 provider id（任务结束时根据 success/failure 反馈）
        if picked_mailbox_id:
            self._task_mailbox[task_id] = picked_mailbox_id
        process = subprocess.Popen(
            command,
            cwd=task_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            env=task_env,
        )
        self._processes[task_id] = ManagedProcess(task_id=task_id, process=process, log_handle=log_handle)
        execute_no_return(
            """
            UPDATE tasks
            SET status = ?, pid = ?, started_at = ?, current_phase = ?, last_log_at = ?
            WHERE id = ?
            """,
            (STATUS_RUNNING, process.pid, now_iso(), "process_started", now_iso(), task_id),
        )

    def _refresh_running(self) -> None:
        finished: list[int] = []
        for task_id, managed in list(self._processes.items()):
            row = task_row(task_id)
            console_path = Path(row["console_path"])
            parsed = parse_console_state(console_path)
            # 增量采集 register_events（用于统计 + 代理成功率）
            proxy_url = self._task_proxy.get(task_id, "")
            cursor = self._task_log_cursor.get(task_id, 0)
            new_cursor = _harvest_log_events(task_id, console_path, cursor, proxy_url)
            if new_cursor != cursor:
                self._task_log_cursor[task_id] = new_cursor
                # 成功后从 sso 文件同步账号到 accounts 表
                try:
                    _harvest_task_accounts(task_id, Path(row["task_dir"]), proxy_url)
                except Exception:
                    pass
            execute_no_return(
                """
                UPDATE tasks
                SET completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"],
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            exit_code = managed.process.poll()
            if exit_code is None:
                continue
            final_status = STATUS_FAILED
            if row["status"] == STATUS_STOPPING or exit_code in (-15, -9):
                final_status = STATUS_STOPPED
            elif parsed["completed_count"] >= int(row["target_count"]) and exit_code == 0:
                final_status = STATUS_COMPLETED
            elif parsed["completed_count"] > 0:
                final_status = STATUS_PARTIAL
            execute_no_return(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, exit_code = ?,
                    completed_count = ?, failed_count = ?, current_round = ?, current_phase = ?,
                    last_email = ?, last_error = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    now_iso(),
                    exit_code,
                    parsed["completed_count"],
                    parsed["failed_count"],
                    parsed["current_round"],
                    parsed["current_phase"] or final_status,
                    parsed["last_email"],
                    parsed["last_error"],
                    parsed["last_log_at"],
                    task_id,
                ),
            )
            finished.append(task_id)
        for task_id in finished:
            managed = self._processes.pop(task_id, None)
            if managed and managed.log_handle:
                managed.log_handle.close()
            # 给邮箱 Provider 反馈本次任务的成功/失败（基于整体结果）
            mbox_id = self._task_mailbox.get(task_id, 0)
            if mbox_id:
                finished_row = task_row(task_id)
                final = finished_row["status"]
                if final in {STATUS_COMPLETED, STATUS_PARTIAL} and int(finished_row["completed_count"]) > 0:
                    mailbox_report_success(mbox_id)
                elif final in {STATUS_FAILED} and int(finished_row["completed_count"]) == 0:
                    mailbox_report_failure(mbox_id)
            # 清理本次任务的代理/邮箱/日志游标缓存
            self._task_proxy.pop(task_id, None)
            self._task_mailbox.pop(task_id, None)
            self._task_log_cursor.pop(task_id, None)


supervisor = TaskSupervisor()


CONSOLE_PASSWORD = os.getenv("GROK_REGISTER_CONSOLE_PASSWORD", "")


def check_auth(request: Request):
    if not CONSOLE_PASSWORD:
        return
    auth = request.headers.get("Authorization", "")
    cookie = request.cookies.get("console_password", "")
    if auth == f"Bearer {CONSOLE_PASSWORD}" or cookie == CONSOLE_PASSWORD:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    supervisor.start()
    # 启动生命周期管理线程（Token 续期/有效性检测）
    if not _lifecycle_thread.is_alive():
        _lifecycle_thread.start()
    try:
        yield
    finally:
        supervisor.stop()


app = FastAPI(title="Grok Register Console", lifespan=lifespan)

# 前端资源目录：
# - 优先读取 WEBUI_DIR 环境变量（Dockerfile 中构建时注入，默认 /opt/webui，
#   这个路径不会被 docker-compose 的 ./:/workspace volume 覆盖）
# - 如果没有就 fallback 到 apps/console/static（宿主机开发时可手动放前端产物）
WEBUI_DIR = Path(os.getenv("WEBUI_DIR", "")).expanduser() if os.getenv("WEBUI_DIR") else (APP_DIR / "static")
if not WEBUI_DIR.exists():
    WEBUI_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEBUI_DIR)), name="static")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    # 兼容旧链接：统一重定向到新前端的 /sign-in 路由
    return HTMLResponse(status_code=302, headers={"Location": "/sign-in"})


@app.post("/api/login")
def api_login(payload: dict):
    """
    新前端登录接口。
    请求体：{"password": "xxx"}
    成功返回：{"success": true}
    失败返回：401
    未启用密码时：直接 success
    """
    if not CONSOLE_PASSWORD:
        return {"success": True}
    password = str(payload.get("password", "")).strip()
    if password == CONSOLE_PASSWORD:
        return {"success": True}
    # 调试信息（不会暴露真实密码，只帮助定位问题）
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


@app.get("/api/auth/debug")
def api_auth_debug(request: Request):
    """
    临时调试接口：返回后端实际使用的密码长度和首尾字符，
    帮助定位"密码不对"问题。不暴露真实密码明文。
    上线后应移除此接口。
    """
    pw = CONSOLE_PASSWORD or ""
    return {
        "password_configured": bool(pw),
        "password_length": len(pw),
        "password_first_char": pw[:1] if pw else None,
        "password_last_char": pw[-1:] if pw else None,
        "env_var_name": "GROK_REGISTER_CONSOLE_PASSWORD",
    }


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """
    检查当前会话是否已认证。
    前端用来启动时判断是否需要跳登录页。
    """
    if not CONSOLE_PASSWORD:
        return {"auth_required": False, "authenticated": True}
    auth = request.headers.get("Authorization", "")
    cookie = request.cookies.get("console_password", "")
    authenticated = auth == f"Bearer {CONSOLE_PASSWORD}" or cookie == CONSOLE_PASSWORD
    return {"auth_required": True, "authenticated": authenticated}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # 新 React 前端自己处理登录重定向（通过 /api/auth/status 检查）
    # 所以 "/" 不做 check_auth，直接返回 HTML 骨架
    new_frontend = WEBUI_DIR / "index.html"
    if new_frontend.exists():
        return HTMLResponse(content=new_frontend.read_text(encoding="utf-8"))
    # 未找到前端产物（通常说明镜像构建时前端 build 失败），返回明确提示
    return HTMLResponse(
        status_code=503,
        content=(
            "<h1>前端资源未就绪</h1>"
            f"<p>未在 <code>{WEBUI_DIR}/index.html</code> 发现前端产物，"
            "请检查 Dockerfile 中 <code>grok-register-ui</code> 的构建步骤是否成功。</p>"
        ),
    )


@app.get("/api/meta")
def api_meta(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {
        "defaults": merged_defaults(),
        "settings": read_settings(),
        "source_project": str(SOURCE_PROJECT),
        "python_path": str(SOURCE_VENV_PYTHON),
        "max_concurrent_tasks": MAX_CONCURRENT_TASKS,
    }


@app.get("/api/health")
def api_health(request: Request) -> dict[str, Any]:
    check_auth(request)
    return run_health_checks()


@app.get("/api/settings")
def get_settings(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"settings": read_settings(), "defaults": merged_defaults()}


@app.post("/api/settings")
def save_settings(request: Request, payload: SystemSettings) -> dict[str, Any]:
    check_auth(request)
    saved = write_settings(payload)
    return {"settings": saved, "defaults": merged_defaults()}


@app.get("/api/tasks")
def list_tasks(request: Request) -> dict[str, Any]:
    check_auth(request)
    rows = fetch_all("SELECT * FROM tasks ORDER BY id DESC")
    return {"tasks": [serialize_task(row) for row in rows]}


@app.post("/api/tasks")
def create_task(request: Request, payload: TaskCreate) -> dict[str, Any]:
    check_auth(request)
    if not SOURCE_PROJECT.exists():
        raise HTTPException(status_code=500, detail=f"Source project not found: {SOURCE_PROJECT}")
    if not SOURCE_VENV_PYTHON.exists():
        raise HTTPException(status_code=500, detail=f"Python not found: {SOURCE_VENV_PYTHON}")
    task_config = build_task_config(payload)
    created_at = now_iso()
    task_id = execute(
        """
        INSERT INTO tasks (
            name, status, target_count, notes, config_json, task_dir, console_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.name.strip(),
            STATUS_QUEUED,
            payload.count,
            payload.notes.strip(),
            json.dumps(task_config, ensure_ascii=False),
            str(TASKS_DIR / "pending"),
            str(TASKS_DIR / "pending.log"),
            created_at,
        ),
    )
    task_dir = TASKS_DIR / f"task_{task_id}"
    console_path = task_dir / "console.log"
    task_dir.mkdir(parents=True, exist_ok=True)
    execute_no_return(
        "UPDATE tasks SET task_dir = ?, console_path = ? WHERE id = ?",
        (str(task_dir), str(console_path), task_id),
    )
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}")
def get_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    return {"task": serialize_task(task_row(task_id))}


@app.get("/api/tasks/{task_id}/logs")
def get_task_logs(
    request: Request,
    task_id: int,
    limit: int = Query(200, ge=20, le=1000),
) -> dict[str, Any]:
    check_auth(request)
    row = task_row(task_id)
    console_path = Path(row["console_path"])
    return {"lines": read_log_lines(console_path, limit=limit)}


@app.post("/api/tasks/{task_id}/stop")
def stop_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    supervisor.stop_task(task_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(request: Request, task_id: int) -> dict[str, Any]:
    check_auth(request)
    row = task_row(task_id)
    managed = supervisor._processes.get(task_id)
    if managed and managed.process.poll() is None:
        raise HTTPException(status_code=409, detail="Task is still running")
    delete_task_files(row)
    execute_no_return("DELETE FROM tasks WHERE id = ?", (task_id,))
    return {"ok": True}


# ================================================================
# 代理池 API
# ================================================================

@app.get("/api/proxies")
def api_list_proxies(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"proxies": proxy_list()}


@app.post("/api/proxies")
def api_add_proxy(request: Request, payload: ProxyItem) -> dict[str, Any]:
    check_auth(request)
    return {"proxy": proxy_add(payload.url, payload.label, payload.enabled)}


@app.patch("/api/proxies/{proxy_id}")
def api_update_proxy(request: Request, proxy_id: int, payload: ProxyUpdate) -> dict[str, Any]:
    check_auth(request)
    return {"proxy": proxy_update(proxy_id, payload.label, payload.enabled, payload.reset_stats)}


@app.delete("/api/proxies/{proxy_id}")
def api_delete_proxy(request: Request, proxy_id: int) -> dict[str, Any]:
    check_auth(request)
    proxy_delete(proxy_id)
    return {"ok": True}


# ================================================================
# 邮箱 Provider 池 API
# ================================================================

@app.get("/api/mailboxes")
def api_list_mailboxes(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"mailboxes": mailbox_list()}


@app.post("/api/mailboxes")
def api_add_mailbox(request: Request, payload: MailboxItem) -> dict[str, Any]:
    check_auth(request)
    return {"mailbox": mailbox_add(payload)}


@app.patch("/api/mailboxes/{mbox_id}")
def api_update_mailbox(
    request: Request, mbox_id: int, payload: MailboxUpdate
) -> dict[str, Any]:
    check_auth(request)
    return {"mailbox": mailbox_update(mbox_id, payload)}


@app.delete("/api/mailboxes/{mbox_id}")
def api_delete_mailbox(request: Request, mbox_id: int) -> dict[str, Any]:
    check_auth(request)
    mailbox_delete(mbox_id)
    return {"ok": True}


@app.post("/api/mailboxes/{mbox_id}/test")
def api_test_mailbox(request: Request, mbox_id: int) -> dict[str, Any]:
    """对单个邮箱 Provider 做一次可达性检测（访问 api_base）"""
    check_auth(request)
    row = fetch_one("SELECT * FROM mailbox_providers WHERE id = ?", (mbox_id,))
    if not row:
        raise HTTPException(status_code=404, detail="mailbox provider not found")
    target = (row["api_base"] or "").strip()
    if not target:
        return {"ok": False, "message": "api_base 未配置"}
    try:
        # 大多数临时邮箱 API 直接访问 base 会返回 200/401/404 之类
        response = _request_with_optional_proxy(target, timeout=15)
        ok = response.status_code < 500
        return {
            "ok": ok,
            "status_code": response.status_code,
            "message": f"HTTP {response.status_code}",
            "checked_at": now_iso(),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc), "checked_at": now_iso()}


# ================================================================
# 统计 API
# ================================================================

@app.get("/api/stats/overview")
def api_stats_overview(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    check_auth(request)
    return stats_overview(days)


@app.get("/api/stats/errors")
def api_stats_errors(
    request: Request, days: int = Query(7, ge=1, le=90)
) -> dict[str, Any]:
    check_auth(request)
    return {"items": stats_errors(days)}


@app.get("/api/stats/by-proxy")
def api_stats_by_proxy(request: Request) -> dict[str, Any]:
    check_auth(request)
    return {"items": stats_by_proxy()}


# ================================================================
# 账号 API（列表 / 导出）
# ================================================================

@app.get("/api/accounts")
def api_accounts(
    request: Request, limit: int = Query(500, ge=1, le=5000)
) -> dict[str, Any]:
    check_auth(request)
    return {"items": account_list(limit)}


@app.get("/api/accounts/export")
def api_accounts_export(
    request: Request,
    fmt: str = Query("json", pattern="^(json|csv|sso)$"),
):
    from fastapi.responses import Response as _Response
    check_auth(request)
    content, media_type, filename = export_accounts(fmt)
    return _Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ================================================================
# 实时日志 SSE（Server-Sent Events）
# ================================================================

@app.get("/api/tasks/{task_id}/stream")
def api_task_stream(request: Request, task_id: int):
    """SSE 实时推送 task console 日志。与老的 /logs 轮询接口并存。"""
    from fastapi.responses import StreamingResponse
    check_auth(request)
    row = task_row(task_id)
    console_path = Path(row["console_path"])

    def _event_gen():
        # 初始先把最后 200 行推回去
        last_size = 0
        if console_path.exists():
            initial = read_log_lines(console_path, limit=200)
            for line in initial:
                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
            try:
                last_size = console_path.stat().st_size
            except Exception:
                last_size = 0
        # 轮询追加（tail -f）
        last_ping = time.time()
        while True:
            try:
                if console_path.exists():
                    size = console_path.stat().st_size
                    if size > last_size:
                        with console_path.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            chunk = f.read()
                        last_size = size
                        for line in chunk.splitlines():
                            yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    elif size < last_size:
                        # 文件被截断或重建
                        last_size = 0
                # 每 15 秒发 ping 保持连接
                if time.time() - last_ping >= 15:
                    yield ": ping\n\n"
                    last_ping = time.time()
                time.sleep(1.0)
                # 任务已完成，再等一会儿把尾部 flush 完后退出
                status_row = fetch_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
                if status_row and status_row["status"] in {
                    STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_STOPPED,
                }:
                    time.sleep(1.5)
                    # 最后一次把未读内容读完
                    if console_path.exists():
                        size = console_path.stat().st_size
                        if size > last_size:
                            with console_path.open("r", encoding="utf-8", errors="replace") as f:
                                f.seek(last_size)
                                chunk = f.read()
                            for line in chunk.splitlines():
                                yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'status': status_row['status']})}\n\n"
                    return
            except GeneratorExit:
                return
            except Exception:
                time.sleep(2.0)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


# ================================================================
# 生命周期（Token 续期 / 有效性检测）
# ================================================================

_lifecycle_state: dict[str, Any] = {
    "last_check_at": "",
    "last_refresh_at": "",
    "last_result": "",
    "running": False,
}


def _lifecycle_check_accounts() -> dict[str, Any]:
    """简单的有效性检测：调用推送接口，确认 token sink 可用。"""
    defaults = merged_defaults()
    api_conf = dict(defaults.get("api") or {})
    endpoint = str(api_conf.get("endpoint", "") or "").strip()
    if not endpoint:
        return {"ok": False, "message": "未配置 token sink（api.endpoint），无法检测"}
    try:
        response = _request_with_optional_proxy(endpoint, timeout=10)
        ok = response.status_code in {200, 401, 403, 405}
        return {
            "ok": ok,
            "message": f"HTTP {response.status_code}",
            "endpoint": endpoint,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc), "endpoint": endpoint}


def _lifecycle_loop():
    while True:
        try:
            defaults = merged_defaults()
            enabled = bool(defaults.get("lifecycle_enabled", False))
            hours = max(1, int(defaults.get("lifecycle_check_hours", 6) or 6))
            if enabled:
                _lifecycle_state["running"] = True
                result = _lifecycle_check_accounts()
                _lifecycle_state["last_check_at"] = now_iso()
                _lifecycle_state["last_result"] = result.get("message", "")
                _lifecycle_state["running"] = False
                # 把所有账号的 last_checked_at 更新（simple，轻量）
                execute_no_return(
                    "UPDATE accounts SET last_checked_at = ? WHERE status = 'active'",
                    (now_iso(),),
                )
                time.sleep(hours * 3600)
            else:
                time.sleep(60)
        except Exception:
            time.sleep(60)


_lifecycle_thread = threading.Thread(target=_lifecycle_loop, daemon=True)


@app.get("/api/lifecycle/status")
def api_lifecycle_status(request: Request) -> dict[str, Any]:
    check_auth(request)
    defaults = merged_defaults()
    return {
        "enabled": bool(defaults.get("lifecycle_enabled", False)),
        "check_hours": int(defaults.get("lifecycle_check_hours", 6) or 6),
        **_lifecycle_state,
    }


@app.post("/api/lifecycle/check")
def api_lifecycle_check(request: Request) -> dict[str, Any]:
    """手动触发一次有效性检测"""
    check_auth(request)
    result = _lifecycle_check_accounts()
    _lifecycle_state["last_check_at"] = now_iso()
    _lifecycle_state["last_result"] = result.get("message", "")
    execute_no_return(
        "UPDATE accounts SET last_checked_at = ? WHERE status = 'active'",
        (now_iso(),),
    )
    return {**result, "checked_at": now_iso()}


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("GROK_REGISTER_CONSOLE_HOST", "127.0.0.1")
    port = int(os.getenv("GROK_REGISTER_CONSOLE_PORT", "18600"))
    uvicorn.run("app:app", host=host, port=port, reload=False)


# ---------- SPA Fallback ----------
# React 前端使用客户端路由（如 /sign-in、/dashboard、/tasks），
# 直接访问这些路径时 FastAPI 会找不到路由。
# 把所有未匹配到的 GET 请求都指向 index.html，让前端路由接管。
# 注意：这个必须放在所有 /api/* 路由之后定义。
@app.get("/{full_path:path}", response_class=HTMLResponse)
def spa_fallback(full_path: str) -> HTMLResponse:
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

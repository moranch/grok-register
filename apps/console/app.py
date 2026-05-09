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
    try:
        response = _request_with_optional_proxy(
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            proxy_url=xai_proxy,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        ok = response.status_code in {200, 301, 302, 303, 307, 308}
        detail = f"使用 `{_mask_proxy(xai_proxy)}` 访问注册页返回 HTTP {response.status_code}。" if xai_proxy else f"直连访问注册页返回 HTTP {response.status_code}。"
        if not ok and response.status_code in {401, 403, 429}:
            detail += " 这通常说明当前出口被目标站点拦截、限流，或还没完成可用的人机验证链路。"
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                ok,
                f"HTTP {response.status_code}",
                detail,
                "https://accounts.x.ai/sign-up?redirect=grok-com",
            )
        )
    except Exception as exc:
        items.append(
            _build_health_item(
                "xai",
                "x.ai Sign-up",
                False,
                "注册页不可达",
                f"访问 `x.ai` 注册页失败：{exc}",
                "https://accounts.x.ai/sign-up?redirect=grok-com",
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


class TaskSupervisor:
    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
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
        process = subprocess.Popen(
            command,
            cwd=task_dir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
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

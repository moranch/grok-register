"""
TaskSupervisor：后台线程，负责：
- 拉起 queued 任务（按代理池/邮箱池挑一个）
- 监控子进程状态，增量解析 console.log
- 成功/失败事件写入 register_events
- 新 sso 入库到 accounts

此模块从 app.py 中抽出，为 api/tasks.py 以及 lifespan 提供统一的 supervisor 单例。
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ._shared import (
    MAX_CONCURRENT_TASKS,
    SUPERVISOR_INTERVAL,
    SOURCE_VENV_PYTHON,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STATUS_STOPPING,
    ManagedProcess,
    _harvest_log_events,
    _harvest_task_accounts,
    copy_source_to_task_dir,
    execute_no_return,
    fetch_all,
    mailbox_pick_best,
    mailbox_report_failure,
    mailbox_report_success,
    now_iso,
    parse_console_state,
    proxy_pick_best,
    task_row,
)


class TaskSupervisor:
    """任务 supervisor：轮询 queued 任务并拉起子进程。"""

    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._task_proxy: dict[int, str] = {}
        self._task_mailbox: dict[int, int] = {}
        self._task_log_cursor: dict[int, int] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        if not self._thread.is_alive():
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

    # ── 内部辅助 ────────────────────────────────────────────────────

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

        # 多平台分派
        _row_keys = set(row.keys())
        platform_name = (
            str(row["platform"]) if "platform" in _row_keys else "grok"
        ).strip().lower() or "grok"

        if platform_name != "grok":
            # 非 grok 平台：在线程池里调 vendor register()
            self._start_vendor_task(task_id, row, platform_name)
            return

        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        task_config = json.loads(row["config_json"])

        pool_proxy = proxy_pick_best()
        picked_proxy_url = ""
        if pool_proxy:
            picked_proxy_url = pool_proxy["url"]
            task_config["proxy"] = picked_proxy_url
            task_config["browser_proxy"] = picked_proxy_url

        pool_mailbox = mailbox_pick_best()
        picked_mailbox_id = 0
        if pool_mailbox:
            picked_mailbox_id = int(pool_mailbox["id"])
            task_config["temp_mail_api_base"] = pool_mailbox["api_base"]
            task_config["temp_mail_admin_password"] = pool_mailbox["admin_password"]
            task_config["temp_mail_domain"] = pool_mailbox["domain"]
            task_config["temp_mail_site_password"] = pool_mailbox["site_password"]
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
        task_env = os.environ.copy()
        debug_mode = bool(task_config.get("debug_mode", False))
        executor = str(task_config.get("executor", "headless"))
        if executor == "headed":
            debug_mode = True
        elif executor == "headless":
            debug_mode = False
        task_env["GROK_DEBUG_MODE"] = "1" if debug_mode else "0"
        task_env["GROK_EXECUTOR"] = executor
        if picked_proxy_url:
            task_env["GROK_TASK_PROXY_URL"] = picked_proxy_url
            self._task_proxy[task_id] = picked_proxy_url
        else:
            fallback_proxy = str(task_config.get("browser_proxy") or task_config.get("proxy") or "")
            if fallback_proxy:
                self._task_proxy[task_id] = fallback_proxy
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
        self._processes[task_id] = ManagedProcess(
            task_id=task_id, process=process, log_handle=log_handle
        )
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
            proxy_url = self._task_proxy.get(task_id, "")
            cursor = self._task_log_cursor.get(task_id, 0)
            new_cursor = _harvest_log_events(task_id, console_path, cursor, proxy_url)
            if new_cursor != cursor:
                self._task_log_cursor[task_id] = new_cursor
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
            mbox_id = self._task_mailbox.get(task_id, 0)
            if mbox_id:
                finished_row = task_row(task_id)
                final = finished_row["status"]
                if final in {STATUS_COMPLETED, STATUS_PARTIAL} and int(finished_row["completed_count"]) > 0:
                    mailbox_report_success(mbox_id)
                elif final in {STATUS_FAILED} and int(finished_row["completed_count"]) == 0:
                    mailbox_report_failure(mbox_id)
            self._task_proxy.pop(task_id, None)
            self._task_mailbox.pop(task_id, None)
            self._task_log_cursor.pop(task_id, None)

    # ── 多平台 vendor dispatch ────────────────────────────────────────

    def _start_vendor_task(self, task_id: int, row: sqlite3.Row, platform_name: str) -> None:
        """
        非 grok 平台：在后台线程里调用 vendor 的 register() 方法。
        每轮注册一个账号，循环 target_count 次。
        成功的账号入库 accounts 表；失败记录到 last_error。
        """
        import threading as _threading

        target_count = int(row["target_count"])
        task_config = json.loads(row["config_json"])
        task_dir = Path(row["task_dir"])
        console_path = Path(row["console_path"])
        task_dir.mkdir(parents=True, exist_ok=True)

        # 读 engine_id / params
        _row_keys = set(row.keys())
        params_raw = row["params_json"] if "params_json" in _row_keys else "{}"
        try:
            params = json.loads(params_raw or "{}")
        except Exception:
            params = {}
        engine_id = (row["engine_id"] if "engine_id" in _row_keys else "") or params.get("engine_id", "")

        # 标记 running
        execute_no_return(
            "UPDATE tasks SET status = ?, started_at = ?, current_phase = ? WHERE id = ?",
            (STATUS_RUNNING, now_iso(), "vendor_dispatch", task_id),
        )

        def _run():
            completed = 0
            failed = 0
            last_error = ""
            last_email = ""

            try:
                from core.registry import PLATFORM_REGISTRY
                cls = PLATFORM_REGISTRY.get(platform_name)
                if cls is None:
                    raise RuntimeError(f"平台 '{platform_name}' 未在 registry 中找到")

                instance = cls()

                # 配置代理
                proxy_url = task_config.get("proxy", "") or task_config.get("browser_proxy", "")
                if hasattr(instance, "config") and instance.config is not None:
                    if hasattr(instance.config, "proxy"):
                        instance.config.proxy = proxy_url

                for round_no in range(1, target_count + 1):
                    execute_no_return(
                        "UPDATE tasks SET current_round = ?, current_phase = ?, last_log_at = ? WHERE id = ?",
                        (round_no, "registering", now_iso(), task_id),
                    )

                    # 写日志到 console_path
                    with console_path.open("a", encoding="utf-8") as log:
                        log.write(f"[{now_iso()}] 开始第 {round_no} 轮注册 (platform={platform_name}, engine={engine_id})\n")

                    try:
                        account = instance.register(email=None, password=None)
                        completed += 1
                        email = getattr(account, "email", "") or ""
                        token = getattr(account, "token", "") or ""
                        last_email = email

                        # 入库 accounts
                        from ._shared import execute as _execute
                        _execute(
                            """
                            INSERT OR IGNORE INTO accounts
                                (email, sso, password, task_id, proxy_url, status, platform, created_at)
                            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                            """,
                            (
                                email,
                                token,
                                getattr(account, "password", ""),
                                task_id,
                                proxy_url,
                                platform_name,
                                now_iso(),
                            ),
                        )

                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(f"[{now_iso()}] 注册成功 | email={email}\n")

                    except NotImplementedError as e:
                        failed += 1
                        last_error = f"NotImplementedError: {e}"
                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(f"[{now_iso()}] [Error] 第 {round_no} 轮失败: {last_error}\n")
                        # NotImplementedError 说明 vendor 没实现，不用继续
                        break

                    except Exception as e:
                        failed += 1
                        last_error = f"{type(e).__name__}: {e}"
                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(f"[{now_iso()}] [Error] 第 {round_no} 轮失败: {last_error}\n")

                    # 更新进度
                    execute_no_return(
                        """
                        UPDATE tasks SET completed_count = ?, failed_count = ?,
                            last_email = ?, last_error = ?, last_log_at = ?
                        WHERE id = ?
                        """,
                        (completed, failed, last_email, last_error, now_iso(), task_id),
                    )

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                with console_path.open("a", encoding="utf-8") as log:
                    log.write(f"[{now_iso()}] [Fatal] {last_error}\n")

            # 最终状态
            if completed >= target_count:
                final_status = STATUS_COMPLETED
            elif completed > 0:
                final_status = STATUS_PARTIAL
            else:
                final_status = STATUS_FAILED

            execute_no_return(
                """
                UPDATE tasks SET status = ?, finished_at = ?, completed_count = ?,
                    failed_count = ?, last_email = ?, last_error = ?,
                    current_phase = ?, last_log_at = ?
                WHERE id = ?
                """,
                (
                    final_status, now_iso(), completed, failed,
                    last_email, last_error, final_status, now_iso(), task_id,
                ),
            )

        # 在独立线程里跑，不阻塞 supervisor 主循环
        t = _threading.Thread(target=_run, daemon=True, name=f"vendor-{platform_name}-{task_id}")
        t.start()


# ── 全局单例 ──────────────────────────────────────────────────────────

supervisor = TaskSupervisor()

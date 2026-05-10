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
        self._vendor_threads: dict[int, threading.Event] = {}  # task_id -> stop_event
        self._task_proxy: dict[int, str] = {}
        self._task_mailbox: dict[int, int] = {}
        self._task_log_cursor: dict[int, int] = {}
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        # 启动时清理"孤儿 running 任务"——上次容器意外终止（docker kill / docker 崩溃 /
        # OOM / 宿主机重启）时留下的 running 状态记录。这些任务的真实线程/子进程早没了，
        # 但 DB 里仍然标记为 running，如果不清会卡死前端列表和下次 launch 的并发上限。
        try:
            self._reap_orphan_tasks_on_startup()
        except Exception as exc:
            print(f"[supervisor] 启动清理孤儿任务失败（不阻塞）: {exc}")

        if not self._thread.is_alive():
            self._thread.start()

    def _reap_orphan_tasks_on_startup(self) -> None:
        """任何 running / stopping 状态的任务，启动时都视为孤儿并标记 stopped。"""
        rows = fetch_all(
            "SELECT id FROM tasks WHERE status IN (?, ?, ?)",
            (STATUS_RUNNING, STATUS_STOPPING, "vendor_dispatch"),
        )
        if not rows:
            return
        for r in rows:
            task_id = int(r["id"])
            execute_no_return(
                """
                UPDATE tasks
                SET status = ?, finished_at = ?, last_error = ?, current_phase = ?
                WHERE id = ?
                """,
                (STATUS_STOPPED, now_iso(),
                 "Task stopped (orphan after container restart/crash).",
                 STATUS_STOPPED, task_id),
            )
        print(f"[supervisor] 启动时清理了 {len(rows)} 个孤儿任务")

    def stop(self) -> None:
        self._stop.set()

    def stop_task(self, task_id: int) -> None:
        # 先检查是否是 vendor 线程任务
        stop_event = self._vendor_threads.get(task_id)
        if stop_event is not None:
            stop_event.set()
            execute_no_return(
                "UPDATE tasks SET status = ?, last_error = ?, current_phase = ? WHERE id = ?",
                (STATUS_STOPPING, "Stopping vendor task...", STATUS_STOPPING, task_id),
            )
            return

        # 子进程任务（grok）
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
            # 如果数据库是 running 但内存里没有（孤儿任务，比如 restart 后），直接标 stopped
            if row["status"] == STATUS_RUNNING:
                execute_no_return(
                    """
                    UPDATE tasks
                    SET status = ?, finished_at = ?, last_error = ?, current_phase = ?
                    WHERE id = ?
                    """,
                    (STATUS_STOPPED, now_iso(), "Task stopped (orphan after restart).", STATUS_STOPPED, task_id),
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

        # 在独立线程里跑，不阻塞 supervisor 主循环
        stop_event = threading.Event()
        self._vendor_threads[task_id] = stop_event

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

                # 获取 vendor 原始平台类（绕过我们的 adapter wrapper）
                vendor_cls = None
                try:
                    vendor_mod = __import__(
                        f"platforms._vendor_aar.{platform_name}.plugin",
                        fromlist=["_"],
                    )
                    for _k, _v in vars(vendor_mod).items():
                        if (
                            isinstance(_v, type)
                            and _k.endswith("Platform")
                            and _k != "BasePlatform"
                            and hasattr(_v, "register")
                        ):
                            vendor_cls = _v
                            break
                except Exception:
                    pass

                # 优先用 vendor 原始类（它有完整的 register() 实现）
                if vendor_cls is not None:
                    from core._vendor_aar.base_platform import RegisterConfig
                    proxy_url = task_config.get("proxy", "") or task_config.get("browser_proxy", "")

                    # 确定执行器类型：
                    #   1. 读用户在前端选择的（tasks.executor_type 列 或 config_json.executor）
                    #   2. 回退到 protocol（协议模式，适合大多数有 API 的平台）
                    #   3. 最后用 vendor 支持列表的第一个
                    vendor_supported = getattr(vendor_cls, "supported_executors", []) or []

                    _row_keys_inner = set(row.keys())
                    user_exec = (
                        (row["executor_type"] if "executor_type" in _row_keys_inner else "")
                        or task_config.get("executor", "")
                        or task_config.get("executor_type", "")
                    )
                    user_exec = str(user_exec or "").strip().lower()

                    if user_exec and (not vendor_supported or user_exec in vendor_supported):
                        executor_type = user_exec
                    elif "protocol" in vendor_supported or not vendor_supported:
                        executor_type = "protocol"
                    else:
                        executor_type = "headless" if "headless" in vendor_supported else vendor_supported[0]

                    config = RegisterConfig(
                        proxy=proxy_url,
                        executor_type=executor_type,
                        extra={},
                    )
                    # 让用户在 console.log 里看到实际生效的 executor
                    try:
                        with console_path.open("a", encoding="utf-8") as _flog:
                            _flog.write(
                                f"[{now_iso()}] 使用执行器: {executor_type} "
                                f"(vendor 支持: {vendor_supported or '<unknown>'})\n"
                            )
                    except Exception:
                        pass
                    # 注入我们的邮箱桥接
                    from platforms._shared.mailbox_bridge import BridgeMailbox
                    mailbox = BridgeMailbox(proxy=proxy_url, stop_event=stop_event)
                    instance = vendor_cls(config=config, mailbox=mailbox)
                else:
                    # fallback: 用我们的 wrapper（可能报 RegistrationContext 错误）
                    instance = cls()
                    proxy_url = task_config.get("proxy", "") or task_config.get("browser_proxy", "")
                    if hasattr(instance, "config") and instance.config is not None:
                        if hasattr(instance.config, "proxy"):
                            instance.config.proxy = proxy_url

                # 把 vendor 的 self.log(...) 重定向到 console_path
                def _append_log(msg: str, _cp=console_path):
                    try:
                        with _cp.open("a", encoding="utf-8") as _lf:
                            _lf.write(f"[{now_iso()}] {msg}\n")
                    except Exception:
                        pass
                if hasattr(instance, "set_logger"):
                    try:
                        instance.set_logger(_append_log)
                    except Exception:
                        pass

                for round_no in range(1, target_count + 1):
                    # 检查停止信号
                    if stop_event.is_set():
                        last_error = "Task stopped by user"
                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(f"[{now_iso()}] [Stopped] 用户手动停止\n")
                        break

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

                        # 从 vendor Account 提取 lifecycle / plan / validity
                        # vendor AccountStatus 枚举值 = 我们 lifecycle_status 列的域
                        _status = getattr(account, "status", None)
                        lifecycle_status = str(
                            getattr(_status, "value", _status) or "registered"
                        )
                        extra = dict(getattr(account, "extra", {}) or {})
                        overview = extra.get("account_overview") or {}
                        if not isinstance(overview, dict):
                            overview = {}

                        # plan_state：用 vendor 的推导规则
                        try:
                            from core._vendor_aar.account_graph import (
                                _derive_plan_state,
                                _derive_validity_status,
                            )
                            _trial_end = int(getattr(account, "trial_end_time", 0) or 0)
                            plan_state = _derive_plan_state(
                                lifecycle_status, overview, _trial_end
                            ) or "unknown"
                            validity_status = _derive_validity_status(
                                lifecycle_status, overview
                            )
                            # 刚注册成功：如果 overview 没明确给 valid，
                            # 默认视作 valid（vendor 本轮能跑通说明 token 有效）
                            if validity_status == "unknown":
                                validity_status = "valid"
                        except Exception:
                            plan_state = "unknown"
                            validity_status = "valid"

                        extra_json = json.dumps(extra, ensure_ascii=False, default=str)

                        # 入库 accounts（一次性写全所有已知字段）
                        from ._shared import execute as _execute
                        _execute(
                            """
                            INSERT OR IGNORE INTO accounts
                                (email, sso, password, task_id, proxy_url, status, platform,
                                 lifecycle_status, plan_state, validity_status, extra_json,
                                 last_checked_at, created_at)
                            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                email,
                                token,
                                getattr(account, "password", ""),
                                task_id,
                                proxy_url,
                                platform_name,
                                lifecycle_status,
                                plan_state,
                                validity_status,
                                extra_json,
                                now_iso(),
                                now_iso(),
                            ),
                        )

                        with console_path.open("a", encoding="utf-8") as log:
                            log.write(
                                f"[{now_iso()}] 注册成功 | email={email} "
                                f"lifecycle={lifecycle_status} plan={plan_state} "
                                f"validity={validity_status}\n"
                            )

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
            if stop_event.is_set():
                final_status = STATUS_STOPPED
            elif completed >= target_count:
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
            # 清理
            self._vendor_threads.pop(task_id, None)

        # 在独立线程里跑，不阻塞 supervisor 主循环
        t = _threading.Thread(target=_run, daemon=True, name=f"vendor-{platform_name}-{task_id}")
        t.start()


# ── 全局单例 ──────────────────────────────────────────────────────────

supervisor = TaskSupervisor()

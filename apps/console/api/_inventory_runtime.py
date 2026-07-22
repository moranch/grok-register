"""Low-inventory monitor that queues Grok registration tasks."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

from . import _shared
from ._delivery_runtime import delivery_stock_snapshot
from ._shared import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_STOPPING,
    TaskCreate,
)
from ._task_runtime import enqueue_registration_task


SETTING_KEY = "auto_replenish"
TASK_SOURCE = "auto_replenish"
ACTIVE_TASK_STATUSES = ("initializing", STATUS_QUEUED, STATUS_RUNNING, STATUS_STOPPING, "vendor_dispatch")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def _environment_defaults() -> dict[str, Any]:
    return {
        "enabled": _as_bool(os.getenv("GROK_REGISTER_AUTO_REPLENISH_ENABLED"), True),
        "threshold": _as_int(os.getenv("GROK_REGISTER_AUTO_REPLENISH_THRESHOLD"), 100, 1, 100000),
        "replenish_count": _as_int(os.getenv("GROK_REGISTER_AUTO_REPLENISH_COUNT"), 100, 1, 5000),
        "check_interval_seconds": _as_int(
            os.getenv("GROK_REGISTER_AUTO_REPLENISH_CHECK_SECONDS"), 60, 10, 3600
        ),
        "cooldown_seconds": _as_int(
            os.getenv("GROK_REGISTER_AUTO_REPLENISH_COOLDOWN_SECONDS"), 300, 0, 86400
        ),
    }


def normalize_auto_replenish_config(values: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = _environment_defaults()
    source = dict(values or {})
    return {
        "enabled": _as_bool(source.get("enabled"), bool(defaults["enabled"])),
        "threshold": _as_int(source.get("threshold"), int(defaults["threshold"]), 1, 100000),
        "replenish_count": _as_int(
            source.get("replenish_count"), int(defaults["replenish_count"]), 1, 5000
        ),
        "check_interval_seconds": _as_int(
            source.get("check_interval_seconds"), int(defaults["check_interval_seconds"]), 10, 3600
        ),
        "cooldown_seconds": _as_int(
            source.get("cooldown_seconds"), int(defaults["cooldown_seconds"]), 0, 86400
        ),
    }


def read_auto_replenish_config() -> dict[str, Any]:
    row = _shared.fetch_one("SELECT value FROM settings WHERE key=?", (SETTING_KEY,))
    if not row:
        return normalize_auto_replenish_config()
    try:
        data = json.loads(row["value"] or "{}")
    except Exception:
        data = {}
    return normalize_auto_replenish_config(data if isinstance(data, dict) else {})


def save_auto_replenish_config(values: dict[str, Any]) -> dict[str, Any]:
    current = read_auto_replenish_config()
    current.update(values)
    config = normalize_auto_replenish_config(current)
    _shared.execute_no_return(
        """
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (SETTING_KEY, json.dumps(config, ensure_ascii=False), _shared.now_iso()),
    )
    return config


def _auto_task_row(*, active_only: bool) -> Any:
    params_json = "CASE WHEN json_valid(params_json) THEN params_json ELSE '{}' END"
    if active_only:
        placeholders = ",".join("?" for _ in ACTIVE_TASK_STATUSES)
        return _shared.fetch_one(
            f"""
            SELECT * FROM tasks
            WHERE platform='grok'
              AND status IN ({placeholders})
              AND json_extract({params_json}, '$.extra.source')=?
            ORDER BY id DESC LIMIT 1
            """,
            (*ACTIVE_TASK_STATUSES, TASK_SOURCE),
        )
    return _shared.fetch_one(
        f"""
        SELECT * FROM tasks
        WHERE platform='grok'
          AND json_extract({params_json}, '$.extra.source')=?
        ORDER BY id DESC LIMIT 1
        """,
        (TASK_SOURCE,),
    )


def _task_summary(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "status": str(row["status"] or ""),
        "target_count": int(row["target_count"] or 0),
        "completed_count": int(row["completed_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
        "created_at": str(row["created_at"] or ""),
        "finished_at": str(row["finished_at"] or "") if "finished_at" in keys else "",
    }


def _seconds_since(value: str) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(value)).total_seconds())
    except (TypeError, ValueError):
        return None


class AutoReplenishRuntime:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._check_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "last_check_at": "",
            "last_trigger_at": "",
            "last_reason": "not_started",
            "last_error": "",
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auto-replenish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def config(self) -> dict[str, Any]:
        return read_auto_replenish_config()

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        return save_auto_replenish_config(values)

    def _remember(self, *, reason: str, error: str = "", triggered: bool = False) -> None:
        now = _shared.now_iso()
        with self._state_lock:
            self._state["last_check_at"] = now
            self._state["last_reason"] = reason
            self._state["last_error"] = error[:500]
            if triggered:
                self._state["last_trigger_at"] = now

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            state = dict(self._state)
        stock = delivery_stock_snapshot()
        return {
            "config": self.config(),
            # Keep available_stock as a compatibility alias for candidate_stock.
            "available_stock": stock["candidate_stock"],
            **stock,
            "active_task": _task_summary(_auto_task_row(active_only=True)),
            "latest_auto_task": _task_summary(_auto_task_row(active_only=False)),
            **state,
        }

    def check_now(self, *, force: bool = False) -> dict[str, Any]:
        with self._check_lock:
            config = self.config()
            stock_snapshot = delivery_stock_snapshot()
            stock = int(stock_snapshot["candidate_stock"])
            stock_fields = {"available_stock": stock, **stock_snapshot}
            active_task = _task_summary(_auto_task_row(active_only=True))

            if not config["enabled"]:
                reason = "disabled"
                self._remember(reason=reason)
                return {"triggered": False, "reason": reason, **stock_fields, "config": config}

            if active_task:
                reason = "active_task"
                self._remember(reason=reason)
                return {
                    "triggered": False,
                    "reason": reason,
                    **stock_fields,
                    "config": config,
                    "task": active_task,
                }

            if stock >= config["threshold"]:
                reason = "stock_sufficient"
                self._remember(reason=reason)
                return {"triggered": False, "reason": reason, **stock_fields, "config": config}

            latest_task = _task_summary(_auto_task_row(active_only=False))
            if latest_task and not force and config["cooldown_seconds"] > 0:
                reference_time = latest_task.get("finished_at") or latest_task.get("created_at") or ""
                elapsed = _seconds_since(str(reference_time))
                if elapsed is not None and elapsed < config["cooldown_seconds"]:
                    reason = "cooldown"
                    self._remember(reason=reason)
                    return {
                        "triggered": False,
                        "reason": reason,
                        **stock_fields,
                        "config": config,
                        "cooldown_remaining_seconds": max(0, int(config["cooldown_seconds"] - elapsed)),
                        "task": latest_task,
                    }

            payload = TaskCreate(
                name=f"自动补货 · 候选库存 {stock}",
                count=config["replenish_count"],
                platform="grok",
                notes=(
                    f"系统自动创建：候选库存 {stock}，低于阈值 {config['threshold']}，"
                    f"本次补货 {config['replenish_count']}。"
                ),
                extra={
                    "source": TASK_SOURCE,
                    "trigger_stock": stock,
                    "trigger_stock_metric": "candidate_stock",
                    "verified_stock": stock_snapshot["verified_stock"],
                    "unverified_stock": stock_snapshot["unverified_stock"],
                    "threshold": config["threshold"],
                    "replenish_count": config["replenish_count"],
                },
            )
            try:
                task = enqueue_registration_task(payload)
            except Exception as exc:
                self._remember(reason="enqueue_failed", error=str(exc))
                raise
            task_summary = {
                key: task.get(key)
                for key in (
                    "id",
                    "name",
                    "status",
                    "target_count",
                    "completed_count",
                    "failed_count",
                    "created_at",
                    "finished_at",
                )
                if key in task
            }
            self._remember(reason="triggered", triggered=True)
            print(
                f"[auto-replenish] candidate={stock} verified={stock_snapshot['verified_stock']} "
                f"threshold={config['threshold']} "
                f"queued_task={task_summary.get('id')} count={config['replenish_count']}"
            )
            return {
                "triggered": True,
                "reason": "triggered",
                **stock_fields,
                "config": config,
                "task": task_summary,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_now()
            except Exception as exc:
                self._remember(reason="check_failed", error=str(exc))
                print(f"[auto-replenish] check failed: {exc}")
            try:
                interval = self.config()["check_interval_seconds"]
            except Exception:
                interval = 60
            self._stop.wait(max(10, int(interval)))


auto_replenish_runtime = AutoReplenishRuntime()

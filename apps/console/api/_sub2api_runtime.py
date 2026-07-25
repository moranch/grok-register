"""Independent Sub2API import queue with grouping and bounded retries."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from typing import Any

from core.cpa_auth import token_to_cpa_record
from core.sub2api_client import import_record, import_sso

from ._shared import execute_no_return, fetch_all, fetch_one, now_iso


class Sub2ApiImportRuntime:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, int, bool, str]] = queue.Queue(maxsize=5000)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending: set[int] = set()
        self._lock = threading.RLock()
        self._jobs_lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def config(self) -> dict[str, Any]:
        row = fetch_one("SELECT value FROM settings WHERE key='exporter_sub2api'")
        if not row:
            return {}
        try:
            payload = json.loads(row["value"] or "{}")
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        extra = dict(payload.get("extra") or {})
        base_url = str(payload.get("endpoint") or extra.get("base_url") or "").strip()
        if base_url:
            extra["base_url"] = base_url
        payload["extra"] = extra
        return payload

    def worker_count(self) -> int:
        config = self.config().get("extra") or {}
        raw = os.getenv("GROK_REGISTER_SUB2API_WORKERS") or config.get("workers") or 2
        try:
            return min(max(1, int(raw)), 8)
        except (TypeError, ValueError):
            return 2

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._run_worker, name=f"sub2api-import-{index + 1}", daemon=True)
            for index in range(self.worker_count())
        ]
        for thread in self._threads:
            thread.start()
        print(f"[sub2api-import] workers={len(self._threads)} queue_maxsize={self._queue.maxsize}")

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []

    def is_auto_enabled(self) -> bool:
        config = self.config()
        extra = config.get("extra") or {}
        return bool(config.get("enabled")) and bool(extra.get("auto_import", False))

    def enqueue(
        self,
        account_id: int,
        *,
        group_id: int = 0,
        force: bool = False,
        job_id: str = "",
    ) -> bool:
        account_id = int(account_id)
        with self._lock:
            if account_id in self._pending:
                return False
            self._pending.add(account_id)
        try:
            self._queue.put_nowait((account_id, int(group_id or 0), bool(force), job_id))
            self._set_stage(account_id, "queued", group_id=group_id, error="")
            return True
        except queue.Full:
            with self._lock:
                self._pending.discard(account_id)
            return False

    def enqueue_many(
        self,
        account_ids: list[int],
        *,
        group_id: int = 0,
        force: bool = False,
    ) -> dict[str, Any]:
        selected = list(dict.fromkeys(int(value) for value in account_ids if int(value) > 0))
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "total": len(selected),
            "queued": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
            "group_id": int(group_id or 0),
            "created_at": now_iso(),
        }
        with self._jobs_lock:
            self._jobs[job_id] = job
        for account_id in selected:
            queued = self.enqueue(
                account_id, group_id=group_id, force=force, job_id=job_id
            )
            job["queued" if queued else "skipped"] += 1
        if not selected or job["queued"] == 0:
            job["status"] = "completed"
            job["finished_at"] = now_iso()
        return self.job(job_id) or dict(job)

    def enqueue_backfill(
        self, *, limit: int = 0, group_id: int = 0, force: bool = False
    ) -> dict[str, Any]:
        rows = fetch_all(
            """
            SELECT id, extra_json
            FROM accounts
            WHERE platform='grok' AND status='active'
            ORDER BY id DESC
            """
        )
        selected: list[int] = []
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except Exception:
                extra = {}
            has_oauth = bool(extra.get("access_token") and extra.get("refresh_token"))
            has_sso = bool(extra.get("sso"))
            stage = ((extra.get("post_process") or {}).get("imports") or {}).get("sub2api") or {}
            if not (has_oauth or has_sso):
                continue
            if not force and stage.get("status") == "completed":
                continue
            selected.append(int(row["id"]))
            if limit > 0 and len(selected) >= limit:
                break
        return self.enqueue_many(selected, group_id=group_id, force=force)

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            source = self._jobs.get(job_id)
            if not source:
                return None
            result = dict(source)
            result["errors"] = list(source.get("errors") or [])
            return result

    def _finish_job(self, job_id: str, *, success: bool, error: str = "") -> None:
        if not job_id:
            return
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
            job["success" if success else "failed"] += 1
            if error and len(job["errors"]) < 20:
                job["errors"].append(error[:300])
            finished = job["success"] + job["failed"] + job["skipped"]
            if finished >= job["total"]:
                job["status"] = "completed"
                job["finished_at"] = now_iso()

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                account_id, group_id, force, job_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                ok, error = self._import_account(account_id, group_id=group_id, force=force)
                self._finish_job(job_id, success=ok, error=error)
            except Exception as exc:
                error = str(exc)[:500]
                self._set_stage(account_id, "failed", group_id=group_id, error=error)
                self._finish_job(job_id, success=False, error=error)
            finally:
                with self._lock:
                    self._pending.discard(account_id)
                self._queue.task_done()

    def _set_stage(
        self,
        account_id: int,
        status: str,
        *,
        group_id: int = 0,
        error: str = "",
        result: dict[str, Any] | None = None,
        attempts: int | None = None,
    ) -> None:
        row = fetch_one("SELECT extra_json, exporter_status_json FROM accounts WHERE id=?", (account_id,))
        if not row:
            return
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except Exception:
            extra = {}
        post_process = extra.get("post_process") if isinstance(extra.get("post_process"), dict) else {}
        imports = post_process.get("imports") if isinstance(post_process.get("imports"), dict) else {}
        current = imports.get("sub2api") if isinstance(imports.get("sub2api"), dict) else {}
        current.update(
            {
                "status": status,
                "group_id": int(group_id or 0) or None,
                "updated_at": now_iso(),
            }
        )
        if attempts is not None:
            current["attempts"] = attempts
        if error:
            current["error"] = error[:500]
        else:
            current.pop("error", None)
        if result:
            current["result"] = result
        imports["sub2api"] = current
        post_process["imports"] = imports
        extra["post_process"] = post_process
        try:
            exporter_status = json.loads(row["exporter_status_json"] or "{}")
        except Exception:
            exporter_status = {}
        exporter_status["sub2api"] = {
            "ok": status == "completed",
            "status": status,
            "message": error[:500] if error else "Sub2API 导入完成" if status == "completed" else status,
            "last_pushed_at": now_iso(),
            "data": result or {},
        }
        execute_no_return(
            "UPDATE accounts SET extra_json=?, exporter_status_json=? WHERE id=?",
            (
                json.dumps(extra, ensure_ascii=False),
                json.dumps(exporter_status, ensure_ascii=False),
                account_id,
            ),
        )

    @staticmethod
    def _record(row: Any, extra: dict[str, Any], base_url: str) -> dict[str, Any] | None:
        if not extra.get("access_token") or not extra.get("refresh_token"):
            return None
        token = {
            key: extra.get(key)
            for key in (
                "access_token",
                "refresh_token",
                "id_token",
                "token_type",
                "expires_in",
                "expired",
                "scope",
            )
            if extra.get(key) not in (None, "")
        }
        return token_to_cpa_record(token, str(row["email"] or ""), base_url=base_url)

    def _import_account(
        self, account_id: int, *, group_id: int = 0, force: bool = False
    ) -> tuple[bool, str]:
        row = fetch_one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            return False, "account not found"
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except Exception:
            extra = {}
        config = self.config()
        config_extra = dict(config.get("extra") or {})
        if not bool(config.get("enabled")) and not force:
            return False, "Sub2API integration is disabled"
        selected_group_id = int(group_id or config_extra.get("group_id") or 0)
        client_config = dict(config_extra)
        for runtime_key in (
            "group_id",
            "retries",
            "workers",
            "auto_import",
            "sso_fallback",
            "oauth_base_url",
        ):
            client_config.pop(runtime_key, None)
        try:
            retries = min(max(0, int(config_extra.get("retries", 2))), 5)
        except (TypeError, ValueError):
            retries = 2
        record = self._record(
            row,
            extra,
            str(config_extra.get("oauth_base_url") or "https://cli-chat-proxy.grok.com/v1"),
        )
        use_sso_fallback = bool(config_extra.get("sso_fallback", True))
        last_error = ""
        for attempt in range(1, retries + 2):
            self._set_stage(
                account_id,
                "running",
                group_id=selected_group_id,
                attempts=attempt,
                error=last_error,
            )
            if record:
                result = import_record(record, group_id=selected_group_id, **client_config)
            elif use_sso_fallback and row["sso"]:
                result = import_sso(
                    sso_token=str(row["sso"]),
                    email=str(row["email"] or ""),
                    group_id=selected_group_id,
                    **client_config,
                )
            else:
                result = {"ok": False, "retryable": False, "error": "账号缺少 OAuth token/SSO"}
            if result.get("ok"):
                self._set_stage(
                    account_id,
                    "completed",
                    group_id=selected_group_id,
                    result=result,
                    attempts=attempt,
                )
                return True, ""
            last_error = str(result.get("error") or "Sub2API import failed")[:500]
            if not result.get("retryable") or attempt > retries:
                break
            if self._stop.wait(min(2 ** (attempt - 1), 8)):
                break
        self._set_stage(
            account_id,
            "failed",
            group_id=selected_group_id,
            error=last_error,
            attempts=attempt,
        )
        return False, f"account_id={account_id}: {last_error}"


sub2api_import_runtime = Sub2ApiImportRuntime()

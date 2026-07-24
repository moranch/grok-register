"""Background CPA OAuth mint queue for newly registered and existing accounts."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from core.cpa_auth import (
    exchange_sso_for_token,
    probe_cpa_account,
    refresh_cpa_token,
    token_to_cpa_record,
    upload_cpa_record,
    write_cpa_record,
)

from ._shared import execute_no_return, fetch_all, fetch_one, now_iso


class CpaMintRuntime:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, bool, str]] = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._next_prevalidate_scan = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cpa-mint", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def config(self) -> dict[str, Any]:
        row = fetch_one("SELECT value FROM settings WHERE key = 'exporter_cpa'")
        if not row:
            return {}
        try:
            data = json.loads(row["value"] or "{}")
            if not isinstance(data, dict):
                return {}
            extra = dict(data.get("extra") or {})
            default_proxy = str(
                os.getenv("GROK_REGISTER_CPA_PROXY")
                or os.getenv("GROK_REGISTER_DEFAULT_PROXY")
                or ""
            ).strip()
            if default_proxy and not str(extra.get("proxy") or "").strip():
                extra["proxy"] = default_proxy
            data["extra"] = extra
            return data
        except Exception:
            return {}

    def is_auto_enabled(self) -> bool:
        config = self.config()
        extra = config.get("extra") or {}
        return bool(config.get("enabled")) and bool(extra.get("auto_mint", True))

    def prevalidation_ttl_minutes(self) -> int:
        extra = self.config().get("extra") or {}
        try:
            return min(max(5, int(extra.get("prevalidate_ttl_minutes", 60))), 1440)
        except (TypeError, ValueError):
            return 60

    def enqueue(self, account_id: int, *, force: bool = False, job_id: str = "") -> bool:
        account_id = int(account_id)
        with self._lock:
            if account_id in self._pending:
                return False
            self._pending.add(account_id)
        try:
            self._queue.put_nowait((account_id, force, job_id))
            return True
        except queue.Full:
            with self._lock:
                self._pending.discard(account_id)
            return False

    def enqueue_backfill(self, *, limit: int = 0, force: bool = False) -> dict[str, Any]:
        sql = "SELECT id, extra_json FROM accounts WHERE platform = 'grok' AND sso <> '' ORDER BY id ASC"
        rows = fetch_all(sql)
        selected: list[int] = []
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except Exception:
                extra = {}
            if not force and extra.get("refresh_token"):
                continue
            selected.append(int(row["id"]))
            if limit > 0 and len(selected) >= limit:
                break

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
            "created_at": now_iso(),
        }
        self._jobs[job_id] = job
        for account_id in selected:
            if self.enqueue(account_id, force=force, job_id=job_id):
                job["queued"] += 1
            else:
                job["skipped"] += 1
        if job["queued"] == 0:
            job["status"] = "completed"
        return dict(job)

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job else None

    def _finish_job(self, job_id: str, *, success: bool, error: str = "") -> None:
        if not job_id or job_id not in self._jobs:
            return
        job = self._jobs[job_id]
        job["status"] = "running"
        job["success" if success else "failed"] += 1
        if error and len(job["errors"]) < 20:
            job["errors"].append(error[:300])
        finished = job["success"] + job["failed"] + job["skipped"]
        if finished >= job["total"]:
            job["status"] = "completed"
            job["finished_at"] = now_iso()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._schedule_prevalidation()
            try:
                account_id, force, job_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                try:
                    ok, error = self._mint_account(account_id, force=force)
                except Exception as exc:
                    ok, error = False, str(exc)[:500]
                    print(f"[cpa-prevalidate] account={account_id} unexpected_error={error}")
                self._finish_job(job_id, success=ok, error=error)
            finally:
                with self._lock:
                    self._pending.discard(account_id)
                self._queue.task_done()

    def _schedule_prevalidation(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic < self._next_prevalidate_scan:
            return
        config = self.config()
        extra = config.get("extra") or {}
        try:
            scan_seconds = min(max(5, int(extra.get("prevalidate_scan_seconds", 30))), 3600)
        except (TypeError, ValueError):
            scan_seconds = 30
        self._next_prevalidate_scan = now_monotonic + scan_seconds
        if not bool(config.get("enabled")) or not bool(extra.get("prevalidate_enabled", True)):
            return
        try:
            batch_size = min(max(1, int(extra.get("prevalidate_batch_size", 10))), 100)
        except (TypeError, ValueError):
            batch_size = 10
        cutoff = (datetime.now() - timedelta(minutes=self.prevalidation_ttl_minutes())).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        retry_cutoff = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        rows = fetch_all(
            """
            SELECT a.id
            FROM accounts a
            WHERE a.platform='grok'
              AND a.status='active'
              AND a.sso <> ''
              AND a.lifecycle_status NOT IN ('expired', 'invalid')
              AND a.validity_status <> 'invalid'
              AND NOT EXISTS (
                  SELECT 1 FROM account_delivery_consumptions c WHERE c.account_id=a.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM account_delivery_leases l
                  WHERE l.account_id=a.id AND l.state IN ('probing','ready','packing')
              )
              AND (
                  COALESCE(json_extract(a.extra_json, '$.cpa.probe.probe_kind'), '')
                      NOT IN ('account_identity', 'account_response')
                  OR (
                      COALESCE(
                          json_extract(a.extra_json, '$.cpa.probe.account_alive'),
                          json_extract(a.extra_json, '$.cpa.probe.ok'),
                          0
                      ) = 1
                      AND COALESCE(
                          json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                          json_extract(a.extra_json, '$.cpa.updated_at'),
                          ''
                      ) < ?
                  )
                  OR (
                      COALESCE(
                          json_extract(a.extra_json, '$.cpa.probe.account_alive'),
                          json_extract(a.extra_json, '$.cpa.probe.ok'),
                          0
                      ) <> 1
                      AND COALESCE(
                          json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                          json_extract(a.extra_json, '$.cpa.updated_at'),
                          ''
                      ) < ?
                  )
              )
            ORDER BY COALESCE(
                         json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                         json_extract(a.extra_json, '$.cpa.updated_at'),
                         ''
                     ) ASC,
                     a.id ASC
            LIMIT ?
            """,
            (cutoff, retry_cutoff, batch_size),
        )
        for row in rows:
            self.enqueue(int(row["id"]), force=False)

    def _probe_existing_token(
        self,
        row: Any,
        extra: dict[str, Any],
        config_extra: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str]:
        access_token = str(extra.get("access_token") or "")
        if not access_token:
            return False, {}, "access_token is empty"
        try:
            timeout = min(max(5, int(config_extra.get("identity_timeout", 12))), 30)
        except (TypeError, ValueError):
            timeout = 12
        try:
            probe = probe_cpa_account(
                access_token,
                proxy=str(config_extra.get("proxy") or row["proxy_url"] or ""),
                timeout=timeout,
                verify_tls=bool(config_extra.get("verify_tls", True)),
            )
        except Exception as exc:
            return False, {}, str(exc)
        alive = bool(probe.get("account_alive", probe.get("ok")))
        return alive, probe, "" if alive else str(probe.get("error") or "account is unavailable")

    def _store_probe_success(
        self,
        account_id: int,
        extra: dict[str, Any],
        probe: dict[str, Any],
    ) -> None:
        checked_at = now_iso()
        cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
        cpa.update({"status": "ready", "probe": probe, "probe_checked_at": checked_at})
        extra["cpa"] = cpa
        execute_no_return(
            "UPDATE accounts SET extra_json=?, validity_status='valid', "
            "last_checked_at=?, last_error='' WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), checked_at, account_id),
        )

    def _store_probe_failure(
        self,
        account_id: int,
        extra: dict[str, Any],
        probe: dict[str, Any],
        error: str,
    ) -> None:
        checked_at = now_iso()
        cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
        cpa.update(
            {
                "status": "failed",
                "probe": probe,
                "probe_checked_at": checked_at,
                "probe_error": error[:500],
            }
        )
        extra["cpa"] = cpa
        banned = bool(probe.get("banned"))
        execute_no_return(
            "UPDATE accounts SET extra_json=?, "
            "validity_status=CASE WHEN ? THEN 'invalid' ELSE validity_status END, "
            "lifecycle_status=CASE WHEN ? THEN 'suspended' ELSE lifecycle_status END, "
            "last_checked_at=?, last_error=? WHERE id=?",
            (
                json.dumps(extra, ensure_ascii=False),
                banned,
                banned,
                checked_at,
                error[:500],
                account_id,
            ),
        )

    def _mint_account(self, account_id: int, *, force: bool) -> tuple[bool, str]:
        row = fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if not row:
            return False, "账号不存在"
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except Exception:
            extra = {}
        config = self.config()
        config_extra = config.get("extra") or {}
        last_probe: dict[str, Any] = {}
        last_probe_error = ""
        if not force and extra.get("refresh_token"):
            probe_ok, last_probe, last_probe_error = self._probe_existing_token(
                row, extra, config_extra
            )
            if probe_ok:
                self._store_probe_success(account_id, extra, last_probe)
                return True, ""
            if last_probe and not bool(last_probe.get("refresh_recommended")):
                self._store_probe_failure(account_id, extra, last_probe, last_probe_error)
                return False, last_probe_error

        endpoint = str(config.get("endpoint") or config_extra.get("endpoint") or "").strip()
        auth_dir = str(config_extra.get("auth_dir") or "").strip()
        if not endpoint and not auth_dir:
            return False, "CPA 未配置 endpoint 或 auth_dir"
        management_key = str(config_extra.get("management_key") or "").strip()
        if endpoint and not management_key:
            return False, "CPA 未配置 management_key"

        try:
            timeout = max(30, int(config_extra.get("timeout", 90)))
            verify_tls = bool(config_extra.get("verify_tls", True))
            proxy = str(config_extra.get("proxy") or row["proxy_url"] or "")
            token: dict[str, Any] | None = None
            mint_method = "device_flow"
            refresh_error = ""

            # access_token 过期时优先走标准 refresh grant。只有 refresh_token
            # 本身失效或网络请求失败时，才回退到耗时更长的完整 SSO device flow。
            saved_refresh_token = str(extra.get("refresh_token") or "").strip()
            if saved_refresh_token:
                try:
                    token = refresh_cpa_token(
                        saved_refresh_token,
                        proxy=proxy,
                        timeout=min(timeout, 30),
                        verify_tls=verify_tls,
                    )
                    mint_method = "refresh_token"
                except Exception as exc:
                    refresh_error = str(exc)[:500]

            if token is None:
                try:
                    token = exchange_sso_for_token(
                        str(row["sso"] or extra.get("sso") or ""),
                        sso_rw=str(extra.get("sso_rw") or ""),
                        proxy=proxy,
                        timeout=timeout,
                        verify_tls=verify_tls,
                        cancel=self._stop.is_set,
                    )
                except Exception as exc:
                    if refresh_error:
                        raise RuntimeError(
                            f"refresh_token 续期失败: {refresh_error}; "
                            f"SSO 重新授权失败: {exc}"
                        ) from exc
                    raise
            record = token_to_cpa_record(
                token,
                str(row["email"] or ""),
                base_url=str(config_extra.get("base_url") or "https://cli-chat-proxy.grok.com/v1"),
            )
            probe = None
            if bool(config_extra.get("probe", True)):
                probe = probe_cpa_account(
                    record["access_token"],
                    proxy=proxy,
                    timeout=min(max(5, int(config_extra.get("identity_timeout", 12))), 30),
                    verify_tls=verify_tls,
                )
                if bool(config_extra.get("probe_required", True)) and not bool(
                    probe.get("account_alive", probe.get("ok"))
                ):
                    raise RuntimeError("账号存活探测失败")

            # 只有自动验活通过后才落地/上传交付凭据。失败账号不生成 CPA，
            # DownloadGate 也不会从未验活库存派生 Sub2API/Cockpit 文件。
            filename = ""
            destinations: list[str] = []
            if auth_dir:
                filename = write_cpa_record(auth_dir, record).name
                destinations.append("local")
            if endpoint:
                filename = upload_cpa_record(
                    endpoint,
                    management_key,
                    record,
                    timeout=min(timeout, 60),
                    verify_tls=verify_tls,
                )
                destinations.append("remote")

            for key in (
                "access_token", "refresh_token", "id_token", "token_type", "expires_in",
                "expired", "sub", "base_url", "token_endpoint", "redirect_uri", "headers",
            ):
                if key in record:
                    extra[key] = record[key]
            probe_alive = probe is None or bool(probe.get("account_alive", probe.get("ok")))
            extra["cpa"] = {
                "status": "ready" if probe_alive else "failed",
                "filename": filename,
                "destinations": destinations,
                "mint_method": mint_method,
                "probe": probe,
                "updated_at": now_iso(),
            }
            if probe is not None:
                extra["cpa"]["probe_checked_at"] = now_iso()
            status = {"cpa": {"status": "pushed", "message": "OAuth 补全成功", "last_pushed_at": now_iso()}}
            execute_no_return(
                "UPDATE accounts SET extra_json = ?, exporter_status_json = ?, "
                "validity_status = CASE WHEN ? THEN 'invalid' WHEN ? THEN 'valid' ELSE validity_status END, "
                "lifecycle_status = CASE WHEN ? THEN 'suspended' ELSE lifecycle_status END, "
                "last_checked_at = ?, last_error = ? WHERE id = ?",
                (
                    json.dumps(extra, ensure_ascii=False),
                    json.dumps(status, ensure_ascii=False),
                    bool(probe and probe.get("banned")),
                    probe_alive,
                    bool(probe and probe.get("banned")),
                    now_iso(),
                    "" if probe_alive else str((probe or {}).get("error") or "")[:500],
                    account_id,
                ),
            )
            return True, ""
        except Exception as exc:
            failed_cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
            failed_cpa.update({"status": "failed", "error": str(exc)[:500], "updated_at": now_iso()})
            if last_probe:
                failed_cpa["probe"] = last_probe
                failed_cpa["probe_checked_at"] = now_iso()
            if last_probe_error:
                failed_cpa["probe_error"] = last_probe_error[:500]
            extra["cpa"] = failed_cpa
            execute_no_return(
                "UPDATE accounts SET extra_json = ?, exporter_status_json = ? WHERE id = ?",
                (
                    json.dumps(extra, ensure_ascii=False),
                    json.dumps({"cpa": {"status": "failed", "message": str(exc)[:500], "last_pushed_at": now_iso()}}, ensure_ascii=False),
                    account_id,
                ),
            )
            return False, f"account_id={account_id}: {exc}"


cpa_mint_runtime = CpaMintRuntime()

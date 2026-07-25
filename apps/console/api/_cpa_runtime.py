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
    probe_grok_account_session,
    refresh_cpa_token,
    token_to_cpa_record,
    upload_cpa_record,
    write_cpa_record,
)

from ._shared import execute_no_return, fetch_all, fetch_one, now_iso

DEFAULT_CPA_WORKERS = 6
MAX_CPA_WORKERS = 16
CPA_QUEUE_MAXSIZE = 5000


def _permanent_credential_failure(error: str) -> bool:
    """Return true only when both renewal paths rejected the credential."""
    lowered = str(error or "").lower()
    transient_markers = (
        "timed out",
        "timeout",
        "connection",
        "network",
        "proxy",
        "temporarily",
        "cancelled",
        "canceled",
    )
    if any(marker in lowered for marker in transient_markers):
        return False
    refresh_failed = "refresh_token" in lowered and (
        "access denied" in lowered or "invalid_grant" in lowered
    )
    sso_failed = "sso" in lowered and (
        "invalid_grant" in lowered or "access denied" in lowered
    )
    return refresh_failed and sso_failed


class CpaMintRuntime:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[int, bool, str]] = queue.Queue(
            maxsize=CPA_QUEUE_MAXSIZE
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._scheduler_thread: threading.Thread | None = None
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._next_prevalidate_scan = 0.0

    def worker_count(self) -> int:
        configured = os.getenv("GROK_REGISTER_CPA_WORKERS", "").strip()
        if not configured:
            try:
                configured = str((self.config().get("extra") or {}).get("workers") or "")
            except Exception:
                configured = ""
        try:
            return min(max(1, int(configured or DEFAULT_CPA_WORKERS)), MAX_CPA_WORKERS)
        except (TypeError, ValueError):
            return DEFAULT_CPA_WORKERS

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        quarantined = self._quarantine_known_permanent_failures()
        if quarantined:
            print(f"[cpa-prevalidate] quarantined_permanent={quarantined}")
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._run_worker,
                name=f"cpa-mint-{index + 1}",
                daemon=True,
            )
            for index in range(self.worker_count())
        ]
        print(
            f"[cpa-prevalidate] workers={len(self._threads)} "
            f"queue_maxsize={self._queue.maxsize}"
        )
        for thread in self._threads:
            thread.start()
        self._scheduler_thread = threading.Thread(
            target=self._run_scheduler,
            name="cpa-mint-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def _quarantine_known_permanent_failures(self) -> int:
        """Remove already-proven unusable OAuth credentials from delivery stock."""
        rows = fetch_all(
            """
            SELECT id, extra_json, last_error
            FROM accounts
            WHERE platform='grok' AND validity_status <> 'invalid'
            """
        )
        quarantined = 0
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except Exception:
                extra = {}
            cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
            error = str(cpa.get("error") or cpa.get("probe_error") or row["last_error"] or "")
            if not _permanent_credential_failure(error):
                continue
            cpa["status"] = "failed"
            cpa["credential_ready"] = False
            cpa["failure_kind"] = "credential_invalid"
            extra["cpa"] = cpa
            execute_no_return(
                "UPDATE accounts SET extra_json=?, validity_status='invalid', "
                "last_checked_at=?, last_error=? WHERE id=?",
                (
                    json.dumps(extra, ensure_ascii=False),
                    now_iso(),
                    error[:500],
                    int(row["id"]),
                ),
            )
            quarantined += 1
        return quarantined

    def stop(self) -> None:
        self._stop.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        self._scheduler_thread = None
        self._threads = []

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
        # Backfill can include historical/consumed accounts, but those accounts can
        # never increase the pickup inventory.  Put currently deliverable accounts
        # first so a large historical table does not keep verified_stock at zero
        # while workers spend minutes renewing credentials that cannot be assigned.
        sql = """
            SELECT a.id, a.extra_json
            FROM accounts a
            WHERE a.platform = 'grok' AND a.sso <> ''
            ORDER BY
                CASE WHEN
                    a.status = 'active'
                    AND a.lifecycle_status NOT IN ('expired', 'invalid')
                    AND a.validity_status <> 'invalid'
                    AND NOT EXISTS (
                        SELECT 1 FROM account_delivery_consumptions c
                        WHERE c.account_id = a.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM account_delivery_leases l
                        WHERE l.account_id = a.id
                          AND l.state IN ('probing', 'ready', 'packing')
                    )
                THEN 0 ELSE 1 END ASC,
                CASE WHEN COALESCE(
                    json_extract(a.extra_json, '$.cpa.credential_ready'), 0
                ) <> 1 THEN 0 ELSE 1 END ASC,
                COALESCE(
                    json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                    json_extract(a.extra_json, '$.cpa.updated_at'),
                    ''
                ) ASC,
                a.id ASC
        """
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
        with self._jobs_lock:
            self._jobs[job_id] = job
        for account_id in selected:
            queued = self.enqueue(account_id, force=force, job_id=job_id)
            with self._jobs_lock:
                job["queued" if queued else "skipped"] += 1
        with self._jobs_lock:
            finished = job["success"] + job["failed"] + job["skipped"]
            if job["queued"] == 0 or finished >= job["total"]:
                job["status"] = "completed"
                job["finished_at"] = now_iso()
            result = dict(job)
            result["errors"] = list(job["errors"])
            return result

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            result = dict(job)
            result["errors"] = list(job.get("errors") or [])
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

    def _run_scheduler(self) -> None:
        while not self._stop.is_set():
            try:
                self._schedule_prevalidation()
            except Exception as exc:
                print(f"[cpa-prevalidate] scheduler_error={str(exc)[:500]}")
            self._stop.wait(1)

    def _run_worker(self) -> None:
        while not self._stop.is_set():
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
                      NOT IN ('account_identity', 'account_response', 'account_session')
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
                  OR (
                      COALESCE(json_extract(a.extra_json, '$.cpa.credential_ready'), 0) <> 1
                      AND COALESCE(
                          json_extract(a.extra_json, '$.cpa.updated_at'),
                          json_extract(a.extra_json, '$.cpa.probe_checked_at'),
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
            (cutoff, retry_cutoff, retry_cutoff, batch_size),
        )
        for row in rows:
            self.enqueue(int(row["id"]), force=False)

    def _probe_existing_token(
        self,
        row: Any,
        extra: dict[str, Any],
        config_extra: dict[str, Any],
    ) -> tuple[bool, dict[str, Any], str]:
        try:
            timeout = min(max(5, int(config_extra.get("identity_timeout", 12))), 30)
        except (TypeError, ValueError):
            timeout = 12
        proxy = str(config_extra.get("proxy") or row["proxy_url"] or "")
        verify_tls = bool(config_extra.get("verify_tls", True))
        session_probe: dict[str, Any] = {}
        session_error = ""
        sso = str(row["sso"] or extra.get("sso") or "").strip()
        if sso:
            try:
                session_probe = probe_grok_account_session(
                    sso,
                    sso_rw=str(extra.get("sso_rw") or ""),
                    proxy=proxy,
                    timeout=timeout,
                    verify_tls=verify_tls,
                )
                session_alive = bool(
                    session_probe.get("account_alive", session_probe.get("ok"))
                )
                if session_alive:
                    return True, session_probe, ""
                session_error = str(
                    session_probe.get("error") or "Grok account session unavailable"
                )
            except Exception as exc:
                session_error = str(exc)

        # SSO 才是 Grok 账号存活的主口径。只有 SSO 不可用时，才用现有
        # OAuth access_token 作为兼容兜底，避免过期 token 拖慢整批验活。
        access_token = str(extra.get("access_token") or "")
        token_probe: dict[str, Any] = {}
        token_error = "access_token is empty" if not access_token else ""
        if access_token:
            try:
                token_probe = probe_cpa_account(
                    access_token,
                    proxy=proxy,
                    timeout=timeout,
                    verify_tls=verify_tls,
                )
                if bool(token_probe.get("account_alive", token_probe.get("ok"))):
                    return True, token_probe, ""
                token_error = str(token_probe.get("error") or "OAuth identity unavailable")
            except Exception as exc:
                token_error = str(exc)

        if session_probe:
            return False, session_probe, session_error
        combined_error = "; ".join(part for part in (session_error, token_error) if part)
        return False, token_probe, combined_error or "account is unavailable"

    def _store_probe_success(
        self,
        account_id: int,
        extra: dict[str, Any],
        probe: dict[str, Any],
    ) -> None:
        checked_at = now_iso()
        cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
        cpa.update({"status": "ready", "probe": probe, "probe_checked_at": checked_at})
        cpa["credential_ready"] = True
        cpa.pop("error", None)
        cpa.pop("probe_error", None)
        cpa.pop("renewal_error", None)
        cpa.pop("failure_kind", None)
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
                "credential_ready": False,
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
        known_alive = False
        if row["sso"] or extra.get("access_token") or extra.get("refresh_token"):
            probe_ok, last_probe, last_probe_error = self._probe_existing_token(
                row, extra, config_extra
            )
            if probe_ok:
                known_alive = True
                if last_probe.get("probe_kind") == "account_identity":
                    self._store_probe_success(account_id, extra, last_probe)
                    if not force:
                        return True, ""
            elif (
                not force
                and last_probe
                and not extra.get("refresh_token")
                and not bool(last_probe.get("refresh_recommended"))
            ):
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
            if known_alive:
                probe = last_probe
            elif bool(config_extra.get("probe", True)):
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
                "credential_ready": probe_alive,
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
            error_text = str(exc)
            permanent_failure = _permanent_credential_failure(error_text)
            failed_cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
            failed_cpa.update(
                {
                    "status": "failed",
                    "credential_ready": False,
                    "failure_kind": (
                        "credential_invalid" if permanent_failure else "renewal_failed"
                    ),
                    "error": error_text[:500],
                    "updated_at": now_iso(),
                }
            )
            if last_probe:
                failed_cpa["probe"] = last_probe
                failed_cpa["probe_checked_at"] = now_iso()
            if last_probe_error:
                failed_cpa["probe_error"] = last_probe_error[:500]
            extra["cpa"] = failed_cpa
            execute_no_return(
                "UPDATE accounts SET extra_json=?, exporter_status_json=?, "
                "validity_status=CASE WHEN ? THEN 'invalid' ELSE validity_status END, "
                "last_checked_at=?, last_error=? WHERE id=?",
                (
                    json.dumps(extra, ensure_ascii=False),
                    json.dumps({"cpa": {"status": "failed", "message": error_text[:500], "last_pushed_at": now_iso()}}, ensure_ascii=False),
                    permanent_failure,
                    now_iso(),
                    error_text[:500],
                    account_id,
                ),
            )
            return False, f"account_id={account_id}: {error_text}"


cpa_mint_runtime = CpaMintRuntime()

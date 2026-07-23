"""Transactional account allocation and one-time delivery records."""
from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.cpa_auth import probe_cpa_account

from . import _shared


MANIFEST_PATH = Path(
    os.getenv(
        "DOWNLOAD_GATE_MANIFEST_PATH",
        str(_shared.REPO_ROOT / "runtime" / "download-gate" / "manifest.json"),
    )
).expanduser()
HISTORY_IMPORT_KEY = "account_delivery_history_import_v1"
HISTORY_IMPORT_BUNDLE_PREFIX = "account_delivery_history_bundle_v2:"
ACTIVE_LEASE_STATES = ("probing", "ready", "packing")
DEFAULT_DELIVERY_PLATFORM = "grok"


def normalize_delivery_platform(value: str | None) -> str:
    platform = str(value or DEFAULT_DELIVERY_PLATFORM).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", platform):
        raise DeliveryConflict("invalid delivery platform")
    return platform


def normalize_required_model(platform: str, value: str | None) -> str:
    model = "" if platform == "grok" else str(value or "").strip()
    if len(model) > 100:
        raise DeliveryConflict("required_model is too long")
    return model


class DeliveryConflict(RuntimeError):
    pass


class DeliveryNotFound(RuntimeError):
    pass


class DeliveryUnavailable(RuntimeError):
    pass


class DeliveryUnauthorized(RuntimeError):
    pass


def _begin_immediate() -> sqlite3.Connection:
    conn = _shared.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _finish(conn: sqlite3.Connection, *, ok: bool) -> None:
    try:
        conn.commit() if ok else conn.rollback()
    finally:
        conn.close()


def _abort_if_open(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    except sqlite3.ProgrammingError:
        pass


def _account_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return _shared._account_row_to_dict(row)


def _document_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return _shared.account_delivery_document(_account_from_row(row))


def check_internal_bearer(authorization: str) -> None:
    expected = _shared.DOWNLOAD_GATE_INTERNAL_TOKEN
    if not expected:
        raise DeliveryUnavailable("DownloadGate internal API token is not configured")
    if not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
        raise DeliveryUnauthorized("Unauthorized")


def _zip_bundle_candidates(bundle_id: str) -> list[dict[str, Any]]:
    if not bundle_id or not all(ch.isalnum() or ch in "-_" for ch in bundle_id):
        return []
    zip_path = MANIFEST_PATH.parent / "zips" / f"{bundle_id}.zip"
    if not zip_path.exists():
        return []
    candidates: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for info in archive.infolist()[:500]:
                if info.is_dir() or not info.filename.lower().endswith(".json"):
                    continue
                if info.file_size > 2 * 1024 * 1024:
                    continue
                try:
                    document = json.loads(archive.read(info).decode("utf-8"))
                except (KeyError, UnicodeDecodeError, ValueError):
                    continue
                if isinstance(document, dict):
                    candidates.append(document)
    except (OSError, zipfile.BadZipFile):
        return []
    return candidates


def _manifest_account_ids(
    manifest: dict[str, Any], rows: list[sqlite3.Row]
) -> list[tuple[str, dict[str, Any], list[int]]]:
    by_email: dict[str, int] = {}
    by_external_id: dict[str, int] = {}
    valid_ids: set[int] = set()
    for row in rows:
        source_id = int(row["id"])
        valid_ids.add(source_id)
        email = str(row["email"] or "").strip().lower()
        if email:
            by_email[email] = source_id
        try:
            extra = json.loads(row["extra_json"] or "{}")
        except Exception:
            extra = {}
        external_id = str(extra.get("sub") or "").strip()
        if external_id:
            by_external_id[external_id] = source_id

    matched: list[tuple[str, dict[str, Any], list[int]]] = []
    bundles = manifest.get("bundles") if isinstance(manifest, dict) else {}
    if not isinstance(bundles, dict):
        return matched
    for manifest_bundle_id, bundle in bundles.items():
        if not isinstance(bundle, dict):
            continue
        bundle_id = str(bundle.get("id") or manifest_bundle_id or "").strip()
        if not bundle_id:
            continue
        account_ids: set[int] = set()
        candidates: list[Any] = []
        identities = bundle.get("identities")
        if isinstance(identities, list):
            candidates.extend(identities)
        files = bundle.get("files")
        if isinstance(files, list):
            candidates.extend(files)
        candidates.extend(_zip_bundle_candidates(bundle_id))
        for candidate in candidates:
            if isinstance(candidate, dict):
                email = str(candidate.get("email") or "").strip().lower()
                cpa_auth = candidate.get("cpa_auth") if isinstance(candidate.get("cpa_auth"), dict) else {}
                extra = candidate.get("extra") if isinstance(candidate.get("extra"), dict) else {}
                account_state = (
                    candidate.get("account_state")
                    if isinstance(candidate.get("account_state"), dict)
                    else {}
                )
                external_id = str(
                    candidate.get("account_id")
                    or candidate.get("sub")
                    or cpa_auth.get("sub")
                    or extra.get("sub")
                    or ""
                ).strip()
                source_id = candidate.get("source_id") or account_state.get("source_id")
                if email in by_email:
                    account_ids.add(by_email[email])
                if external_id in by_external_id:
                    account_ids.add(by_external_id[external_id])
                try:
                    numeric_id = int(source_id or external_id)
                except (TypeError, ValueError):
                    numeric_id = 0
                if numeric_id in valid_ids:
                    account_ids.add(numeric_id)
                continue
            filename = str(candidate or "")
            match = re.search(r"grok-account-(\d+)-", filename)
            if match and int(match.group(1)) in valid_ids:
                account_ids.add(int(match.group(1)))
        if account_ids:
            matched.append((bundle_id, bundle, sorted(account_ids)))
    return matched


def import_download_gate_history() -> dict[str, int]:
    """Incrementally import every previously unseen DownloadGate bundle."""
    if not MANIFEST_PATH.exists():
        return {"bundles": 0, "accounts": 0}
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"bundles": 0, "accounts": 0}

    with _shared.db_lock:
        conn = _begin_immediate()
        try:
            rows = conn.execute("SELECT * FROM accounts").fetchall()
            matches = _manifest_account_ids(manifest, rows)
            rows_by_id = {int(row["id"]): row for row in rows}
            imported_bundles = 0
            imported_accounts = 0
            for bundle_id, bundle, account_ids in matches:
                marker_key = f"{HISTORY_IMPORT_BUNDLE_PREFIX}{bundle_id}"
                if conn.execute(
                    "SELECT 1 FROM settings WHERE key=?", (marker_key,)
                ).fetchone():
                    continue
                card_key = str(bundle.get("key") or f"history:{bundle_id}")
                timestamp = str(bundle.get("created_at") or _shared.now_iso())
                bundle_platform = str(bundle.get("platform") or "").strip().lower()
                if not bundle_platform:
                    bundle_platforms = {
                        str(rows_by_id[account_id]["platform"] or DEFAULT_DELIVERY_PLATFORM)
                        .strip()
                        .lower()
                        for account_id in account_ids
                        if account_id in rows_by_id
                    }
                    bundle_platform = (
                        next(iter(bundle_platforms))
                        if len(bundle_platforms) == 1
                        else DEFAULT_DELIVERY_PLATFORM
                    )
                bundle_platform = normalize_delivery_platform(bundle_platform)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO delivery_orders
                        (card_key, platform, required_model, state, source, title, bundle_id,
                         created_at, updated_at, committed_at)
                    VALUES (?, ?, '', 'consumed', 'history', ?, ?, ?, ?, ?)
                    """,
                    (
                        card_key,
                        bundle_platform,
                        str(bundle.get("title") or ""),
                        bundle_id,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                order = conn.execute(
                    "SELECT id FROM delivery_orders WHERE card_key = ?", (card_key,)
                ).fetchone()
                if not order:
                    continue
                bundle_accounts = 0
                for account_id in account_ids:
                    row = rows_by_id.get(account_id)
                    if not row:
                        continue
                    document = _document_from_row(row)
                    lease = conn.execute(
                        "SELECT id FROM account_delivery_leases WHERE order_id=? AND account_id=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (int(order["id"]), account_id),
                    ).fetchone()
                    lease_id = str(lease["id"]) if lease else None
                    inserted = conn.execute(
                        """
                        INSERT OR IGNORE INTO account_delivery_consumptions
                            (order_id, lease_id, account_id, card_key, bundle_id,
                             document_json, consumed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(order["id"]),
                            lease_id,
                            account_id,
                            card_key,
                            bundle_id,
                            json.dumps(document, ensure_ascii=False),
                            timestamp,
                        ),
                    )
                    if inserted.rowcount == 1:
                        bundle_accounts += 1
                placeholders = ",".join("?" for _ in account_ids)
                conn.execute(
                    f"UPDATE account_delivery_leases SET state='consumed', updated_at=? "
                    f"WHERE order_id=? AND account_id IN ({placeholders})",
                    (timestamp, int(order["id"]), *account_ids),
                )
                conn.execute(
                    "UPDATE delivery_orders SET state='consumed', bundle_id=?, updated_at=?, "
                    "committed_at=COALESCE(committed_at, ?) WHERE id=?",
                    (bundle_id, timestamp, timestamp, int(order["id"])),
                )
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        marker_key,
                        json.dumps({"bundle_id": bundle_id, "accounts": account_ids}),
                        _shared.now_iso(),
                    ),
                )
                if bundle_accounts:
                    imported_bundles += 1
                    imported_accounts += bundle_accounts
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (
                    HISTORY_IMPORT_KEY,
                    json.dumps({"bundles": imported_bundles, "accounts": imported_accounts}),
                    _shared.now_iso(),
                ),
            )
            _finish(conn, ok=True)
            return {"bundles": imported_bundles, "accounts": imported_accounts}
        except Exception:
            _abort_if_open(conn)
            raise


def _probe_account(account_id: int, required_model: str) -> dict[str, Any]:
    cached_accounts = _shared.account_list_by_ids([account_id])
    if not cached_accounts:
        return {"ok": False, "error": "account not found"}
    cached_account = cached_accounts[0]
    account_platform = normalize_delivery_platform(
        str(cached_account.get("platform") or DEFAULT_DELIVERY_PLATFORM)
    )

    # CPA / 模型探测只属于 Grok。其他平台按各自账号资产做基础交付校验，
    # 避免把 ChatGPT、Kiro 等凭证错误发送到 Grok 的模型接口。
    if account_platform != "grok":
        tokens = (
            cached_account.get("tokens")
            if isinstance(cached_account.get("tokens"), dict)
            else {}
        )
        try:
            extra = json.loads(cached_account.get("extra_json") or "{}")
        except Exception:
            extra = {}
        credential_present = any(
            str(value or "").strip()
            for value in (
                cached_account.get("sso"),
                cached_account.get("password"),
                tokens.get("access_token"),
                tokens.get("refresh_token"),
                tokens.get("session_token"),
                extra.get("access_token"),
                extra.get("refresh_token"),
                extra.get("session_token"),
            )
        )
        lifecycle = str(cached_account.get("lifecycle_status") or "registered")
        validity = str(cached_account.get("validity_status") or "unknown")
        ok = (
            str(cached_account.get("status") or "") == "active"
            and lifecycle not in {"expired", "invalid", "suspended"}
            and validity != "invalid"
            and credential_present
        )
        return {
            "ok": ok,
            "platform": account_platform,
            "probe_kind": "platform_inventory",
            "credential_present": credential_present,
            "required_model": required_model,
            "error": "" if ok else "platform account credential is unavailable",
        }

    try:
        from ._cpa_runtime import cpa_mint_runtime

        cpa_config = cpa_mint_runtime.config()
    except Exception:
        cpa_mint_runtime = None
        cpa_config = {}
    cpa_extra = cpa_config.get("extra") if isinstance(cpa_config.get("extra"), dict) else {}
    try:
        cache_ttl_minutes = min(max(5, int(cpa_extra.get("prevalidate_ttl_minutes", 60))), 1440)
    except (TypeError, ValueError):
        cache_ttl_minutes = 60

    if cached_accounts:
        try:
            cached_extra = json.loads(cached_account.get("extra_json") or "{}")
        except Exception:
            cached_extra = {}
        cached_cpa = cached_extra.get("cpa") if isinstance(cached_extra.get("cpa"), dict) else {}
        cached_probe = cached_cpa.get("probe") if isinstance(cached_cpa.get("probe"), dict) else {}
        checked_text = str(cached_cpa.get("probe_checked_at") or cached_cpa.get("updated_at") or "")
        try:
            checked_at = datetime.fromisoformat(checked_text.replace("Z", "+00:00")).replace(tzinfo=None)
        except (TypeError, ValueError):
            checked_at = None
        cached_alive = bool(cached_probe.get("account_alive", cached_probe.get("ok")))
        if (
            checked_at is not None
            and checked_at >= datetime.now() - timedelta(minutes=cache_ttl_minutes)
            and cached_probe.get("probe_kind") in {"account_identity", "account_response"}
            and cached_alive
        ):
            result = dict(cached_probe)
            result["required_model"] = ""
            result["account_alive"] = True
            result["delivery_eligible"] = True
            result["cache_hit"] = True
            return result
    try:
        probe_timeout = min(max(10, int(cpa_extra.get("timeout", 30))), 60)
    except (TypeError, ValueError):
        probe_timeout = 30

    def run_probe() -> dict[str, Any]:
        accounts = _shared.account_list_by_ids([account_id])
        if not accounts:
            return {"ok": False, "error": "account not found"}
        account = accounts[0]
        tokens = account.get("tokens") or {}
        try:
            extra = json.loads(account.get("extra_json") or "{}")
        except Exception:
            extra = {}
        access_token = str(tokens.get("access_token") or "")
        if not access_token:
            return {
                "ok": False,
                "account_alive": False,
                "delivery_eligible": False,
                "error": "access_token is empty",
                "failure_kind": "token_expired",
                "refresh_recommended": True,
                "banned": False,
                "probe_kind": "account_identity",
            }
        result = probe_cpa_account(
            access_token,
            proxy=str(cpa_extra.get("proxy") or account.get("proxy_url") or ""),
            timeout=probe_timeout,
            verify_tls=bool(cpa_extra.get("verify_tls", True)),
        )
        result["required_model"] = ""
        result["delivery_eligible"] = bool(result.get("account_alive", result.get("ok")))
        return result

    first = run_probe()
    if first.get("account_alive", first.get("ok")):
        return first
    if not first.get("refresh_recommended"):
        return first
    if cpa_mint_runtime is None:
        minted, mint_error = False, "CPA mint runtime is unavailable"
    else:
        try:
            minted, mint_error = cpa_mint_runtime._mint_account(account_id, force=True)
        except Exception as exc:
            minted, mint_error = False, str(exc)
    second = run_probe() if minted else dict(first)
    second["mint_attempted"] = True
    second["mint_ok"] = bool(minted)
    if mint_error:
        second["mint_error"] = str(mint_error)[:500]
    return second


def _delivery_candidate_rows(platform: str) -> list[sqlite3.Row]:
    platform = normalize_delivery_platform(platform)
    import_download_gate_history()
    return _shared.fetch_all(
        """
        SELECT a.id, a.extra_json
        FROM accounts a
        WHERE a.platform=?
          AND a.status='active'
          AND (
              (?='grok' AND a.sso <> '')
              OR (?<>'grok' AND (a.sso <> '' OR a.password <> ''))
          )
          AND a.lifecycle_status NOT IN ('expired', 'invalid')
          AND a.validity_status <> 'invalid'
          AND NOT EXISTS (
              SELECT 1 FROM account_delivery_consumptions c
              WHERE c.account_id=a.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM account_delivery_leases l
              WHERE l.account_id=a.id AND l.state IN ('probing','ready','packing')
          )
        """,
        (platform, platform, platform),
    )


def _delivery_prevalidation_ttl_minutes() -> int:
    try:
        from ._cpa_runtime import cpa_mint_runtime

        return cpa_mint_runtime.prevalidation_ttl_minutes()
    except Exception:
        return 60


def delivery_stock_snapshot(
    platform: str = DEFAULT_DELIVERY_PLATFORM,
    required_model: str = "",
) -> dict[str, Any]:
    """Return candidate and recently verified delivery inventory separately.

    Candidate inventory only applies the persisted account/lifecycle/lease filters.
    Grok verified inventory requires a recent probe proving the account credential is
    alive. No model endpoint is called and model availability is not evaluated.
    """
    platform = normalize_delivery_platform(platform)
    required_model = normalize_required_model(platform, required_model)
    rows = _delivery_candidate_rows(platform)
    candidate_stock = len(rows)
    ttl_minutes = _delivery_prevalidation_ttl_minutes() if platform == "grok" else 0
    cutoff = datetime.now() - timedelta(minutes=ttl_minutes) if ttl_minutes else None

    if platform != "grok":
        verified_stock = candidate_stock
    else:
        verified_stock = 0
        for row in rows:
            try:
                extra = json.loads(row["extra_json"] or "{}")
            except (TypeError, ValueError):
                continue
            cpa = extra.get("cpa") if isinstance(extra.get("cpa"), dict) else {}
            probe = cpa.get("probe") if isinstance(cpa.get("probe"), dict) else {}
            checked_text = str(cpa.get("probe_checked_at") or cpa.get("updated_at") or "")
            try:
                checked_at = datetime.fromisoformat(checked_text.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
            except (TypeError, ValueError):
                continue
            recent_account_probe = (
                cutoff is not None
                and checked_at >= cutoff
                and probe.get("probe_kind") in {"account_identity", "account_response"}
            )
            if recent_account_probe and bool(probe.get("account_alive", probe.get("ok"))):
                verified_stock += 1

    return {
        "platform": platform,
        "required_model": required_model,
        "candidate_stock": candidate_stock,
        "verified_stock": verified_stock,
        "unverified_stock": max(0, candidate_stock - verified_stock),
        "prevalidate_ttl_minutes": ttl_minutes,
        "replenishment_metric": "candidate_stock",
    }


def available_delivery_stock_count(platform: str = DEFAULT_DELIVERY_PLATFORM) -> int:
    """Legacy candidate-stock count used by registration replenishment."""
    return int(delivery_stock_snapshot(platform)["candidate_stock"])


def reserve(
    card_key: str,
    required_model: str = "",
    platform: str = DEFAULT_DELIVERY_PLATFORM,
) -> dict[str, Any]:
    card_key = card_key.strip()
    platform = normalize_delivery_platform(platform)
    required_model = normalize_required_model(platform, required_model)
    if not card_key:
        raise DeliveryConflict("card_key is required")
    import_download_gate_history()
    try:
        from ._cpa_runtime import cpa_mint_runtime

        prevalidate_ttl_minutes = cpa_mint_runtime.prevalidation_ttl_minutes()
    except Exception:
        prevalidate_ttl_minutes = 60
    prevalidate_cutoff = (datetime.now() - timedelta(minutes=prevalidate_ttl_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    while True:
        with _shared.db_lock:
            conn = _begin_immediate()
            try:
                now = _shared.now_iso()
                conn.execute(
                    "UPDATE account_delivery_leases SET state='failed', last_error='probe lease expired', updated_at=? "
                    "WHERE state='probing' AND expires_at IS NOT NULL AND expires_at < ?",
                    (now, now),
                )
                order = conn.execute(
                    "SELECT * FROM delivery_orders WHERE card_key = ?", (card_key,)
                ).fetchone()
                if order and normalize_delivery_platform(order["platform"]) != platform:
                    _finish(conn, ok=False)
                    raise DeliveryConflict("card key belongs to another platform")
                if order and order["state"] == "consumed":
                    consumed = conn.execute(
                        "SELECT * FROM account_delivery_consumptions WHERE order_id=? ORDER BY id LIMIT 1",
                        (int(order["id"]),),
                    ).fetchone()
                    if not consumed:
                        _finish(conn, ok=False)
                        raise DeliveryConflict("consumed delivery order has no document")
                    result = _consumption_result(consumed)
                    if consumed["lease_id"]:
                        consumed_lease = conn.execute(
                            "SELECT lease_token FROM account_delivery_leases WHERE id=?",
                            (str(consumed["lease_id"]),),
                        ).fetchone()
                        if consumed_lease:
                            result["lease_token"] = str(consumed_lease["lease_token"])
                    result["platform"] = platform
                    _finish(conn, ok=True)
                    return result
                if order:
                    lease = conn.execute(
                        "SELECT * FROM account_delivery_leases WHERE order_id=? AND state IN ('probing','ready') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (int(order["id"]),),
                    ).fetchone()
                    if lease and lease["state"] == "ready":
                        result = {
                            "order_id": int(order["id"]),
                            "lease_id": str(lease["id"]),
                            "lease_token": str(lease["lease_token"]),
                            "state": "ready",
                            "platform": platform,
                        }
                        _finish(conn, ok=True)
                        return result
                    if lease:
                        _finish(conn, ok=False)
                        raise DeliveryConflict("delivery reservation is already in progress")
                    order_id = int(order["id"])
                    conn.execute(
                        "UPDATE delivery_orders SET state='pending', required_model=?, updated_at=? WHERE id=?",
                        (required_model, now, order_id),
                    )
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO delivery_orders
                            (card_key, platform, required_model, state, source, created_at, updated_at)
                        VALUES (?, ?, ?, 'pending', 'dynamic', ?, ?)
                        """,
                        (card_key, platform, required_model, now, now),
                    )
                    order_id = int(cur.lastrowid)

                candidate = conn.execute(
                    """
                    SELECT a.* FROM accounts a
                    WHERE a.platform=?
                      AND a.status='active'
                      AND (
                          (?='grok' AND a.sso <> '')
                          OR (?<>'grok' AND (a.sso <> '' OR a.password <> ''))
                      )
                      AND a.lifecycle_status NOT IN ('expired', 'invalid')
                      AND a.validity_status <> 'invalid'
                      AND NOT EXISTS (
                          SELECT 1 FROM account_delivery_consumptions c
                          WHERE c.account_id=a.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM account_delivery_leases l
                          WHERE l.account_id=a.id AND l.state IN ('probing','ready','packing')
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM account_delivery_leases tried
                          WHERE tried.account_id=a.id AND tried.order_id=?
                            AND (tried.state <> 'failed' OR tried.updated_at > ?)
                      )
                    ORDER BY CASE WHEN ?='grok' AND
                                 COALESCE(
                                     json_extract(a.extra_json, '$.cpa.probe.account_alive'),
                                     json_extract(a.extra_json, '$.cpa.probe.ok'),
                                     0
                                 ) = 1
                                 AND COALESCE(
                                     json_extract(a.extra_json, '$.cpa.probe.probe_kind'), ''
                                 ) IN ('account_identity', 'account_response')
                                 AND COALESCE(
                                     json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                                     json_extract(a.extra_json, '$.cpa.updated_at'),
                                     ''
                                 ) >= ?
                             THEN 0 ELSE 1 END,
                             CASE a.validity_status WHEN 'valid' THEN 0 ELSE 1 END,
                             COALESCE(
                                 json_extract(a.extra_json, '$.cpa.probe_checked_at'),
                                 json_extract(a.extra_json, '$.cpa.updated_at'),
                                 ''
                             ) DESC,
                             a.id ASC
                    LIMIT 1
                    """,
                    (
                        platform,
                        platform,
                        platform,
                        order_id,
                        (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
                        platform,
                        prevalidate_cutoff,
                    ),
                ).fetchone()
                if not candidate:
                    conn.execute(
                        "UPDATE delivery_orders SET state='pending', last_error=?, updated_at=? WHERE id=?",
                        ("no active account is available", now, order_id),
                    )
                    _finish(conn, ok=True)
                    raise DeliveryUnavailable("no active account is available")
                lease_id = uuid.uuid4().hex
                lease_token = uuid.uuid4().hex + uuid.uuid4().hex
                expires_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    INSERT INTO account_delivery_leases
                        (id, order_id, account_id, lease_token, state, created_at,
                         updated_at, expires_at)
                    VALUES (?, ?, ?, ?, 'probing', ?, ?, ?)
                    """,
                    (lease_id, order_id, int(candidate["id"]), lease_token, now, now, expires_at),
                )
                account_id = int(candidate["id"])
                _finish(conn, ok=True)
            except Exception:
                _abort_if_open(conn)
                raise

        try:
            probe = _probe_account(account_id, required_model)
        except Exception as exc:
            probe = {
                "ok": False,
                "required_model": required_model,
                "error": str(exc)[:500],
            }
        with _shared.db_lock:
            conn = _begin_immediate()
            try:
                now = _shared.now_iso()
                if probe.get("account_alive", probe.get("ok")):
                    updated = conn.execute(
                        "UPDATE account_delivery_leases SET state='ready', probe_json=?, last_error='', "
                        "updated_at=?, expires_at=NULL WHERE id=? AND state='probing'",
                        (json.dumps(probe, ensure_ascii=False), now, lease_id),
                    )
                    if updated.rowcount != 1:
                        _finish(conn, ok=True)
                        continue
                    conn.execute(
                        "UPDATE delivery_orders SET state='ready', last_error='', updated_at=? WHERE id=?",
                        (now, order_id),
                    )
                    conn.execute(
                        "UPDATE accounts SET validity_status='valid', last_checked_at=?, last_error='' WHERE id=?",
                        (now, account_id),
                    )
                    _finish(conn, ok=True)
                    return {
                        "order_id": order_id,
                        "lease_id": lease_id,
                        "lease_token": lease_token,
                        "state": "ready",
                        "platform": platform,
                    }
                # access_token 为空时会先尝试从 SSO 自动补全 OAuth。补全失败的
                # mint_error 才是真正根因（例如 CPA 目的地未配置或 SSO 失效），
                # 不应再被笼统的 "access_token is empty" 掩盖。
                error = str(probe.get("mint_error") or probe.get("error") or "required model unavailable")[:500]
                updated = conn.execute(
                    "UPDATE account_delivery_leases SET state='failed', probe_json=?, last_error=?, updated_at=? "
                    "WHERE id=? AND state='probing'",
                    (json.dumps(probe, ensure_ascii=False), error, now, lease_id),
                )
                if updated.rowcount != 1:
                    _finish(conn, ok=True)
                    continue
                conn.execute(
                    "UPDATE delivery_orders SET state='pending', last_error=?, updated_at=? WHERE id=?",
                    (error, now, order_id),
                )
                banned = bool(probe.get("banned"))
                conn.execute(
                    "UPDATE accounts SET "
                    "validity_status=CASE WHEN ? THEN 'invalid' ELSE validity_status END, "
                    "lifecycle_status=CASE WHEN ? THEN 'suspended' ELSE lifecycle_status END, "
                    "last_checked_at=?, last_error=? WHERE id=?",
                    (banned, banned, now, error, account_id),
                )
                _finish(conn, ok=True)
            except Exception:
                _finish(conn, ok=False)
                raise


def _consumption_result(row: sqlite3.Row) -> dict[str, Any]:
    document = json.loads(row["document_json"])
    return {
        "order_id": int(row["order_id"]),
        "lease_id": str(row["lease_id"] or ""),
        "account_id": int(row["account_id"]),
        "card_key": str(row["card_key"]),
        "bundle_id": str(row["bundle_id"] or ""),
        "state": "consumed",
        "platform": normalize_delivery_platform(document.get("platform")),
        "document": document,
        "consumed_at": str(row["consumed_at"]),
    }


def commit(card_key: str, lease_id: str, lease_token: str, bundle_id: str = "") -> dict[str, Any]:
    card_key = card_key.strip()
    with _shared.db_lock:
        conn = _begin_immediate()
        try:
            order = conn.execute(
                "SELECT * FROM delivery_orders WHERE card_key=?", (card_key,)
            ).fetchone()
            if not order:
                _finish(conn, ok=False)
                raise DeliveryNotFound("delivery order not found")
            lease = conn.execute(
                "SELECT * FROM account_delivery_leases WHERE id=? AND order_id=?",
                (lease_id, int(order["id"])),
            ).fetchone()
            if not lease or not hmac.compare_digest(str(lease["lease_token"]), lease_token or ""):
                _finish(conn, ok=False)
                raise DeliveryUnauthorized("invalid delivery lease")
            existing = conn.execute(
                "SELECT * FROM account_delivery_consumptions WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            if existing:
                if bundle_id and not existing["bundle_id"]:
                    conn.execute(
                        "UPDATE account_delivery_consumptions SET bundle_id=? WHERE id=?",
                        (bundle_id, int(existing["id"])),
                    )
                    existing = conn.execute(
                        "SELECT * FROM account_delivery_consumptions WHERE id=?",
                        (int(existing["id"]),),
                    ).fetchone()
                result = _consumption_result(existing)
                _finish(conn, ok=True)
                return result
            if lease["state"] != "ready":
                _finish(conn, ok=False)
                raise DeliveryConflict(f"delivery lease is {lease['state']}")
            account = conn.execute(
                "SELECT * FROM accounts WHERE id=?", (int(lease["account_id"]),)
            ).fetchone()
            if not account:
                _finish(conn, ok=False)
                raise DeliveryNotFound("account not found")
            document = _document_from_row(account)
            now = _shared.now_iso()
            try:
                cur = conn.execute(
                    """
                    INSERT INTO account_delivery_consumptions
                        (order_id, lease_id, account_id, card_key, bundle_id,
                         document_json, consumed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(order["id"]),
                        lease_id,
                        int(account["id"]),
                        card_key,
                        bundle_id,
                        json.dumps(document, ensure_ascii=False),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                _finish(conn, ok=False)
                raise DeliveryConflict("account has already been delivered") from exc
            conn.execute(
                "UPDATE account_delivery_leases SET state='consumed', updated_at=? WHERE id=?",
                (now, lease_id),
            )
            conn.execute(
                "UPDATE delivery_orders SET state='consumed', bundle_id=?, updated_at=?, committed_at=? WHERE id=?",
                (bundle_id, now, now, int(order["id"])),
            )
            saved = conn.execute(
                "SELECT * FROM account_delivery_consumptions WHERE id=?", (int(cur.lastrowid),)
            ).fetchone()
            assert saved is not None
            result = _consumption_result(saved)
            _finish(conn, ok=True)
            return result
        except Exception:
            _abort_if_open(conn)
            raise


def by_card(card_key: str) -> dict[str, Any]:
    with _shared.db_lock, closing(_shared.get_conn()) as conn:
        order = conn.execute(
            "SELECT * FROM delivery_orders WHERE card_key=?", (card_key.strip(),)
        ).fetchone()
        if not order:
            raise DeliveryNotFound("delivery order not found")
        consumptions = conn.execute(
            "SELECT * FROM account_delivery_consumptions WHERE order_id=? ORDER BY id",
            (int(order["id"]),),
        ).fetchall()
        if consumptions:
            results = [_consumption_result(row) for row in consumptions]
            response = dict(results[0])
            if len(results) > 1:
                response["documents"] = [item["document"] for item in results]
                response["account_ids"] = [item["account_id"] for item in results]
            return response
        lease = conn.execute(
            "SELECT * FROM account_delivery_leases WHERE order_id=? ORDER BY created_at DESC LIMIT 1",
            (int(order["id"]),),
        ).fetchone()
        response: dict[str, Any] = {
            "order_id": int(order["id"]),
            "card_key": str(order["card_key"]),
            "platform": normalize_delivery_platform(order["platform"]),
            "state": str(order["state"]),
            "bundle_id": str(order["bundle_id"] or ""),
            "last_error": str(order["last_error"] or ""),
        }
        if lease and lease["state"] in {"probing", "ready"}:
            response.update(
                lease_id=str(lease["id"]),
                lease_token=str(lease["lease_token"]),
                state=str(lease["state"]),
            )
        return response


def _manual_order_details(conn: sqlite3.Connection, order: sqlite3.Row) -> dict[str, Any]:
    account_ids = [
        int(row["account_id"])
        for row in conn.execute(
            "SELECT account_id FROM account_delivery_leases WHERE order_id=? ORDER BY account_id",
            (int(order["id"]),),
        ).fetchall()
    ]
    return {
        "order_id": int(order["id"]),
        "card_key": str(order["card_key"]),
        "bundle_id": str(order["bundle_id"] or ""),
        "state": str(order["state"]),
        "platform": normalize_delivery_platform(order["platform"]),
        "title": str(order["title"] or ""),
        "account_ids": account_ids,
    }


def prepare_selected_request(account_ids: list[int], title: str = "") -> dict[str, Any]:
    import_download_gate_history()
    unique_ids = list(dict.fromkeys(int(value) for value in account_ids))
    if not unique_ids:
        raise DeliveryConflict("at least one account is required")
    with _shared.db_lock:
        conn = _begin_immediate()
        try:
            placeholders = ",".join("?" for _ in unique_ids)
            rows = conn.execute(
                f"SELECT id, platform FROM accounts WHERE id IN ({placeholders})",
                tuple(unique_ids),
            ).fetchall()
            if len(rows) != len(unique_ids):
                raise DeliveryNotFound("one or more accounts do not exist")
            platforms = {
                normalize_delivery_platform(row["platform"])
                for row in rows
            }
            if len(platforms) != 1:
                raise DeliveryConflict("manual delivery cannot mix account platforms")
            platform = next(iter(platforms))
            requested_set = set(unique_ids)
            manual_orders = conn.execute(
                "SELECT * FROM delivery_orders WHERE source='manual' AND state IN ('packing','consumed') "
                "ORDER BY id DESC"
            ).fetchall()
            for existing_order in manual_orders:
                details = _manual_order_details(conn, existing_order)
                if set(details["account_ids"]) == requested_set:
                    details["reused"] = True
                    _finish(conn, ok=True)
                    return details
            blocked = conn.execute(
                f"""
                SELECT a.id FROM accounts a
                WHERE a.id IN ({placeholders}) AND (
                    EXISTS (SELECT 1 FROM account_delivery_consumptions c WHERE c.account_id=a.id)
                    OR EXISTS (
                        SELECT 1 FROM account_delivery_leases l
                        WHERE l.account_id=a.id AND l.state IN ('probing','ready','packing')
                    )
                )
                """,
                tuple(unique_ids),
            ).fetchall()
            if blocked:
                blocked_ids = ", ".join(str(int(row["id"])) for row in blocked)
                raise DeliveryConflict(f"accounts already delivered or reserved: {blocked_ids}")
            now = _shared.now_iso()
            card_key = f"DG-MANUAL-{uuid.uuid4().hex[:20].upper()}"
            cur = conn.execute(
                """
                INSERT INTO delivery_orders
                    (card_key, platform, required_model, state, source, title, created_at, updated_at)
                VALUES (?, ?, '', 'packing', 'manual', ?, ?, ?)
                """,
                (card_key, platform, title, now, now),
            )
            order_id = int(cur.lastrowid)
            for account_id in unique_ids:
                conn.execute(
                    """
                    INSERT INTO account_delivery_leases
                        (id, order_id, account_id, lease_token, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'packing', ?, ?)
                    """,
                    (uuid.uuid4().hex, order_id, account_id, uuid.uuid4().hex, now, now),
                )
            order = conn.execute("SELECT * FROM delivery_orders WHERE id=?", (order_id,)).fetchone()
            assert order is not None
            details = _manual_order_details(conn, order)
            details["reused"] = False
            _finish(conn, ok=True)
            return details
        except Exception:
            _abort_if_open(conn)
            raise


def prepare_selected(account_ids: list[int], title: str = "") -> int:
    return int(prepare_selected_request(account_ids, title)["order_id"])


def _manifest_bundle_by_card(card_key: str) -> dict[str, Any] | None:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    bundles = manifest.get("bundles") if isinstance(manifest, dict) else {}
    if not isinstance(bundles, dict):
        return None
    for manifest_bundle_id, bundle in bundles.items():
        if not isinstance(bundle, dict) or str(bundle.get("key") or "").strip() != card_key:
            continue
        bundle_id = str(bundle.get("id") or manifest_bundle_id or "").strip()
        if not bundle_id:
            continue
        return {
            "ok": True,
            "bundle_id": bundle_id,
            "key": card_key,
            "file_count": int(bundle.get("file_count") or len(bundle.get("files") or [])),
            "title": str(bundle.get("title") or ""),
        }
    return None


def recover_selected(
    order_id: int, documents: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    with _shared.db_lock, closing(_shared.get_conn()) as conn:
        order = conn.execute("SELECT * FROM delivery_orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise DeliveryNotFound("manual delivery order not found")
        details = _manual_order_details(conn, order)
    if details["state"] == "consumed":
        return {
            "ok": True,
            "bundle_id": details["bundle_id"],
            "key": details["card_key"],
            "file_count": len(details["account_ids"]),
            "title": details["title"],
        }
    recovered = _manifest_bundle_by_card(details["card_key"])
    if not recovered:
        return None
    commit_selected(
        order_id,
        card_key=details["card_key"],
        bundle_id=str(recovered["bundle_id"]),
        documents=documents,
    )
    return recovered


def abort_selected(order_id: int, error: str) -> None:
    """Keep an uncertain manual delivery reserved until it can be recovered."""
    with _shared.db_lock:
        conn = _begin_immediate()
        try:
            now = _shared.now_iso()
            conn.execute(
                "UPDATE account_delivery_leases SET last_error=?, updated_at=? "
                "WHERE order_id=? AND state='packing'",
                (error[:500], now, order_id),
            )
            conn.execute(
                "UPDATE delivery_orders SET last_error=?, updated_at=? WHERE id=? AND state='packing'",
                (error[:500], now, order_id),
            )
            _finish(conn, ok=True)
        except Exception:
            _abort_if_open(conn)
            raise


def commit_selected(
    order_id: int,
    *,
    card_key: str,
    bundle_id: str,
    documents: dict[int, dict[str, Any]],
) -> None:
    with _shared.db_lock:
        conn = _begin_immediate()
        try:
            order = conn.execute("SELECT * FROM delivery_orders WHERE id=?", (order_id,)).fetchone()
            if order and order["state"] == "consumed":
                if str(order["card_key"]) != card_key or (
                    bundle_id and str(order["bundle_id"] or "") != bundle_id
                ):
                    raise DeliveryConflict("manual delivery order was committed differently")
                _finish(conn, ok=True)
                return
            if not order or order["state"] != "packing":
                raise DeliveryConflict("manual delivery order is not packable")
            duplicate = conn.execute(
                "SELECT id FROM delivery_orders WHERE card_key=? AND id<>?", (card_key, order_id)
            ).fetchone()
            if duplicate:
                raise DeliveryConflict("delivery card key already exists")
            leases = conn.execute(
                "SELECT * FROM account_delivery_leases WHERE order_id=? AND state='packing'",
                (order_id,),
            ).fetchall()
            now = _shared.now_iso()
            for lease in leases:
                account_id = int(lease["account_id"])
                document = documents[account_id]
                conn.execute(
                    """
                    INSERT INTO account_delivery_consumptions
                        (order_id, lease_id, account_id, card_key, bundle_id,
                         document_json, consumed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        str(lease["id"]),
                        account_id,
                        card_key,
                        bundle_id,
                        json.dumps(document, ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "UPDATE account_delivery_leases SET state='consumed', updated_at=? WHERE order_id=?",
                (now, order_id),
            )
            conn.execute(
                "UPDATE delivery_orders SET card_key=?, bundle_id=?, state='consumed', "
                "updated_at=?, committed_at=? WHERE id=?",
                (card_key, bundle_id, now, now, order_id),
            )
            _finish(conn, ok=True)
        except Exception:
            _abort_if_open(conn)
            raise

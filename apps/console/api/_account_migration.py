"""Credential-preserving account migration with explicit backup and deduplication."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from . import _shared


MIGRATION_SCHEMA = "grok-register.account-migration.v1"
MAX_MIGRATION_ACCOUNTS = 5000
MAX_JSON_FIELD_BYTES = 2 * 1024 * 1024
ACCOUNT_FIELDS = (
    "email",
    "sso",
    "password",
    "proxy_url",
    "status",
    "lifecycle_status",
    "plan_state",
    "validity_status",
    "last_error",
    "last_checked_at",
    "notes",
    "created_at",
    "platform",
    "extra_json",
    "exporter_status_json",
)
UPDATE_FIELDS = tuple(field for field in ACCOUNT_FIELDS if field != "created_at")


def _text(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _json_object(value: Any, *, field: str, index: int) -> tuple[str, dict[str, Any]]:
    raw = value if isinstance(value, str) else json.dumps(value or {}, ensure_ascii=False)
    raw = raw or "{}"
    if len(raw.encode("utf-8")) > MAX_JSON_FIELD_BYTES:
        raise HTTPException(status_code=413, detail=f"accounts[{index}].{field} is too large")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"accounts[{index}].{field} is invalid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail=f"accounts[{index}].{field} must be an object",
        )
    return json.dumps(parsed, ensure_ascii=False), parsed


def _identity_key(item: dict[str, Any], extra: dict[str, Any]) -> tuple[str, str]:
    platform = _text(item.get("platform"), "grok").strip().lower() or "grok"
    subject = _text(extra.get("sub")).strip()
    if subject:
        return platform, f"sub:{subject}"
    sso = _text(item.get("sso")).strip()
    if sso:
        return platform, f"sso:{sso}"
    email = _text(item.get("email")).strip().lower()
    if email:
        return platform, f"email:{email}"
    return platform, f"source:{_text(item.get('source_id') or item.get('id'))}"


def _normalized_account(item: dict[str, Any], index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    email = _text(item.get("email")).strip()[:320]
    sso = _text(item.get("sso")).strip()
    password = _text(item.get("password"))
    platform = _text(item.get("platform"), "grok").strip().lower() or "grok"
    if not (email or sso):
        raise HTTPException(
            status_code=422,
            detail=f"accounts[{index}] has no stable email or SSO identity",
        )
    if len(sso.encode("utf-8")) > MAX_JSON_FIELD_BYTES:
        raise HTTPException(status_code=413, detail=f"accounts[{index}].sso is too large")
    extra_json, extra = _json_object(
        item.get("extra_json", {}), field="extra_json", index=index
    )
    exporter_status_json, _ = _json_object(
        item.get("exporter_status_json", {}),
        field="exporter_status_json",
        index=index,
    )
    normalized = {
        "email": email,
        "sso": sso,
        "password": password,
        "proxy_url": _text(item.get("proxy_url")),
        "status": _text(item.get("status"), "active") or "active",
        "lifecycle_status": (
            _text(item.get("lifecycle_status"), "registered") or "registered"
        ),
        "plan_state": _text(item.get("plan_state"), "unknown") or "unknown",
        "validity_status": (
            _text(item.get("validity_status"), "unknown") or "unknown"
        ),
        "last_error": _text(item.get("last_error"))[:1000],
        "last_checked_at": _text(item.get("last_checked_at")) or None,
        "notes": _text(item.get("notes"))[:5000],
        "created_at": _text(item.get("created_at")) or _shared.now_iso(),
        "platform": platform,
        "extra_json": extra_json,
        "exporter_status_json": exporter_status_json,
        "source_id": item.get("source_id", item.get("id")),
    }
    return normalized, extra


def _deduplicate(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    selected: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"accounts[{index}] must be an object")
        normalized, extra = _normalized_account(item, index)
        key = _identity_key(normalized, extra)
        previous = selected.get(key)
        if previous is None:
            selected[key] = (normalized, extra)
            order.append(key)
        elif not previous[0].get("email") and normalized.get("email"):
            selected[key] = (normalized, extra)
    return [selected[key][0] for key in order], len(items) - len(selected)


def build_migration_document(
    account_ids: list[int],
    *,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered_ids = list(dict.fromkeys(int(account_id) for account_id in account_ids))
    if not ordered_ids:
        raise HTTPException(status_code=409, detail="no accounts match the export filters")
    if len(ordered_ids) > MAX_MIGRATION_ACCOUNTS:
        raise HTTPException(status_code=413, detail="too many accounts to export")
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = _shared.fetch_all(
        f"SELECT * FROM accounts WHERE id IN ({placeholders})",
        tuple(ordered_ids),
    )
    by_id = {int(row["id"]): dict(row) for row in rows}
    source: list[dict[str, Any]] = []
    for account_id in ordered_ids:
        row = by_id.get(account_id)
        if not row:
            continue
        item = {field: row.get(field) for field in ACCOUNT_FIELDS}
        item["source_id"] = account_id
        source.append(item)
    accounts, duplicates_removed = _deduplicate(source)
    return {
        "schema": MIGRATION_SCHEMA,
        "created_at": _shared.now_iso(),
        "selection": selection or {},
        "source_count": len(source),
        "count": len(accounts),
        "duplicates_removed": duplicates_removed,
        "accounts": accounts,
    }


def _find_existing(conn: sqlite3.Connection, item: dict[str, Any]) -> sqlite3.Row | None:
    try:
        extra = json.loads(item["extra_json"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    subject = _text(extra.get("sub")).strip()
    return conn.execute(
        """
        SELECT *
        FROM accounts
        WHERE platform=?
          AND (
                (? <> '' AND COALESCE(json_extract(
                    CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END,
                    '$.sub'
                ), '') = ?)
             OR (? <> '' AND sso = ?)
             OR (? <> '' AND LOWER(email) = LOWER(?))
          )
        ORDER BY
          CASE
            WHEN ? <> '' AND LOWER(email)=LOWER(?) AND sso=? THEN 0
            WHEN ? <> '' AND COALESCE(json_extract(
                CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END,
                '$.sub'
            ), '')=? THEN 1
            WHEN ? <> '' AND sso=? THEN 2
            ELSE 3
          END,
          id DESC
        LIMIT 1
        """,
        (
            item["platform"],
            subject,
            subject,
            item["sso"],
            item["sso"],
            item["email"],
            item["email"],
            item["email"],
            item["email"],
            item["sso"],
            subject,
            subject,
            item["sso"],
            item["sso"],
        ),
    ).fetchone()


def _same_account(existing: sqlite3.Row, item: dict[str, Any]) -> bool:
    for field in UPDATE_FIELDS:
        if field in {"extra_json", "exporter_status_json"}:
            try:
                current = json.loads(existing[field] or "{}")
                incoming = json.loads(item[field] or "{}")
            except (TypeError, ValueError):
                return False
            if current != incoming:
                return False
        elif (existing[field] or "") != (item[field] or ""):
            return False
    return True


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_existing(existing: sqlite3.Row, item: dict[str, Any]) -> dict[str, Any]:
    merged = dict(item)
    for field in ("email", "sso", "password", "proxy_url"):
        if not merged.get(field) and existing[field]:
            merged[field] = existing[field]
    for field in ("extra_json", "exporter_status_json"):
        try:
            current = json.loads(existing[field] or "{}")
        except (TypeError, ValueError):
            current = {}
        try:
            incoming = json.loads(merged[field] or "{}")
        except (TypeError, ValueError):
            incoming = {}
        merged[field] = json.dumps(
            _deep_merge(
                current if isinstance(current, dict) else {},
                incoming if isinstance(incoming, dict) else {},
            ),
            ensure_ascii=False,
        )
    return merged


def _backup_database(conn: sqlite3.Connection) -> Path:
    directory = _shared.RUNTIME_DIR / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"console-before-account-import-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    with closing(sqlite3.connect(path)) as destination:
        conn.backup(destination)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def import_migration_document(document: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("schema") != MIGRATION_SCHEMA:
        raise HTTPException(status_code=422, detail="unsupported account migration schema")
    source = document.get("accounts")
    if not isinstance(source, list) or not source:
        raise HTTPException(status_code=422, detail="migration contains no accounts")
    if len(source) > MAX_MIGRATION_ACCOUNTS:
        raise HTTPException(status_code=413, detail="migration contains too many accounts")
    accounts, duplicates_removed = _deduplicate(source)
    inserted = updated = unchanged = 0
    backup_path: Path | None = None
    with _shared.db_lock, closing(_shared.get_conn()) as conn:
        existing_rows: list[sqlite3.Row | None] = []
        planned_accounts: list[dict[str, Any]] = []
        for item in accounts:
            existing = _find_existing(conn, item)
            existing_rows.append(existing)
            planned = _merge_existing(existing, item) if existing is not None else item
            planned_accounts.append(planned)
            if existing is None:
                inserted += 1
            elif _same_account(existing, planned):
                unchanged += 1
            else:
                updated += 1
        if not dry_run and (inserted or updated):
            backup_path = _backup_database(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item, existing in zip(planned_accounts, existing_rows):
                    if existing is None:
                        columns = ",".join(ACCOUNT_FIELDS)
                        placeholders = ",".join("?" for _ in ACCOUNT_FIELDS)
                        conn.execute(
                            f"INSERT INTO accounts ({columns}, task_id) "
                            f"VALUES ({placeholders}, NULL)",
                            tuple(item[field] for field in ACCOUNT_FIELDS),
                        )
                    elif not _same_account(existing, item):
                        assignments = ",".join(f"{field}=?" for field in UPDATE_FIELDS)
                        conn.execute(
                            f"UPDATE accounts SET {assignments} WHERE id=?",
                            tuple(item[field] for field in UPDATE_FIELDS)
                            + (int(existing["id"]),),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "source_count": len(source),
        "unique_count": len(accounts),
        "duplicates_removed": duplicates_removed,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "backup": backup_path.name if backup_path else "",
    }

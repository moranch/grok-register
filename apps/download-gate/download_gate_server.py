from __future__ import annotations

import cgi
import base64
import csv
import hashlib
import hmac
import html
import io
import json
import mimetypes
import os
import re
import shutil
import secrets
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


def normalize_admin_path(value: str) -> str:
    path = "/" + str(value or "/dg-admin").strip().strip("/")
    if path in {"", "/", "/admin", "/download", "/api"}:
        return "/dg-admin"
    return path


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DOWNLOAD_GATE_DATA_DIR", ROOT / "download_gate_data")).expanduser().resolve()
ZIP_DIR = DATA_DIR / "zips"
JSON_DIR = DATA_DIR / "jsons"
BACKUP_DIR = DATA_DIR / "backups"
MANIFEST_PATH = DATA_DIR / "manifest.json"
ANNOUNCEMENT_PATH = DATA_DIR / "announcement.json"
ADMIN_PASSWORD_PATH = DATA_DIR / "admin_password.txt"
ADMIN_PATH = normalize_admin_path(os.environ.get("DOWNLOAD_GATE_ADMIN_PATH", "/dg-admin"))
INTERNAL_API_TOKEN = os.environ.get("DOWNLOAD_GATE_INTERNAL_TOKEN", "").strip()
CONSOLE_URL = os.environ.get("DOWNLOAD_GATE_CONSOLE_URL", "").strip().rstrip("/")
CONSOLE_TIMEOUT_SECONDS = max(int(os.environ.get("DOWNLOAD_GATE_CONSOLE_TIMEOUT", "120") or 120), 5)
APP_VERSION = "2026.07.26.04"
CLAIM_TTL_SECONDS = 24 * 60 * 60
BATCH_DOWNLOAD_TTL_SECONDS = 10 * 60
MAX_BATCH_KEYS = 20
MAX_ISSUE_CARDS = 500
SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
CARD_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CARD_KEY_GROUP_SIZE = 4
CARD_KEY_DEFAULT_LENGTH = 12
CARD_KEY_LENGTHS = (12, 16, 20)
CARD_STATUS_LABELS = {
    "issued": "未使用卡密",
    "provisioning": "正在分配",
    "claimed": "领取成功",
    "retryable": "领取超时 · 可重试",
    "failed": "领取失败",
    "void": "已作废",
}
CARD_PLATFORMS = (
    "grok",
    "chatgpt",
    "cursor",
    "kiro",
    "windsurf",
    "trae",
    "cerebras",
    "openblocklabs",
    "tavily",
    "blink",
    "anything",
)
GROK_OIDC_ISSUER = "https://auth.x.ai"
GROK_OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_OIDC_SCOPE = "openid profile email offline_access grok-cli:access api:access"
GROK_AUTH_REGISTRY_KEY = f"{GROK_OIDC_ISSUER}::{GROK_OIDC_CLIENT_ID}"
SUB2API_DATA_TYPE = "sub2api-data"
SUB2API_DATA_VERSION = 1
GROKCLI2API_VARIANT = "grokcli2api"
GROKCLI2API_VARIANT_ALIASES = {"grokcli", "grokcli2api", "grokcli-2api"}
ACCOUNT_MIGRATION_SCHEMA = "grok-register.account-migration.v1"
MAX_ACCOUNT_MIGRATION_ITEMS = 5000
ACCOUNT_LIST_STATUSES = {"all", "ready", "unverified", "invalid", "delivered", "leased"}
ACCOUNT_LIST_FIELDS = (
    "id",
    "platform",
    "email",
    "status",
    "lifecycle_status",
    "validity_status",
    "plan_state",
    "created_at",
    "last_checked_at",
    "cpa_status",
    "credential_ready",
    "account_alive",
    "probe_checked_at",
    "probe_kind",
    "failure_kind",
    "last_error",
    "delivered",
    "leased",
    "recently_verified",
    "inventory_status",
    "model_test_model",
    "model_test_ok",
    "model_test_status",
    "model_test_checked_at",
    "model_test_latency_ms",
    "model_test_transport",
    "model_test_failure_kind",
    "model_test_error",
)


def normalize_card_platform(value: str | None) -> str:
    platform = str(value or "grok").strip().lower()
    if platform not in CARD_PLATFORMS:
        raise ValueError(f"不支持的卡密平台：{platform}")
    return platform


def normalize_card_required_model(platform: str, value: str | None) -> str:
    if platform == "grok":
        return ""
    return str(value or "").strip()[:100]


def card_status_view(card: dict) -> tuple[str, str]:
    status = str(card.get("status") or "issued").strip().lower()
    error = str(card.get("last_error") or "").strip()
    if status == "issued" and error:
        lowered = error.lower()
        retryable_markers = (
            "timed out",
            "timeout",
            "already in progress",
            "connection failed",
            "connection reset",
            "temporarily unavailable",
            "http 502",
            "http 503",
            "http 504",
        )
        status = "retryable" if any(marker in lowered for marker in retryable_markers) else "failed"
    if status not in CARD_STATUS_LABELS:
        status = "issued"
    return status, CARD_STATUS_LABELS[status]

SESSIONS: dict[str, float] = {}
BATCH_DOWNLOADS: dict[str, dict] = {}
MANIFEST_LOCK = threading.RLock()
CARD_LOCKS_GUARD = threading.Lock()
CARD_LOCKS: dict[str, threading.RLock] = {}
SENSITIVE_PATH_PARTS = {
    ".git",
    ".svn",
    ".hg",
    ".bzr",
    ".idea",
    ".vscode",
    ".ssh",
    ".github",
    "__pycache__",
    "node_modules",
    "runtime",
    "download_gate_data",
    "zips",
    "jsons",
}
SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".user.ini",
    ".htaccess",
    ".htpasswd",
    "admin_password.txt",
    "manifest.json",
    "announcement.json",
    "download_gate_server.py",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
}
SENSITIVE_NAME_KEYWORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "credentials",
    "private",
)
SENSITIVE_SUFFIXES = (
    ".bak",
    ".backup",
    ".old",
    ".tmp",
    ".temp",
    ".log",
    ".sql",
    ".sqlite",
    ".db",
    ".pyc",
    ".pyo",
    ".pem",
    ".key",
    ".crt",
    ".cer",
)


def generate_admin_password() -> str:
    token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")
    return "ADM-" + token[:24]


def load_admin_password() -> str:
    override = os.environ.get("DOWNLOAD_GATE_ADMIN_PASSWORD", "").strip()
    if override:
        return override
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ADMIN_PASSWORD_PATH.exists():
        saved = ADMIN_PASSWORD_PATH.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    password = generate_admin_password()
    ADMIN_PASSWORD_PATH.write_text(password + "\n", encoding="utf-8")
    return password


ADMIN_PASSWORD = load_admin_password()


def ensure_dirs() -> None:
    with MANIFEST_LOCK:
        ZIP_DIR.mkdir(parents=True, exist_ok=True)
        JSON_DIR.mkdir(parents=True, exist_ok=True)
        if not MANIFEST_PATH.exists():
            save_manifest({"bundles": {}, "keys": {}, "cards": {}})


def migrate_manifest(data: dict) -> tuple[dict, bool]:
    if not isinstance(data, dict):
        data = {}
    changed = False
    for name in ("bundles", "keys", "cards"):
        if not isinstance(data.get(name), dict):
            data[name] = {}
            changed = True

    # 旧 Compose 没有给 DownloadGate 设置 TZ，历史卡密时间因此以 UTC
    # 无时区字符串保存。首次升级到 Asia/Shanghai 时统一平移并写入标记，
    # 避免后台继续显示少 8 小时，也避免后续启动重复转换。
    metadata = data.get("_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        data["_metadata"] = metadata
        changed = True
    if os.environ.get("TZ", "").strip() == "Asia/Shanghai":
        migration_version = str(metadata.get("timestamp_migration") or "")
        migrate_timestamp_values = metadata.get("timestamp_timezone") != "Asia/Shanghai"
        migrate_batch_labels = migration_version != "legacy-utc-to-cst-v2"
        timestamp_fields = {
            "created_at",
            "bound_at",
            "claimed_at",
            "provisioning_at",
            "provisioned_at",
            "last_failed_at",
            "stock_assigned_at",
            "voided_at",
        }
        china_tz = timezone(timedelta(hours=8))
        backup_created = False
        for collection_name in ("bundles", "cards"):
            for item in data[collection_name].values():
                if not isinstance(item, dict):
                    continue
                if migrate_timestamp_values:
                    for field in timestamp_fields:
                        raw = str(item.get(field) or "").strip()
                        if not raw:
                            continue
                        try:
                            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            continue
                        if not backup_created:
                            backup_manifest("timezone-migration-utc-to-cst")
                            backup_created = True
                        item[field] = (
                            parsed.replace(tzinfo=timezone.utc)
                            .astimezone(china_tz)
                            .strftime("%Y-%m-%d %H:%M:%S")
                        )
                batch = str(item.get("batch") or "").strip()
                if migrate_batch_labels and batch.startswith("batch-"):
                    try:
                        batch_time = datetime.strptime(batch, "batch-%Y%m%d-%H%M%S")
                    except ValueError:
                        continue
                    if not backup_created:
                        backup_manifest("timezone-migration-utc-to-cst")
                        backup_created = True
                    item["batch"] = (
                        batch_time.replace(tzinfo=timezone.utc)
                        .astimezone(china_tz)
                        .strftime("batch-%Y%m%d-%H%M%S")
                    )
        if migrate_timestamp_values or migrate_batch_labels:
            metadata["timestamp_timezone"] = "Asia/Shanghai"
            metadata["timestamp_migration"] = "legacy-utc-to-cst-v2"
            changed = True
    bundles = data["bundles"]
    keys = data["keys"]
    cards = data["cards"]

    raw_cards = data.get("cards") if isinstance(data.get("cards"), dict) else {}
    historical: dict[str, str] = {}
    for raw_key, raw_bundle_id in list(keys.items()):
        key = normalize_key(str(raw_key or ""))
        bundle_id = str(raw_bundle_id or "").strip()
        card = raw_cards.get(key)
        revoked = isinstance(card, dict) and str(card.get("status") or "") == "void"
        if key and bundle_id and not revoked:
            historical[key] = bundle_id
    for bundle_id, bundle in bundles.items():
        if not isinstance(bundle, dict):
            continue
        key = normalize_key(str(bundle.get("key") or ""))
        card = raw_cards.get(key)
        revoked = isinstance(card, dict) and str(card.get("status") or "") == "void"
        if key and not revoked:
            historical.setdefault(key, str(bundle_id))
    if historical != keys:
        data["keys"] = keys = historical
        changed = True

    for key, bundle_id in historical.items():
        bundle = bundles.get(bundle_id) if isinstance(bundles.get(bundle_id), dict) else {}
        bundle_claimed = bool(bundle.get("bound_at") or bundle.get("bound_client"))
        card = cards.get(key)
        if not isinstance(card, dict):
            platform = normalize_card_platform(bundle.get("platform"))
            card = {
                "key": key,
                "status": "claimed" if bundle_claimed else "issued",
                "batch": "legacy",
                "created_at": str(bundle.get("created_at") or now_text()),
                "bundle_id": bundle_id,
                "platform": platform,
                "required_model": normalize_card_required_model(
                    platform, bundle.get("required_model")
                ),
            }
            if bundle_claimed:
                card["claimed_at"] = str(bundle.get("bound_at") or now_text())
            cards[key] = card
            changed = True
        else:
            if card.get("key") != key:
                card["key"] = key
                changed = True
            if not card.get("bundle_id"):
                card["bundle_id"] = bundle_id
                changed = True
            if card.get("status") not in {"issued", "provisioning", "claimed", "void"}:
                card["status"] = "claimed" if bundle_claimed else "issued"
                changed = True
    for key, card in cards.items():
        if not isinstance(card, dict):
            continue
        if str(card.get("status") or "") == "void" and key in keys:
            keys.pop(key, None)
            changed = True
        bundle_id = str(card.get("bundle_id") or "")
        bundle = bundles.get(bundle_id) if isinstance(bundles.get(bundle_id), dict) else {}
        platform = normalize_card_platform(card.get("platform") or bundle.get("platform"))
        required_model = normalize_card_required_model(
            platform,
            card.get("required_model") or bundle.get("required_model"),
        )
        if card.get("platform") != platform:
            card["platform"] = platform
            changed = True
        if card.get("required_model") != required_model:
            card["required_model"] = required_model
            changed = True
        if bundle and bundle.get("platform") != platform:
            bundle["platform"] = platform
            changed = True
    # Browser/device binding was removed. Keep the original claim timestamp and
    # audit fields, but discard legacy fingerprint hashes on first load.
    for bundle in bundles.values():
        if isinstance(bundle, dict) and bundle.get("bound_client"):
            bundle["bound_client"] = ""
            changed = True
    return data, changed


def load_manifest() -> dict:
    with MANIFEST_LOCK:
        ensure_dirs()
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {"bundles": {}, "keys": {}, "cards": {}}
        data, changed = migrate_manifest(data)
        if changed:
            save_manifest(data)
        return data


def save_manifest(data: dict) -> None:
    with MANIFEST_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = MANIFEST_PATH.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, MANIFEST_PATH)


def sync_manifest(target: dict, source: dict) -> dict:
    if target is not source:
        target.clear()
        target.update(source)
    return target


def card_lock(card_key: str) -> threading.RLock:
    key = normalize_key(card_key)
    with CARD_LOCKS_GUARD:
        lock = CARD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            CARD_LOCKS[key] = lock
        return lock


@contextmanager
def try_card_lock(card_key: str):
    lock = card_lock(card_key)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


@contextmanager
def lock_card_keys(card_keys: list[str]):
    locks = [card_lock(key) for key in sorted({normalize_key(key) for key in card_keys if normalize_key(key)})]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def load_announcement() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    default = {"enabled": False, "title": "公告", "content": "", "pool_closed": False}
    if not ANNOUNCEMENT_PATH.exists():
        return default
    try:
        data = json.loads(ANNOUNCEMENT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(data, dict):
        return default
    title = str(data.get("title") or "公告").strip()[:60] or "公告"
    content = str(data.get("content") or "").strip()[:3000]
    return {
        "enabled": bool(data.get("enabled")) and bool(content),
        "title": title,
        "content": content,
        "pool_closed": bool(data.get("pool_closed")),
        "updated_at": str(data.get("updated_at") or ""),
    }


def save_announcement(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    title = str(data.get("title") or "公告").strip()[:60] or "公告"
    content = str(data.get("content") or "").strip()[:3000]
    payload = {
        "enabled": bool(data.get("enabled")) and bool(content),
        "title": title,
        "content": content,
        "pool_closed": bool(data.get("pool_closed")),
        "updated_at": now_text(),
    }
    ANNOUNCEMENT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def text_lines_html(value: str) -> str:
    lines = str(value or "").splitlines() or [""]
    return "<br>".join(html.escape(line) for line in lines)


def pool_closed_message() -> str:
    return "当前号池为空，未激活卡密暂不可取件。已取件用户可在有效期内继续下载。"


def backup_manifest(reason: str) -> Path | None:
    if not MANIFEST_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(reason or "manual")).strip("-")
    safe_reason = safe_reason[:40] or "manual"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = BACKUP_DIR / f"manifest-{stamp}-{safe_reason}.json"
    index = 2
    while target.exists():
        target = BACKUP_DIR / f"manifest-{stamp}-{safe_reason}-{index}.json"
        index += 1
    shutil.copy2(MANIFEST_PATH, target)
    return target


def bundle_zip_path(bundle_id: str) -> Path | None:
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id or not all(ch.isalnum() or ch in "-_" for ch in bundle_id):
        return None
    return ZIP_DIR / f"{bundle_id}.zip"


def bundle_json_path(bundle_id: str) -> Path | None:
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id or not all(ch.isalnum() or ch in "-_" for ch in bundle_id):
        return None
    return JSON_DIR / f"{bundle_id}.json"


def bundle_variant_json_path(bundle_id: str, variant: str) -> Path | None:
    bundle_id = str(bundle_id or "").strip()
    normalized_variant = str(variant or "").strip().lower()
    if not bundle_id or not all(ch.isalnum() or ch in "-_" for ch in bundle_id):
        return None
    if normalized_variant != GROKCLI2API_VARIANT:
        return None
    return JSON_DIR / f"{bundle_id}.{normalized_variant}.json"


def bundle_payload_path(bundle_id: str, bundle: dict | None = None) -> Path | None:
    bundle = bundle if isinstance(bundle, dict) else {}
    json_path = bundle_json_path(bundle_id)
    if json_path and (bundle.get("download_format") == "json" or json_path.exists()):
        return json_path
    return bundle_zip_path(bundle_id)


def bundle_payload_exists(bundle_id: str, bundle: dict | None = None) -> bool:
    path = bundle_payload_path(bundle_id, bundle)
    return bool(path and path.exists())


def bundle_download_name(bundle_id: str, bundle: dict | None = None) -> str:
    bundle = bundle if isinstance(bundle, dict) else {}
    path = bundle_payload_path(bundle_id, bundle)
    if path and path.suffix.lower() == ".json":
        return safe_filename(str(bundle.get("json_name") or next(iter(bundle.get("files") or []), "account.json")))
    return str(bundle.get("zip_name") or f"{bundle_id}.zip")


def bundle_download_path(bundle_id: str, bundle: dict | None = None, *, admin: bool = False) -> str:
    payload = bundle_payload_path(bundle_id, bundle)
    suffix = ".json" if payload and payload.suffix.lower() == ".json" else ".zip"
    path = f"/download/{quote(str(bundle_id or ''), safe='')}{suffix}"
    return path + ("?admin=1" if admin else "")


def delete_bundle_from_manifest(
    manifest: dict,
    bundle_id: str,
    *,
    delete_zip: bool = True,
    include_related: bool = False,
) -> int:
    bundle_id = str(bundle_id or "").strip()
    bundles = manifest.setdefault("bundles", {})
    keys = manifest.setdefault("keys", {})
    cards = manifest.setdefault("cards", {})
    if bundle_id not in bundles:
        return 0

    target_ids = {bundle_id}
    target_keys = {normalize_key(str((bundles.get(bundle_id) or {}).get("key") or ""))}
    target_keys.discard("")

    if include_related:
        changed = True
        while changed:
            changed = False
            for other_id, bundle in list(bundles.items()):
                if other_id in target_ids or not isinstance(bundle, dict):
                    continue
                bundle_key = normalize_key(str(bundle.get("key") or ""))
                replaced_by = str(bundle.get("replaced_by") or "").strip()
                if (bundle_key and bundle_key in target_keys) or replaced_by in target_ids:
                    target_ids.add(str(other_id))
                    if bundle_key:
                        target_keys.add(bundle_key)
                    changed = True

    deleted = 0
    for target_id in list(target_ids):
        if bundles.pop(target_id, None) is not None:
            deleted += 1
    for key, mapped_id in list(keys.items()):
        if mapped_id in target_ids or normalize_key(str(key or "")) in target_keys:
            keys.pop(key, None)
            normalized_key = normalize_key(str(key or ""))
            card = cards.get(normalized_key)
            if isinstance(card, dict):
                card["status"] = "void"
                card["voided_at"] = now_text()
                card["bundle_id"] = ""
    for bundle in bundles.values():
        if bundle.get("replaced_by") in target_ids:
            bundle.pop("replaced_by", None)

    if delete_zip:
        for target_id in target_ids:
            for payload_path in (
                bundle_zip_path(target_id),
                bundle_json_path(target_id),
                bundle_variant_json_path(target_id, GROKCLI2API_VARIANT),
            ):
                if payload_path and payload_path.exists():
                    payload_path.unlink()
    return deleted


def clear_orphan_zips(manifest: dict) -> int:
    bundles = manifest.setdefault("bundles", {})
    active_zip_names = {f"{bundle_id}.zip" for bundle_id in bundles}
    deleted = 0
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    for zip_path in ZIP_DIR.glob("*.zip"):
        if zip_path.name not in active_zip_names:
            zip_path.unlink()
            deleted += 1
    active_json_names = {
        name
        for bundle_id in bundles
        for name in (
            f"{bundle_id}.json",
            f"{bundle_id}.{GROKCLI2API_VARIANT}.json",
        )
    }
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    for json_path in JSON_DIR.glob("*.json"):
        if json_path.name not in active_json_names:
            json_path.unlink()
            deleted += 1
    return deleted


CPA_DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_DEFAULT_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_DEFAULT_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"


def _jwt_payload(value: object) -> dict:
    try:
        segment = str(value or "").split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _utc_text(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return ""


def cpa_import_payload(document: dict) -> dict:
    """Return the exact flat CPA shape emitted by AaronL725/grok-register."""
    nested = document.get("cpa_auth") if isinstance(document.get("cpa_auth"), dict) else {}
    if nested and (nested.get("access_token") or nested.get("refresh_token")):
        source = nested
    elif document.get("type") == "xai" and (
        document.get("access_token") or document.get("refresh_token")
    ):
        source = document
    else:
        return document
    credentials = document.get("credentials") if isinstance(document.get("credentials"), dict) else {}
    access_token = str(
        source.get("access_token")
        or document.get("access_token")
        or credentials.get("access_token")
        or ""
    ).strip()
    refresh_token = str(
        source.get("refresh_token")
        or document.get("refresh_token")
        or credentials.get("refresh_token")
        or ""
    ).strip()
    if not access_token or not refresh_token:
        return document
    id_token = str(
        source.get("id_token")
        or document.get("id_token")
        or credentials.get("id_token")
        or ""
    ).strip()
    # Aaron's parse_identity() prefers id_token, then access_token.
    identity = _jwt_payload(id_token) if id_token else {}
    if not identity:
        identity = _jwt_payload(access_token)
    email = str(
        source.get("email")
        or document.get("email")
        or identity.get("email")
        or ""
    ).strip()
    subject = str(identity.get("sub") or identity.get("principal_id") or source.get("sub") or "").strip()
    expires_in = source.get("expires_in")
    if expires_in is None:
        expires_in = document.get("expires_in")
    if expires_in is None and identity.get("exp") and identity.get("iat"):
        expires_in = max(int(identity["exp"]) - int(identity["iat"]), 0)
    try:
        expires_in = int(expires_in or 21600)
    except (TypeError, ValueError):
        expires_in = 21600
    expired = _utc_text(identity.get("exp"))
    if not expired:
        expired = str(source.get("expired") or document.get("expired") or "")
    last_refresh = str(source.get("last_refresh") or document.get("last_refresh") or "")
    if not last_refresh:
        last_refresh = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Keep field names and insertion order aligned with upstream schema.py.
    payload = {
        "type": "xai",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": str(source.get("token_type") or document.get("token_type") or "Bearer"),
        "expires_in": expires_in,
        "expired": expired,
        "last_refresh": last_refresh,
        "email": email,
        "sub": subject,
        "base_url": str(source.get("base_url") or document.get("base_url") or CPA_DEFAULT_BASE_URL).rstrip("/"),
        "redirect_uri": str(source.get("redirect_uri") or document.get("redirect_uri") or CPA_DEFAULT_REDIRECT_URI),
        "token_endpoint": str(source.get("token_endpoint") or document.get("token_endpoint") or CPA_DEFAULT_TOKEN_ENDPOINT),
        "auth_kind": "oauth",
    }
    if id_token:
        payload["id_token"] = id_token
    return payload


def cockpit_auth_payload(document: dict) -> dict:
    """Derive a single-account official Grok auth.json registry for Cockpit."""
    cpa = cpa_import_payload(document)
    source = cpa if cpa is not document else document
    access_token = str(source.get("access_token") or "").strip()
    refresh_token = str(source.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("CPA JSON 缺少 access_token，无法生成 Cockpit auth.json")
    subject = str(source.get("sub") or "").strip()
    entry: dict[str, object] = {
        "key": access_token,
        "auth_mode": "oidc",
        "email": str(source.get("email") or "").strip(),
        "principal_type": "User",
        "oidc_issuer": GROK_OIDC_ISSUER,
        "oidc_client_id": GROK_OIDC_CLIENT_ID,
    }
    if refresh_token:
        entry["refresh_token"] = refresh_token
    if subject:
        entry["user_id"] = subject
        entry["principal_id"] = subject
    if source.get("expired") not in (None, ""):
        entry["expires_at"] = source["expired"]
    if source.get("last_refresh") not in (None, ""):
        entry["create_time"] = source["last_refresh"]
    return {GROK_AUTH_REGISTRY_KEY: entry}


def grokcli_2api_payload(document: dict) -> dict:
    """Build the native grokcli-2api auth export wrapper for one account.

    grokcli-2api 2.x accepts an ``auth`` map and deliberately preserves SSO and
    registration-password metadata.  Those fields allow its token maintainer to
    recover an account when a refresh token is revoked instead of immediately
    removing it from the pool.
    """
    cpa = cpa_import_payload(document)
    source = cpa if cpa is not document else document
    credentials = document.get("credentials") if isinstance(document.get("credentials"), dict) else {}
    access_token = str(source.get("access_token") or credentials.get("access_token") or "").strip()
    refresh_token = str(source.get("refresh_token") or credentials.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("CPA JSON 缺少 access_token，无法生成 GrokCLI-2API 导入文件")

    email = str(source.get("email") or document.get("email") or credentials.get("email") or "").strip()
    subject = str(
        source.get("sub")
        or document.get("sub")
        or document.get("account_id")
        or credentials.get("sub")
        or ""
    ).strip()
    entry: dict[str, object] = {
        "key": access_token,
        "access_token": access_token,
        "auth_mode": "oidc",
        "email": email,
        "principal_type": "User",
        "oidc_issuer": GROK_OIDC_ISSUER,
        "oidc_client_id": GROK_OIDC_CLIENT_ID,
        "source": "grok-register-download-gate",
    }
    if refresh_token:
        entry["refresh_token"] = refresh_token
    id_token = str(source.get("id_token") or credentials.get("id_token") or "").strip()
    if id_token:
        entry["id_token"] = id_token
    if subject:
        entry["user_id"] = subject
        entry["principal_id"] = subject
    if source.get("expired") not in (None, ""):
        entry["expires_at"] = source["expired"]
    if source.get("last_refresh") not in (None, ""):
        entry["create_time"] = source["last_refresh"]
    if source.get("base_url") not in (None, ""):
        entry["base_url"] = source["base_url"]

    sso = str(
        document.get("sso")
        or document.get("sso_cookie")
        or credentials.get("sso")
        or credentials.get("sso_cookie")
        or source.get("sso")
        or source.get("sso_cookie")
        or ""
    ).strip()
    if sso.lower().startswith("sso="):
        sso = sso.split("=", 1)[1].strip()
    if sso:
        entry["sso"] = sso
        entry["sso_cookie"] = sso

    password = str(
        document.get("password")
        or document.get("register_password")
        or credentials.get("password")
        or credentials.get("register_password")
        or ""
    ).strip()
    if password:
        entry["password"] = password
        entry["register_password"] = password

    account_key = f"{GROK_OIDC_ISSUER}::{subject or GROK_OIDC_CLIENT_ID}"
    return {
        "ok": True,
        "auth": {account_key: entry},
        "count": 1,
        "exported_at": time.time(),
    }


def grokcli_2api_filename(document: dict) -> str:
    cpa = cpa_import_payload(document)
    source = cpa if cpa is not document else document
    identity = str(source.get("email") or source.get("sub") or "account").strip()
    return safe_filename(
        f"grokcli-2api-auth-{identity}.json",
        "grokcli-2api-auth-account.json",
    )


def sub2api_payload(document: dict) -> dict:
    """Derive a one-account Sub2API DataPayload for Grok OAuth import."""
    cpa = cpa_import_payload(document)
    source = cpa if cpa is not document else document
    access_token = str(source.get("access_token") or "").strip()
    refresh_token = str(source.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("CPA JSON 缺少 access_token，无法生成 Sub2API 导入文件")
    email = str(source.get("email") or "").strip()
    subject = str(source.get("sub") or "").strip()
    credentials: dict[str, object] = {
        "access_token": access_token,
    }
    if refresh_token:
        credentials["refresh_token"] = refresh_token
    credentials["token_type"] = str(source.get("token_type") or "Bearer")
    if source.get("expired") not in (None, ""):
        credentials["expires_at"] = source["expired"]
    if source.get("id_token") not in (None, ""):
        credentials["id_token"] = source["id_token"]
    credentials["client_id"] = GROK_OIDC_CLIENT_ID
    credentials["scope"] = GROK_OIDC_SCOPE
    if email:
        credentials["email"] = email
    if subject:
        credentials["sub"] = subject
    credentials["base_url"] = str(source.get("base_url") or CPA_DEFAULT_BASE_URL).rstrip("/")
    return {
        "type": SUB2API_DATA_TYPE,
        "version": SUB2API_DATA_VERSION,
        "exported_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": [],
        "accounts": [
            {
                "name": email or subject or "Grok OAuth Account",
                "platform": "grok",
                "type": "oauth",
                "credentials": credentials,
                "concurrency": 1,
                "priority": 0,
                "auto_pause_on_expired": True,
            }
        ],
    }


def sub2api_filename(document: dict) -> str:
    cpa = cpa_import_payload(document)
    source = cpa if cpa is not document else document
    identity = str(source.get("email") or source.get("sub") or "account").strip()
    return safe_filename(f"SUB2API-grok-{identity}.json", "SUB2API-grok-account.json")


def cpa_import_filename(payload: dict, fallback: str = "account.json") -> str:
    email = str(payload.get("email") or payload.get("sub") or "").strip()
    if not email:
        return safe_filename(fallback, "account.json")
    stem = email if email.lower().startswith("xai") else f"xai-{email}"
    safe_stem = "".join(char if char.isalnum() or char in "._-@" else "_" for char in stem)
    safe_stem = safe_stem.strip("._-")[:115]
    return f"CPA-{safe_stem or 'xai-account'}.json"


def normalize_delivery_json_file(filename: str, raw: bytes) -> tuple[str, bytes, dict | None]:
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return filename, raw, None
    if not isinstance(document, dict):
        return filename, raw, None
    payload = cpa_import_payload(document)
    if payload is document:
        return filename, raw, document
    normalized_name = cpa_import_filename(payload, filename)
    normalized_raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return normalized_name, normalized_raw, payload


def create_delivery_bundle(
    manifest: dict,
    *,
    title: str,
    card_key: str,
    json_files: list[tuple[str, bytes]],
    errors: list[str],
    warnings: list[str] | None = None,
    bundle_id: str = "",
    request_fingerprint: str = "",
    platform: str = "",
) -> str:
    detected_platforms: set[str] = set()
    for _filename, raw in json_files:
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(document, dict) and document.get("platform"):
            detected_platforms.add(normalize_card_platform(document.get("platform")))
    if len(detected_platforms) > 1:
        raise ValueError("一个交付包不能混合多个目标平台")
    platform = normalize_card_platform(
        platform or (next(iter(detected_platforms)) if detected_platforms else "grok")
    )
    required_model = normalize_card_required_model(platform, "")
    normalized_files: list[tuple[str, bytes]] = []
    for filename, raw in json_files:
        normalized_name, normalized_raw, _ = normalize_delivery_json_file(filename, raw)
        normalized_files.append((normalized_name, normalized_raw))
    json_files = normalized_files
    card_key = normalize_key(card_key)
    if card_key and not is_card_key_available(manifest, card_key):
        raise ValueError(f"卡密已存在：{card_key}，请换一个卡密")
    bundle_id = str(bundle_id or "").strip() or hashlib.sha1(
        f"{card_key or 'stock'}:{time.time()}:{secrets.token_hex(8)}".encode()
    ).hexdigest()[:16]
    zip_name = f"{card_key}.zip" if card_key else f"stock-{bundle_id}.zip"
    zip_path = ZIP_DIR / f"{bundle_id}.zip"
    used_names: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for index, (filename, raw) in enumerate(json_files, 1):
            arcname = filename
            if arcname.casefold() in used_names:
                arcname = unique_upload_filename(filename, used_names)
                warnings = [*(warnings or []), f"{filename} 重名，ZIP 内已改为 {arcname}"]
            else:
                used_names.add(arcname.casefold())
            zf.writestr(arcname, raw)

    if card_key:
        manifest.setdefault("keys", {})[card_key] = bundle_id
    manifest.setdefault("bundles", {})[bundle_id] = {
        "id": bundle_id,
        "key": card_key,
        "title": title,
        "platform": platform,
        "required_model": required_model,
        "zip_name": zip_name,
        "created_at": now_text(),
        "file_count": len(json_files),
        "size": zip_path.stat().st_size,
        "files": [name for name, _ in json_files],
        "identities": [
            identity
            for identity in (delivery_identity_from_raw(name, raw) for name, raw in json_files)
            if identity.get("key")
        ],
        "errors": errors,
        "warnings": warnings or [],
        "bound_client": "",
        "bound_at": "",
        "bound_ip": "",
        "bound_user_agent": "",
        "request_fingerprint": str(request_fingerprint or ""),
    }
    if card_key:
        existing_card = manifest.setdefault("cards", {}).get(card_key)
        card = existing_card if isinstance(existing_card, dict) else {}
        card.update(
            {
                "key": card_key,
                "status": "issued",
                "batch": str(card.get("batch") or "uploaded"),
                "created_at": str(card.get("created_at") or now_text()),
                "bundle_id": bundle_id,
                "platform": platform,
                "required_model": required_model,
            }
        )
        manifest["cards"][card_key] = card
    return bundle_id


def deterministic_bundle_id(card_key: str) -> str:
    return hashlib.sha256(f"download-gate-card:{normalize_key(card_key)}".encode("utf-8")).hexdigest()[:16]


def delivery_request_fingerprint(title: str, json_files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(str(title or "").encode("utf-8"))
    digest.update(b"\0")
    for filename, raw in json_files:
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def console_json_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    if not CONSOLE_URL:
        raise RuntimeError("DOWNLOAD_GATE_CONSOLE_URL is not configured")
    if not INTERNAL_API_TOKEN:
        raise RuntimeError("DOWNLOAD_GATE_INTERNAL_TOKEN is not configured")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{CONSOLE_URL}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout_seconds or CONSOLE_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8")).get("detail") or json.loads(raw.decode("utf-8")).get("error")
        except Exception:
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Console HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Console connection failed: {exc.reason}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Console returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Console response must be a JSON object")
    return result


def console_json_post(path: str, payload: dict, *, timeout_seconds: int | None = None) -> dict:
    return console_json_request(path, method="POST", payload=payload, timeout_seconds=timeout_seconds)


def console_json_get(path: str, *, timeout_seconds: int | None = None) -> dict:
    return console_json_request(path, timeout_seconds=timeout_seconds)


def _query_value(query: dict, name: str, default: str = "") -> str:
    value = query.get(name, [default])
    if isinstance(value, (list, tuple)):
        value = value[0] if value else default
    return str(value if value is not None else default)


def _bounded_query_int(
    query: dict,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(_query_value(query, name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def account_list_query(query: dict) -> dict[str, str | int]:
    """Normalize the small, non-sensitive account-list query forwarded to Console."""
    platform = _query_value(query, "platform", "grok").strip().lower()
    if platform not in CARD_PLATFORMS:
        platform = "grok"
    status = _query_value(query, "status", "all").strip().lower()
    if status not in ACCOUNT_LIST_STATUSES:
        status = "all"
    return {
        "platform": platform,
        "q": _query_value(query, "q").strip()[:160],
        "status": status,
        "page": _bounded_query_int(query, "page", 1, 1, 1_000_000),
        "page_size": _bounded_query_int(query, "page_size", 25, 10, 200),
    }


def _account_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _account_count(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, default)


def sanitize_account_list_response(payload: dict, requested: dict[str, str | int]) -> dict:
    """Enforce a second allow-list boundary before Console data reaches the browser."""
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("Console 账户列表响应缺少 items")
    items: list[dict] = []
    boolean_fields = {
        "credential_ready",
        "account_alive",
        "delivered",
        "leased",
        "recently_verified",
        "model_test_ok",
    }
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item: dict[str, object] = {}
        for field in ACCOUNT_LIST_FIELDS:
            value = raw.get(field)
            if field in boolean_fields:
                item[field] = _account_bool(value)
            elif value is None:
                item[field] = ""
            elif isinstance(value, (str, int, float, bool)):
                item[field] = value
            else:
                item[field] = str(value)
        item["email"] = str(item.get("email") or "")[:320]
        item["last_error"] = str(item.get("last_error") or "")[:1200]
        item["failure_kind"] = str(item.get("failure_kind") or "")[:120]
        item["model_test_error"] = str(item.get("model_test_error") or "")[:500]
        item["model_test_failure_kind"] = str(
            item.get("model_test_failure_kind") or ""
        )[:120]
        items.append(item)

    summary_source = payload.get("summary")
    summary_source = summary_source if isinstance(summary_source, dict) else {}
    summary = {
        key: _account_count(summary_source.get(key))
        for key in ("total", "ready", "unverified", "invalid", "delivered", "leased")
    }
    total = _account_count(payload.get("total"), summary["total"])
    if not summary["total"] and total:
        summary["total"] = total
    page = _account_count(payload.get("page"), int(requested["page"])) or 1
    page_size = _account_count(payload.get("page_size"), int(requested["page_size"])) or int(
        requested["page_size"]
    )
    pages = _account_count(payload.get("pages"))
    if not pages:
        pages = max(1, (total + page_size - 1) // page_size) if total else 1
    return {
        "ok": True,
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "summary": summary,
    }


def load_admin_accounts(query: dict) -> dict:
    requested = account_list_query(query)
    forwarded = {key: value for key, value in requested.items() if key != "q"}
    forwarded["search"] = requested["q"]
    path = f"/api/internal/accounts?{urlencode(forwarded)}"
    payload = console_json_get(path, timeout_seconds=15)
    return sanitize_account_list_response(payload, requested)


def run_admin_account_model_test(account_id: int, model: str) -> dict:
    account_id = int(account_id)
    selected_model = str(model or "grok-4.5").strip()
    if account_id <= 0:
        raise ValueError("invalid account id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", selected_model):
        raise ValueError("invalid model")
    payload = console_json_post(
        f"/api/internal/accounts/{account_id}/model-test",
        {"model": selected_model},
        timeout_seconds=65,
    )
    raw = payload.get("test")
    if not payload.get("ok") or not isinstance(raw, dict):
        raise RuntimeError("Console model-test response is invalid")
    result = {
        "account_id": _account_count(raw.get("account_id"), account_id),
        "ok": _account_bool(raw.get("ok")),
        "model_available": _account_bool(raw.get("model_available")),
        "model": str(raw.get("model") or selected_model)[:100],
        "status": _account_count(raw.get("status")),
        "latency_ms": _account_count(raw.get("latency_ms")),
        "probe_kind": "model_response",
        "transport": str(raw.get("transport") or "")[:40],
        "failure_kind": str(raw.get("failure_kind") or "")[:120],
        "refresh_recommended": _account_bool(raw.get("refresh_recommended")),
        "error": str(raw.get("error") or "")[:500],
        "checked_at": str(raw.get("checked_at") or "")[:40],
    }
    return {"ok": True, "test": result}


def load_admin_account_export(query: dict) -> tuple[str, bytes]:
    requested = account_list_query(query)
    forwarded = {
        "platform": requested["platform"],
        "status": requested["status"],
        "search": requested["q"],
    }
    payload = console_json_get(
        f"/api/internal/accounts/export?{urlencode(forwarded)}",
        timeout_seconds=30,
    )
    document = payload.get("document")
    if not payload.get("ok") or not isinstance(document, dict):
        raise RuntimeError("Console account export response is invalid")
    accounts = document.get("accounts")
    if document.get("schema") != ACCOUNT_MIGRATION_SCHEMA or not isinstance(accounts, list):
        raise RuntimeError("Console returned an unsupported account migration")
    if not 1 <= len(accounts) <= MAX_ACCOUNT_MIGRATION_ITEMS:
        raise RuntimeError("Console returned an invalid account migration size")
    raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise RuntimeError("account migration export is too large")
    filename = safe_filename(
        str(payload.get("filename") or f"grok-account-migration-{len(accounts)}.json")
    )
    return filename, raw


def import_admin_accounts(document: dict, *, dry_run: bool = False) -> dict:
    if not isinstance(document, dict) or document.get("schema") != ACCOUNT_MIGRATION_SCHEMA:
        raise ValueError("不支持的账号迁移文件")
    accounts = document.get("accounts")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= MAX_ACCOUNT_MIGRATION_ITEMS:
        raise ValueError("账号迁移文件为空或数量超过限制")
    payload = console_json_post(
        f"/api/internal/accounts/import?dry_run={'true' if dry_run else 'false'}",
        document,
        timeout_seconds=90,
    )
    if not payload.get("ok"):
        raise RuntimeError("Console account import failed")
    return {
        "ok": True,
        "dry_run": _account_bool(payload.get("dry_run")),
        "source_count": _account_count(payload.get("source_count")),
        "unique_count": _account_count(payload.get("unique_count")),
        "duplicates_removed": _account_count(payload.get("duplicates_removed")),
        "inserted": _account_count(payload.get("inserted")),
        "updated": _account_count(payload.get("updated")),
        "unchanged": _account_count(payload.get("unchanged")),
        "backup": Path(str(payload.get("backup") or "")).name,
    }


def console_auto_replenish_request(*, method: str = "GET", payload: dict | None = None) -> dict:
    if INTERNAL_API_TOKEN:
        return console_json_request(
            "/api/internal/inventory/auto-replenish",
            method=method,
            payload=payload,
            timeout_seconds=10,
        )
    try:
        return console_json_request(
            "/api/inventory/auto-replenish",
            method=method,
            payload=payload,
            timeout_seconds=10,
        )
    except RuntimeError as exc:
        if "Console HTTP 401" not in str(exc) and "Console HTTP 403" not in str(exc):
            raise
    return console_json_request(
        "/api/internal/inventory/auto-replenish",
        method=method,
        payload=payload,
        timeout_seconds=10,
    )


def load_auto_replenish_status() -> tuple[dict, str]:
    try:
        return console_auto_replenish_request(), ""
    except Exception as exc:
        return {}, str(exc)


def public_pool_summary() -> dict:
    """Build the non-sensitive, public account-pool status used by the pickup page."""
    status, error = load_auto_replenish_status()

    def number(value: object, default: int = 0) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    candidate_stock = number(status.get("candidate_stock"), number(status.get("available_stock")))
    verified_stock = min(candidate_stock, number(status.get("verified_stock")))
    unverified_stock = max(
        0, number(status.get("unverified_stock"), candidate_stock - verified_stock)
    )
    ttl_minutes = number(status.get("prevalidate_ttl_minutes"), 60)
    known = not bool(error)
    percent = round(verified_stock * 100 / candidate_stock) if candidate_stock else 0
    return {
        "pool": {
            "label": "Grok 账号池",
            "platform": "grok",
            "availableKnown": known,
            "availableCount": verified_stock if known else 0,
            "candidateCount": candidate_stock if known else 0,
            "unverifiedCount": unverified_stock if known else 0,
            "percent": percent if known else 0,
            "verificationTtlMinutes": ttl_minutes,
            "checkedAt": now_text(),
        },
        "settings": {
            "claimValidHours": max(1, CLAIM_TTL_SECONDS // 3600),
            "refreshSeconds": 10,
        },
    }


def dynamic_document_filename(document: dict) -> str:
    payload = cpa_import_payload(document)
    if payload is not document:
        return cpa_import_filename(payload)
    email = deep_first_text(document, {"email"})
    account_id = str(document.get("account_id") or document.get("id") or "").strip()
    stem = email or account_id or "account"
    return safe_filename(f"{stem}.json", "account.json")


def write_dynamic_bundle_json_atomic(manifest: dict, card: dict, document: dict) -> tuple[str, dict]:
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    key = normalize_key(str(card.get("key") or ""))
    bundle_id = str(card.get("bundle_id") or deterministic_bundle_id(key))
    json_path = JSON_DIR / f"{bundle_id}.json"
    temp_path = JSON_DIR / f".{bundle_id}.{secrets.token_hex(6)}.tmp"
    export_document = cpa_import_payload(document)
    filename = dynamic_document_filename(export_document)
    raw = json.dumps(export_document, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        temp_path.write_bytes(raw)
        os.replace(temp_path, json_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    variants: dict[str, dict] = {}
    grokcli_path = bundle_variant_json_path(bundle_id, GROKCLI2API_VARIANT)
    try:
        grokcli_document = grokcli_2api_payload(document)
        grokcli_raw = json.dumps(grokcli_document, ensure_ascii=False, indent=2).encode("utf-8")
        if grokcli_path is None:
            raise ValueError("GrokCLI-2API 交付路径无效")
        grokcli_temp_path = JSON_DIR / f".{bundle_id}.{GROKCLI2API_VARIANT}.{secrets.token_hex(6)}.tmp"
        try:
            grokcli_temp_path.write_bytes(grokcli_raw)
            os.replace(grokcli_temp_path, grokcli_path)
        finally:
            if grokcli_temp_path.exists():
                grokcli_temp_path.unlink()
        variants[GROKCLI2API_VARIANT] = {
            "format": "grokcli-2api-auth-map-v1",
            "file_name": grokcli_2api_filename(document),
            "size": grokcli_path.stat().st_size,
        }
    except (OSError, ValueError):
        if grokcli_path and grokcli_path.exists():
            grokcli_path.unlink()
    existing_bundle = manifest.setdefault("bundles", {}).get(bundle_id)
    existing_bundle = existing_bundle if isinstance(existing_bundle, dict) else {}
    platform = normalize_card_platform(card.get("platform") or document.get("platform"))
    required_model = normalize_card_required_model(
        platform, card.get("required_model")
    )
    bundle = {
        "id": bundle_id,
        "key": key,
        "title": f"{platform} 账号交付 JSON",
        "platform": platform,
        "required_model": required_model,
        "zip_name": "",
        "json_name": filename,
        "download_format": "json",
        "created_at": str(existing_bundle.get("created_at") or card.get("provisioned_at") or now_text()),
        "file_count": 1,
        "size": json_path.stat().st_size,
        "files": [filename],
        "identities": [delivery_identity_from_json(export_document, filename=filename)],
        "errors": [],
        "warnings": [],
        "variants": variants,
        "bound_client": str(existing_bundle.get("bound_client") or ""),
        "bound_at": str(existing_bundle.get("bound_at") or ""),
        "bound_ip": str(existing_bundle.get("bound_ip") or ""),
        "bound_user_agent": str(existing_bundle.get("bound_user_agent") or ""),
        "dynamic": True,
        "account_id": str(document.get("account_id") or document.get("id") or ""),
        "order_id": str(card.get("order_id") or ""),
        "lease_id": str(card.get("lease_id") or ""),
    }
    manifest.setdefault("bundles", {})[bundle_id] = bundle
    manifest.setdefault("keys", {})[key] = bundle_id
    return bundle_id, bundle


def migrate_existing_bundle_json(manifest: dict) -> int:
    """Rewrite legacy account-delivery wrappers as flat CPA auth JSON files."""
    migrated = 0
    for bundle_id, bundle in manifest.setdefault("bundles", {}).items():
        if not isinstance(bundle, dict):
            continue
        direct_json_path = JSON_DIR / f"{bundle_id}.json"
        if direct_json_path.exists():
            try:
                original_raw = direct_json_path.read_bytes()
                original_name = str(
                    bundle.get("json_name")
                    or next(iter(bundle.get("files") or []), "account.json")
                )
                name, raw, payload = normalize_delivery_json_file(original_name, original_raw)
            except (OSError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload is not None and (
                raw != original_raw
                or name != original_name
                or bundle.get("format") != "cpa-aaron-v1"
            ):
                temp_json_path = JSON_DIR / f".{bundle_id}.{secrets.token_hex(6)}.tmp"
                try:
                    temp_json_path.write_bytes(raw)
                    os.replace(temp_json_path, direct_json_path)
                finally:
                    if temp_json_path.exists():
                        temp_json_path.unlink()
                bundle["files"] = [name]
                bundle["file_count"] = 1
                bundle["size"] = direct_json_path.stat().st_size
                bundle["json_name"] = name
                bundle["download_format"] = "json"
                bundle["format"] = "cpa-aaron-v1"
                bundle["identities"] = [delivery_identity_from_json(payload, filename=name)]
                migrated += 1
            continue
        zip_path = ZIP_DIR / f"{bundle_id}.zip"
        if not zip_path.exists():
            continue
        entries: list[tuple[str, bytes]] = []
        identities: list[dict] = []
        changed = False
        used_names: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "r") as source:
                for member in source.infolist():
                    if member.is_dir():
                        continue
                    original_name = member.filename
                    original_raw = source.read(member.filename)
                    name = original_name
                    raw = original_raw
                    payload = None
                    if name.lower().endswith(".json"):
                        normalized_name, normalized_raw, payload = normalize_delivery_json_file(name, raw)
                        name, raw = normalized_name, normalized_raw
                    if name.casefold() in used_names:
                        name = unique_upload_filename(name, used_names)
                    else:
                        used_names.add(name.casefold())
                    if name != original_name or raw != original_raw:
                        changed = True
                    entries.append((name, raw))
                    if isinstance(payload, dict):
                        identity = delivery_identity_from_json(payload, filename=name)
                        if identity.get("key"):
                            identities.append(identity)
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        json_entries = [(name, raw) for name, raw in entries if name.lower().endswith(".json")]
        if bundle.get("dynamic") and len(entries) == 1 and len(json_entries) == 1:
            JSON_DIR.mkdir(parents=True, exist_ok=True)
            name, raw = json_entries[0]
            json_path = JSON_DIR / f"{bundle_id}.json"
            temp_json_path = JSON_DIR / f".{bundle_id}.{secrets.token_hex(6)}.tmp"
            try:
                temp_json_path.write_bytes(raw)
                os.replace(temp_json_path, json_path)
            finally:
                if temp_json_path.exists():
                    temp_json_path.unlink()
            zip_path.unlink()
            bundle["files"] = [name]
            bundle["file_count"] = 1
            bundle["size"] = json_path.stat().st_size
            bundle["json_name"] = name
            bundle["zip_name"] = ""
            bundle["download_format"] = "json"
            bundle["format"] = "cpa-flat-v1"
            if identities:
                bundle["identities"] = identities
            migrated += 1
            continue
        if not changed:
            continue
        temp_path = ZIP_DIR / f".{bundle_id}.{secrets.token_hex(6)}.tmp"
        try:
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as target:
                for name, raw in entries:
                    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    target.writestr(info, raw)
            os.replace(temp_path, zip_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        json_names = [name for name, _ in entries if name.lower().endswith(".json")]
        bundle["files"] = json_names
        bundle["file_count"] = len(json_names)
        bundle["size"] = zip_path.stat().st_size
        if identities:
            bundle["identities"] = identities
        bundle["format"] = "cpa-flat-v1"
        migrated += 1
    return migrated


def delivery_document_from_response(response: dict) -> dict | None:
    document = response.get("document")
    if isinstance(document, dict):
        return document
    documents = response.get("documents")
    if isinstance(documents, list):
        return next((item for item in documents if isinstance(item, dict)), None)
    return None


def provision_card_bundle(manifest: dict, card_key: str) -> tuple[str, dict]:
    key = normalize_key(card_key)
    with card_lock(key):
        with MANIFEST_LOCK:
            sync_manifest(manifest, load_manifest())
            card = manifest.setdefault("cards", {}).get(key)
            if not isinstance(card, dict) or card.get("status") == "void":
                raise KeyError("卡密不存在或已作废")
            platform = normalize_card_platform(card.get("platform"))
            required_model = normalize_card_required_model(
                platform, card.get("required_model")
            )
            bundle_id = str(card.get("bundle_id") or deterministic_bundle_id(key))
            bundle = manifest.setdefault("bundles", {}).get(bundle_id)
            if isinstance(bundle, dict) and bundle_payload_exists(bundle_id, bundle):
                return bundle_id, bundle
            card["status"] = "provisioning"
            card["bundle_id"] = bundle_id
            card["provisioning_at"] = now_text()
            card["last_error"] = ""
            lease_id = str(card.get("lease_id") or "")
            lease_token = str(card.get("lease_token") or "")
            save_manifest(manifest)

        try:
            recovered: dict | None = None
            if not lease_id or not lease_token:
                reserved = console_json_post(
                    "/api/internal/account-deliveries/reserve",
                    {
                        "card_key": key,
                        "platform": platform,
                        "required_model": required_model,
                    },
                )
                reserved_platform = normalize_card_platform(
                    reserved.get("platform") or platform
                )
                if reserved_platform != platform:
                    raise RuntimeError("Console returned an account from another platform")
                state = str(reserved.get("state") or "")
                if state == "consumed":
                    recovered = reserved
                    if delivery_document_from_response(recovered) is None:
                        recovered = console_json_get(
                            f"/api/internal/account-deliveries/by-card/{quote(key, safe='')}"
                        )
                elif state == "ready":
                    lease_id = str(reserved.get("lease_id") or "")
                    lease_token = str(reserved.get("lease_token") or "")
                    if not lease_id or not lease_token:
                        raise RuntimeError("Console reserve response is missing lease credentials")
                    with MANIFEST_LOCK:
                        sync_manifest(manifest, load_manifest())
                        card = manifest.setdefault("cards", {}).get(key)
                        if not isinstance(card, dict) or card.get("status") == "void":
                            raise KeyError("卡密不存在或已作废")
                        card["order_id"] = str(reserved.get("order_id") or "")
                        card["lease_id"] = lease_id
                        card["lease_token"] = lease_token
                        card["bundle_id"] = bundle_id
                        save_manifest(manifest)
                else:
                    raise RuntimeError(
                        str(reserved.get("detail") or reserved.get("error") or "Console reserve is not ready")
                    )

            committed = recovered
            if committed is None:
                committed = console_json_post(
                    "/api/internal/account-deliveries/commit",
                    {
                        "card_key": key,
                        "lease_id": lease_id,
                        "lease_token": lease_token,
                        "bundle_id": bundle_id,
                    },
                )
            document = delivery_document_from_response(committed)
            if not isinstance(document, dict):
                raise RuntimeError(
                    str(committed.get("detail") or committed.get("error") or "Console delivery response is missing document")
                )
            document_platform = normalize_card_platform(
                document.get("platform") or platform
            )
            if document_platform != platform:
                raise RuntimeError("Console delivery document platform mismatch")
            with MANIFEST_LOCK:
                sync_manifest(manifest, load_manifest())
                card = manifest.setdefault("cards", {}).get(key)
                if not isinstance(card, dict) or card.get("status") == "void":
                    raise KeyError("卡密不存在或已作废")
                card["bundle_id"] = bundle_id
                card["order_id"] = str(committed.get("order_id") or card.get("order_id") or "")
                card["lease_id"] = str(committed.get("lease_id") or card.get("lease_id") or "")
                card["account_id"] = str(committed.get("account_id") or document.get("account_id") or "")
                card["provisioned_at"] = str(committed.get("consumed_at") or now_text())
                bundle_id, bundle = write_dynamic_bundle_json_atomic(manifest, card, document)
                card["status"] = "claimed"
                card["claimed_at"] = str(card.get("claimed_at") or now_text())
                card["last_error"] = ""
                save_manifest(manifest)
                return bundle_id, bundle
        except Exception as exc:
            with MANIFEST_LOCK:
                sync_manifest(manifest, load_manifest())
                card = manifest.setdefault("cards", {}).get(key)
                if isinstance(card, dict) and card.get("status") != "void":
                    card["status"] = "issued"
                    card["last_error"] = str(exc)[:1000]
                    card["last_failed_at"] = now_text()
                    save_manifest(manifest)
            raise


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def parse_time_text(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def bundle_claim_expires_at(bundle: dict) -> float | None:
    if not isinstance(bundle, dict):
        return None
    bound_ts = parse_time_text(str(bundle.get("bound_at") or ""))
    if bound_ts is None:
        return None
    return bound_ts + CLAIM_TTL_SECONDS


def bundle_is_expired(bundle: dict, *, now: float | None = None) -> bool:
    expires_at = bundle_claim_expires_at(bundle)
    if expires_at is None:
        return False
    return (time.time() if now is None else now) > expires_at


def bundle_expires_text(bundle: dict) -> str:
    expires_at = bundle_claim_expires_at(bundle)
    if expires_at is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))


def available_stock_bundle_ids(manifest: dict) -> list[str]:
    items: list[tuple[str, str]] = []
    for bundle_id, bundle in (manifest.get("bundles") or {}).items():
        if not isinstance(bundle, dict) or bundle.get("replaced_by"):
            continue
        if normalize_key(str(bundle.get("key") or "")):
            continue
        if bundle.get("bound_at"):
            continue
        zip_path = ZIP_DIR / f"{bundle_id}.zip"
        if not zip_path.exists():
            continue
        items.append((str(bundle.get("created_at") or ""), str(bundle_id)))
    return [bundle_id for _, bundle_id in sorted(items, key=lambda item: (item[0], item[1]))]


def assign_stock_bundle_to_key(manifest: dict, card_key: str) -> tuple[str, dict] | tuple[None, None]:
    key = normalize_key(card_key)
    if not key or key in (manifest.get("keys") or {}):
        return None, None
    stock_ids = available_stock_bundle_ids(manifest)
    if not stock_ids:
        return None, None
    bundle_id = stock_ids[0]
    bundle = manifest.setdefault("bundles", {}).get(bundle_id)
    if not isinstance(bundle, dict):
        return None, None
    bundle["key"] = key
    bundle["zip_name"] = f"{key}.zip"
    bundle["stock_assigned_at"] = now_text()
    manifest.setdefault("keys", {})[key] = bundle_id
    return bundle_id, bundle


def normalize_key(value: str) -> str:
    return "".join(str(value or "").strip().split()).upper()


def is_sensitive_request_path(path: str) -> bool:
    normalized = unquote(str(path or "")).replace("\\", "/").strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    lowered_parts = [part.lower() for part in normalized.split("/") if part]
    if any(part in SENSITIVE_PATH_PARTS for part in lowered_parts):
        return True
    if lowered_parts:
        filename = lowered_parts[-1]
        joined_path = "/".join(lowered_parts)
        if filename in SENSITIVE_FILE_NAMES:
            return True
        if any(keyword in joined_path for keyword in SENSITIVE_NAME_KEYWORDS):
            return True
        if filename.startswith(".env"):
            return True
        if filename.endswith(SENSITIVE_SUFFIXES):
            return True
    return False


def safe_filename(value: str, fallback: str = "data.json") -> str:
    raw = Path(str(value or fallback)).name
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in ".-_()[] ":
            keep.append(ch)
        else:
            keep.append("_")
    name = "".join(keep).strip(" .") or fallback
    if not name.lower().endswith(".json"):
        name += ".json"
    if len(name) <= 120:
        return name
    suffix = Path(name).suffix or ".json"
    stem = Path(name).stem or "data"
    return f"{stem[:max(1, 120 - len(suffix))]}{suffix}"


def deep_first_text(value, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key in keys:
            item = value.get(key)
            if item not in (None, "") and not isinstance(item, (dict, list, tuple, set)):
                text = str(item).strip()
                if text:
                    return text
        for item in value.values():
            found = deep_first_text(item, keys)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = deep_first_text(item, keys)
            if found:
                return found
    return ""


def delivery_identity_from_json(data, *, filename: str = "") -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    email = deep_first_text(
        data,
        {"email", "account_email", "login_identifier", "handle"},
    ).strip().lower()
    account_id = deep_first_text(
        data,
        {
            "account_id",
            "chatgpt_account_id",
            "accountId",
            "chatgptAccountId",
            "workspace_account_id",
            "resource_identifier",
        },
    ).strip().lower()
    workspace_id = deep_first_text(
        data,
        {"workspace_id", "workspaceId", "team_id", "teamId", "organization_id", "organizationId"},
    ).strip().lower()
    workspace_key = workspace_id or account_id
    key = "|".join(part for part in (email, workspace_key) if part)
    label_parts = []
    if email:
        label_parts.append(email)
    if account_id:
        label_parts.append(account_id)
    if workspace_id and workspace_id != account_id:
        label_parts.append(f"workspace={workspace_id}")
    return {
        "key": key,
        "email": email,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "filename": filename,
        "label": " / ".join(label_parts) or filename,
    }


def delivery_identity_from_raw(filename: str, raw: bytes) -> dict[str, str]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return {}
    return delivery_identity_from_json(data, filename=filename)


def unique_upload_filename(filename: str, used_names: set[str]) -> str:
    name = safe_filename(filename)
    marker = name.casefold()
    if marker not in used_names:
        used_names.add(marker)
        return name
    suffix = Path(name).suffix or ".json"
    stem = Path(name).stem or "data"
    for counter in range(2, 10000):
        tail = f"-{counter:02d}{suffix}"
        max_stem = max(1, 120 - len(tail))
        candidate = f"{stem[:max_stem]}{tail}"
        candidate_marker = candidate.casefold()
        if candidate_marker not in used_names:
            used_names.add(candidate_marker)
            return candidate
    fallback = f"data-{secrets.token_hex(4)}.json"
    used_names.add(fallback.casefold())
    return fallback


def active_uploaded_filename_map(manifest: dict) -> dict[str, dict[str, str]]:
    uploaded: dict[str, dict[str, str]] = {}
    for bundle_id, bundle in (manifest.get("bundles") or {}).items():
        if not isinstance(bundle, dict) or bundle.get("replaced_by"):
            continue
        title = str(bundle.get("title") or "").strip()
        key = str(bundle.get("key") or "").strip()
        for raw_name in bundle.get("files") or []:
            filename = safe_filename(str(raw_name or ""))
            marker = filename.casefold()
            if marker and marker not in uploaded:
                uploaded[marker] = {
                    "bundle_id": str(bundle_id),
                    "filename": filename,
                    "title": title,
                    "key": key,
                }
    return uploaded


def active_uploaded_identity_map(manifest: dict) -> dict[str, dict[str, str]]:
    uploaded: dict[str, dict[str, str]] = {}
    bundles = manifest.get("bundles") or {}
    for bundle_id, bundle in bundles.items():
        if not isinstance(bundle, dict) or bundle.get("replaced_by"):
            continue
        title = str(bundle.get("title") or "").strip()
        card_key = str(bundle.get("key") or "").strip()
        identities = [item for item in (bundle.get("identities") or []) if isinstance(item, dict)]
        if not identities:
            zip_path = bundle_zip_path(str(bundle_id))
            if zip_path and zip_path.exists():
                try:
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for name in zf.namelist():
                            if not name.lower().endswith(".json"):
                                continue
                            info = delivery_identity_from_raw(Path(name).name, zf.read(name))
                            if info.get("key"):
                                identities.append(info)
                except Exception:
                    identities = []
        for identity in identities:
            identity_key = str(identity.get("key") or "").strip().lower()
            if not identity_key or identity_key in uploaded:
                continue
            uploaded[identity_key] = {
                "bundle_id": str(bundle_id),
                "title": title,
                "key": card_key,
                "filename": str(identity.get("filename") or ""),
                "label": str(identity.get("label") or identity_key),
            }
    return uploaded


def existing_delivery_label(existing: dict | None) -> str:
    existing = existing or {}
    title = str(existing.get("title") or "").strip()
    key = str(existing.get("key") or "").strip()
    if title and key:
        return f"{title} / 卡密 {key}"
    if key:
        return f"卡密 {key}"
    return title or "历史记录"


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def existing_card_keys(manifest: dict) -> set[str]:
    keys = {
        normalize_key(str(key or ""))
        for key in (manifest.get("keys") or {}).keys()
        if normalize_key(str(key or ""))
    }
    for bundle in (manifest.get("bundles") or {}).values():
        if not isinstance(bundle, dict):
            continue
        key = normalize_key(str(bundle.get("key") or ""))
        if key:
            keys.add(key)
    for key in (manifest.get("cards") or {}).keys():
        normalized = normalize_key(str(key or ""))
        if normalized:
            keys.add(normalized)
    return keys


def is_card_key_available(manifest: dict, card_key: str) -> bool:
    key = normalize_key(card_key).upper()
    return bool(key) and key not in existing_card_keys(manifest)


def generate_card_key(length: int = CARD_KEY_DEFAULT_LENGTH) -> str:
    length = max(int(length or CARD_KEY_DEFAULT_LENGTH), 8)
    length += (-length) % CARD_KEY_GROUP_SIZE
    token = "".join(secrets.choice(CARD_KEY_ALPHABET) for _ in range(length))
    groups = [token[index : index + CARD_KEY_GROUP_SIZE] for index in range(0, len(token), CARD_KEY_GROUP_SIZE)]
    return "DG-" + "-".join(groups)


def generate_available_card_key(manifest: dict, reserved: set[str] | None = None) -> str:
    used = existing_card_keys(manifest)
    blocked = {normalize_key(key) for key in (reserved or set()) if normalize_key(key)}
    for length in CARD_KEY_LENGTHS:
        for _ in range(300):
            key = generate_card_key(length)
            if key not in used and key not in blocked:
                return key
    for _ in range(20):
        key = generate_card_key(CARD_KEY_LENGTHS[-1] + CARD_KEY_GROUP_SIZE)
        if key not in used and key not in blocked:
            return key
    raise RuntimeError("生成唯一卡密失败：随机空间连续撞重，请重试")


def generate_unique_card_key(manifest: dict) -> str:
    return generate_available_card_key(manifest)


def reserve_delivery_key(manifest: dict, reserved: set[str], preferred_key: str = "") -> str:
    preferred = normalize_key(preferred_key)
    if preferred:
        normalized = preferred.upper()
        if normalized in reserved or normalized in existing_card_keys(manifest):
            raise ValueError(f"卡密已存在：{preferred}，请换一个卡密")
        reserved.add(normalized)
        return preferred
    key = generate_available_card_key(manifest, reserved)
    reserved.add(key)
    return key


def issue_cards(
    manifest: dict,
    count: int,
    batch: str,
    platform: str = "grok",
    required_model: str = "",
) -> list[str]:
    count = int(count or 0)
    if not 1 <= count <= MAX_ISSUE_CARDS:
        raise ValueError(f"每次生成数量必须在 1 到 {MAX_ISSUE_CARDS} 之间")
    batch = str(batch or "").strip()[:80] or time.strftime("batch-%Y%m%d-%H%M%S", time.localtime())
    platform = normalize_card_platform(platform)
    required_model = normalize_card_required_model(platform, required_model)
    cards = manifest.setdefault("cards", {})
    issued: list[str] = []
    for _ in range(count):
        key = generate_unique_card_key(manifest)
        cards[key] = {
            "key": key,
            "status": "issued",
            "batch": batch,
            "platform": platform,
            "required_model": required_model,
            "created_at": now_text(),
            "bundle_id": "",
            "last_error": "",
        }
        issued.append(key)
    return issued


def parse_card_keys_input(value: str, *, limit: int = 5000) -> list[str]:
    """Extract unique card keys from lines, pasted links, or mixed separators."""
    text = str(value or "")
    candidates: list[str] = []
    for part in re.split(r"[\r\n,，;；\s]+", text):
        part = part.strip()
        if not part:
            continue
        parsed_key = ""
        try:
            parsed = urlparse(part)
            if parsed.scheme and parsed.netloc:
                parsed_key = str((parse_qs(parsed.query).get("key") or [""])[0]).strip()
        except Exception:
            parsed_key = ""
        matches = re.findall(r"DG-[A-Za-z0-9_-]+", part, flags=re.IGNORECASE)
        if parsed_key:
            candidates.append(parsed_key)
        elif matches:
            candidates.extend(matches)
        else:
            candidates.append(part)

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalize_key(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= max(1, int(limit or 1)):
            break
    return result


def batch_manage_cards(manifest: dict, card_keys: list[str], *, mode: str) -> dict:
    """Revoke cards or hide-delete unused cards while retaining audit tombstones."""
    mode = str(mode or "revoke").strip().lower()
    if mode not in {"revoke", "delete"}:
        raise ValueError("不支持的卡密操作")
    cards = manifest.setdefault("cards", {})
    keys = manifest.setdefault("keys", {})
    bundles = manifest.setdefault("bundles", {})
    result = {
        "requested": len(card_keys),
        "revoked": 0,
        "deleted": 0,
        "claimed_preserved": 0,
        "busy": 0,
        "missing": 0,
        "unchanged": 0,
    }
    changed = False
    timestamp = now_text()

    for raw_key in card_keys:
        key = normalize_key(raw_key)
        card = cards.get(key)
        if not isinstance(card, dict):
            result["missing"] += 1
            continue
        if str(card.get("status") or "") == "provisioning":
            result["busy"] += 1
            continue
        if bool(card.get("deleted")):
            result["unchanged"] += 1
            continue

        bundle_id = str(card.get("bundle_id") or keys.get(key) or "").strip()
        bundle = bundles.get(bundle_id) if isinstance(bundles.get(bundle_id), dict) else {}
        claimed = bool(
            str(card.get("status") or "") == "claimed"
            or card.get("claimed_at")
            or bundle.get("bound_at")
        )

        if mode == "delete" and not claimed:
            if bundle_id:
                delete_bundle_from_manifest(
                    manifest,
                    bundle_id,
                    delete_zip=True,
                    include_related=True,
                )
            keys.pop(key, None)
            card = cards.get(key) if isinstance(cards.get(key), dict) else card
            card.update(
                {
                    "status": "void",
                    "deleted": True,
                    "deleted_at": timestamp,
                    "voided_at": timestamp,
                    "bundle_id": "",
                    "last_error": "管理员批量删除卡密",
                }
            )
            result["deleted"] += 1
            changed = True
            continue

        already_void = str(card.get("status") or "") == "void"
        keys.pop(key, None)
        card.update(
            {
                "status": "void",
                "voided_at": str(card.get("voided_at") or timestamp),
                "last_error": "管理员批量销卡",
            }
        )
        if claimed and mode == "delete":
            card["delete_requested_at"] = timestamp
            result["claimed_preserved"] += 1
        elif already_void:
            result["unchanged"] += 1
        else:
            result["revoked"] += 1
        changed = True

    result["changed"] = changed
    return result


def page_shell(
    title: str,
    body: str,
    *,
    admin: bool = False,
    page_class: str = "",
    extra_style: str = "",
) -> bytes:
    nav = '<a href="/">取件页</a>'
    if admin:
        nav += f'<a href="{ADMIN_PATH}">管理后台</a><a href="{ADMIN_PATH}/logout">退出</a>'
    body_class = "admin-page" if admin else "user-page"
    if page_class:
        body_class += " " + page_class
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · DownloadGate</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    :root{{
      --ink:#1b1a18;--paper:#faf8f4;--accent:#bd4b1d;--muted:#706960;
      --surface:#f0ece6;--rule:#dfd8cf;--good:#14743d;--good-bg:rgba(34,197,94,.12);
      --warn:#a85716;--warn-bg:rgba(189,75,29,.1);--bad:#b42318;--bad-bg:rgba(180,35,24,.1)
    }}
    body{{min-height:100vh;background:radial-gradient(circle at top left,rgba(189,75,29,.08),transparent 34vw),linear-gradient(180deg,#fffdfa 0%,var(--paper) 58%,#f3eee7 100%);color:var(--ink);font-family:Inter,"PingFang SC","Microsoft YaHei",Arial,sans-serif;-webkit-font-smoothing:antialiased}}
    .wrap{{width:min(860px,100%);margin:0 auto;padding:52px 22px 54px}}
    body.admin-page .wrap{{width:min(1160px,100%);padding-top:28px}}
    body.user-page .wrap{{width:min(820px,100%)}}
    header{{margin-bottom:30px}}
    body.admin-page header{{display:grid;grid-template-columns:1fr auto;align-items:start;gap:12px;margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid var(--rule)}}
    body.admin-page header .wordmark{{font-size:1.18rem;grid-column:1}}
    body.admin-page header nav{{grid-column:2;grid-row:1;justify-content:flex-end;margin-top:2px}}
    body.admin-page header .eyebrow{{grid-column:1 / -1;margin-top:8px}}
    body.admin-page header h1{{grid-column:1 / -1;margin-top:2px;font-size:clamp(1.55rem,3vw,2rem)}}
    body.admin-page .lead{{max-width:760px;margin-top:10px;font-size:.88rem;line-height:1.58}}
    body.login-page{{background:linear-gradient(180deg,#fffdfa 0%,var(--paper) 100%)}}
    body.login-page .wrap{{width:min(460px,100%);min-height:100vh;display:flex;flex-direction:column;justify-content:center;padding:34px 20px}}
    body.login-page header{{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:18px}}
    body.login-page .wordmark{{font-size:1.25rem}}
    body.login-page nav{{margin-top:0}}
    body.login-page .eyebrow,body.login-page h1{{display:none}}
    body.login-page footer{{margin-top:32px}}
    body.user-page header{{margin-bottom:18px}}
    body.user-page h1{{font-size:clamp(1.85rem,5vw,2.45rem)}}
    body.pickup-page .wrap{{width:min(780px,100%);padding-top:46px}}
    body.pickup-page header{{margin-bottom:12px}}
    body.pickup-page h1{{font-size:clamp(2.1rem,6vw,2.9rem)}}
    .wordmark{{display:flex;align-items:center;gap:10px;font-family:Georgia,"Times New Roman",serif;font-size:1.5rem;font-weight:600}}
    .logo{{width:20px;height:20px;border-radius:50%;background:var(--accent);box-shadow:inset 0 0 0 5px rgba(255,255,255,.34)}}
    .wordmark span{{color:var(--accent)}}
    nav{{display:flex;gap:14px;flex-wrap:wrap;margin-top:16px}}
    nav a{{color:var(--accent);text-decoration:none;font-size:.86rem}}
    .eyebrow{{margin-top:18px;color:var(--muted);font-size:.72rem;letter-spacing:.14em;text-transform:uppercase}}
    h1{{margin-top:8px;font-family:Georgia,"Times New Roman",serif;font-size:clamp(2rem,6vw,2.75rem);font-weight:600;line-height:1.14}}
    h1 em{{color:var(--accent);font-style:italic}}
    .lead{{max-width:640px;margin-top:15px;color:var(--muted);font-size:.96rem;line-height:1.72}}
    .panel,.card{{border:1px solid var(--rule);background:#fff;border-radius:8px}}
    .panel{{padding:18px;margin-top:22px}}
    .panel-title{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;font-weight:800;color:var(--ink)}}
    .panel-title small{{font-size:.76rem;font-weight:500;color:var(--muted)}}
    label{{display:grid;gap:8px;margin-bottom:14px;color:#39342f;font-size:.9rem;font-weight:600}}
    input,textarea,select{{width:100%;min-width:0;border:1px solid var(--rule);border-radius:6px;background:#fff;color:var(--ink);font:inherit;padding:13px 14px;outline:none}}
    input:focus,textarea:focus,select:focus{{border-color:var(--accent)}}
    input[type=file]{{padding:11px;background:#fff}}
    .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
    .radio-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:0 0 14px}}
    .radio-card{{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--rule);border-radius:8px;background:#fff;padding:12px 13px;font-weight:600;cursor:pointer}}
    .radio-card input{{width:auto;margin-top:3px;accent-color:var(--accent)}}
    .radio-card span{{display:grid;gap:4px}}
    .radio-card small{{color:var(--muted);font-weight:500;line-height:1.45}}
    .pickup-lead{{max-width:690px;margin-top:0;color:var(--muted);font-size:.96rem;line-height:1.72;overflow-wrap:anywhere}}
    .announcement-card{{border:1px solid rgba(189,75,29,.24);border-left:3px solid var(--accent);background:#fff7ef;border-radius:8px;margin-top:16px;padding:14px 16px;color:var(--ink)}}
    .announcement-card strong{{display:block;margin-bottom:6px;font-size:.93rem}}
    .announcement-body{{color:#57483b;font-size:.86rem;line-height:1.68;overflow-wrap:anywhere}}
    .lookup-card{{position:relative;border:1px solid var(--rule);border-radius:8px;background:#fff;margin-top:22px;padding:22px;box-shadow:0 18px 44px rgba(46,35,24,.08);overflow:hidden}}
    .lookup-card::before{{content:"";position:absolute;left:0;right:0;top:0;height:3px;background:var(--accent)}}
    .lookup-title{{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px;font-weight:800;min-width:0}}
    .lookup-title small{{color:var(--muted);font-size:.76rem;font-weight:500}}
    .lookup-heading{{display:grid;gap:4px}}
    .lookup-heading strong{{font-size:1.05rem}}
    .lookup-heading span{{color:var(--muted);font-size:.82rem;font-weight:500;line-height:1.5}}
    .key-entry{{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:10px;align-items:stretch;min-width:0}}
    .key-entry textarea{{min-height:58px;resize:vertical;font-weight:700;letter-spacing:.01em}}
    .hint-line{{margin-top:12px;color:var(--muted);font-size:.8rem;line-height:1.5}}
    .pool-closed-alert{{margin:12px 0 10px;border:1px solid rgba(189,75,29,.34);border-left:3px solid var(--accent);background:#fff1e8;border-radius:7px;padding:11px 12px;color:#7a2f0d;font-size:.84rem;line-height:1.55;font-weight:700}}
    .pool-closed-alert span{{display:block;color:#7a4a32;font-size:.78rem;font-weight:500;margin-top:4px}}
    body.modal-open{{overflow:hidden}}
    .pool-modal{{position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(31,24,18,.42)}}
    .pool-modal[hidden]{{display:none}}
    .pool-modal-card{{width:min(440px,calc(100vw - 40px));border:1px solid rgba(189,75,29,.3);border-radius:8px;background:#fffaf5;box-shadow:0 24px 70px rgba(31,24,18,.24);padding:20px;color:var(--ink)}}
    .pool-modal-card strong{{display:block;font-size:1.08rem;margin-bottom:8px}}
    .pool-modal-card p{{margin:0;color:#6b4a38;font-size:.9rem;line-height:1.68}}
    .pool-modal-actions{{display:flex;justify-content:flex-end;margin-top:16px}}
    .pool-modal-actions button{{min-height:38px}}
    .batch-hint{{margin-top:10px;border:1px solid #f0ebe4;background:#fffaf5;border-radius:6px;padding:9px 11px;color:var(--muted);font-size:.8rem;line-height:1.5}}
    .batch-hint strong{{color:var(--accent)}}
    .batch-hint.is-batch{{border-color:rgba(189,75,29,.28);background:#fff7ef;color:#7a3a15}}
    .lookup-footnote{{margin-top:13px;padding-top:13px;border-top:1px solid #f0ebe4;color:var(--muted);font-size:.8rem;line-height:1.55;overflow-wrap:anywhere}}
    .service-strip{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}}
    .service-strip div{{border:1px solid var(--rule);background:#fff;padding:12px 13px;color:var(--muted);font-size:.8rem;line-height:1.45}}
    .service-strip b{{display:block;color:var(--ink);font-size:.86rem;margin-bottom:4px}}
    .login-panel{{width:100%;padding:24px;margin-top:0;box-shadow:0 18px 44px rgba(46,35,24,.08)}}
    .login-card-head{{display:grid;gap:7px;margin-bottom:18px}}
    .login-card-head strong{{font-family:Georgia,"Times New Roman",serif;font-size:1.55rem;font-weight:600;line-height:1.2}}
    .login-card-head span{{color:var(--muted);font-size:.86rem;line-height:1.58}}
    .login-version{{display:inline-flex;width:max-content;border:1px solid var(--rule);border-radius:999px;padding:4px 9px;color:var(--muted);font-size:.72rem;background:#fff}}
    .login-copy{{margin:-2px 0 18px;color:var(--muted);font-size:.86rem;line-height:1.62}}
    .password-row{{display:grid;grid-template-columns:minmax(0,1fr) 46px;gap:8px;align-items:stretch;min-width:0}}
    .password-row input{{min-height:48px}}
    .password-row button{{padding:0;min-width:0}}
    .login-foot{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:14px;color:var(--muted);font-size:.78rem}}
    .login-foot a{{color:var(--accent);text-decoration:none}}
    .btn,button{{border:0;border-radius:6px;background:var(--ink);color:var(--paper);cursor:pointer;font:inherit;font-weight:600;min-height:46px;padding:0 18px;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}}
    .btn:hover,button:hover{{background:var(--accent)}}
    .btn:disabled,button:disabled{{opacity:.55;cursor:not-allowed}}
    .btn.full,button.full{{width:100%}}
    .btn.secondary,button.secondary{{background:#fff;color:var(--ink);border:1px solid var(--rule)}}
    .btn.secondary:hover,button.secondary:hover{{background:#fff;color:var(--accent);border-color:var(--accent)}}
    .btn.compact,button.compact{{min-height:34px;padding:0 10px;font-size:.76rem;white-space:nowrap}}
    .btn.danger,button.danger{{background:#fff;color:var(--bad);border:1px solid rgba(180,35,24,.36)}}
    .btn.danger:hover,button.danger:hover{{background:var(--bad);color:#fff;border-color:var(--bad)}}
    .note{{margin-top:16px;padding:13px 15px;border-left:2px solid;font-size:.88rem;line-height:1.62;overflow-wrap:anywhere}}
    .note.ok{{border-color:var(--good);background:var(--good-bg);color:var(--good)}}
    .note.warn{{border-color:var(--accent);background:var(--warn-bg);color:var(--warn)}}
    .note.err{{border-color:var(--bad);background:var(--bad-bg);color:var(--bad)}}
    .note-title{{font-weight:800;color:inherit}}
    .note details{{margin-top:7px}}
    .note summary{{cursor:pointer;font-weight:700;user-select:none}}
    .skip-list{{display:grid;gap:5px;max-height:170px;overflow:auto;margin-top:8px;padding-left:18px}}
    .skip-list li{{padding-right:6px;overflow-wrap:anywhere;word-break:break-word}}
    .card{{margin-top:15px}}
    .card .hd{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 18px;border-bottom:1px solid var(--rule)}}
    .result{{border-color:rgba(20,116,61,.35);background:#fbfffc}}
    .key{{color:var(--muted);font-size:.82rem;overflow-wrap:anywhere}}
    .key b{{color:var(--ink);font-family:Georgia,"Times New Roman",serif;font-size:1.06rem}}
    .bigkey{{margin-top:3px;color:var(--ink);font-family:Georgia,"Times New Roman",serif;font-size:1.35rem;overflow-wrap:anywhere}}
    .result-list{{display:grid;gap:8px}}
    .result-row{{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid #edf2ed;background:#fff;border-radius:7px;padding:9px 10px}}
    .result-key{{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.82rem;color:var(--ink);line-height:1.38;overflow-wrap:anywhere;word-break:break-all}}
    .result-info{{display:grid;gap:4px;min-width:0;font-size:.82rem;line-height:1.45;overflow-wrap:anywhere}}
    .result-title{{font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .result-meta{{color:var(--muted);font-size:.76rem}}
    .result-link{{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;color:var(--accent);font-size:.74rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .result-actions{{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}}
    .result-actions .btn,.result-actions button{{min-height:28px;padding:0 7px;font-size:.7rem;border-radius:5px}}
    .result-actions .download-mini{{color:var(--muted)}}
    .result-actions .download-mini:hover{{color:var(--accent)}}
    .mini{{display:block;margin-bottom:5px;color:var(--muted);font-size:.73rem;letter-spacing:.02em}}
    .tag{{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;padding:4px 10px;border-radius:999px;font-size:.72rem;font-weight:700;color:var(--good);background:var(--good-bg)}}
    .tag::before{{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}}
    .tag.replaced{{color:var(--warn);background:var(--warn-bg)}}
    .tag.expired{{color:var(--bad);background:var(--bad-bg)}}
    .tag.card-status.claimed{{color:var(--good);background:var(--good-bg)}}
    .tag.card-status.provisioning{{color:var(--warn);background:var(--warn-bg)}}
    .tag.card-status.retryable{{color:var(--warn);background:var(--warn-bg)}}
    .tag.card-status.issued{{color:#2563a8;background:rgba(37,99,168,.10)}}
    .tag.card-status.failed{{color:var(--bad);background:var(--bad-bg)}}
    .tag.card-status.void{{color:var(--bad);background:var(--bad-bg)}}
    .rows{{padding:8px 18px 4px}}
    .row{{display:flex;gap:14px;padding:8px 0;border-bottom:1px solid #f3f0eb;font-size:.86rem}}
    .row:last-child{{border-bottom:0}}
    .row .k{{width:82px;flex:none;color:var(--muted)}}
    .row .v{{color:var(--ink);overflow-wrap:anywhere}}
    .downloads{{display:grid;gap:9px;padding:0 18px 18px}}
    .downloads a,.copybox{{display:block;border:1px solid var(--rule);border-radius:6px;padding:12px 14px;color:var(--accent);text-decoration:none;overflow-wrap:anywhere;font-size:.82rem;background:#fff}}
    .downloads .result-actions a.btn,.downloads .claim-actions a.btn,.downloads .download-actions a.btn{{display:inline-flex;align-items:center;justify-content:center;line-height:1}}
    .downloads .result-actions a.btn{{padding:0 7px;font-size:.7rem;min-height:28px}}
    .downloads .claim-actions a.btn{{padding:0 8px;font-size:.72rem;min-height:30px}}
    .downloads .download-actions a.btn{{padding:0 18px;min-height:40px}}
    .download-actions{{display:flex;gap:8px;flex-wrap:wrap}}
    .download-actions .btn,.download-actions button{{min-height:40px}}
    .stack{{display:grid;gap:12px}}
    .copyrow{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}}
    .copyrow input{{font-size:.82rem;color:var(--accent);background:#fff}}
    .upload-panel{{padding:12px 14px;margin-top:14px;scroll-margin-top:18px}}
    .upload-panel .panel-title{{margin-bottom:10px;font-size:.94rem}}
    .upload-panel .panel-title small{{font-size:.7rem}}
    .upload-panel .radio-grid{{gap:8px;margin-bottom:10px}}
    .upload-panel .radio-card{{padding:9px 11px;gap:8px;border-radius:6px;font-size:.82rem}}
    .upload-panel .radio-card input{{margin-top:1px}}
    .upload-panel .radio-card span{{gap:2px}}
    .upload-panel .radio-card small{{font-size:.72rem;line-height:1.35}}
    .upload-panel .grid{{gap:10px}}
    .upload-panel label{{gap:6px;margin-bottom:10px;font-size:.8rem}}
    .upload-panel input{{min-height:36px;padding:8px 10px;font-size:.8rem}}
    .upload-panel input[type=file]{{padding:8px 10px}}
    .upload-panel .copyrow{{gap:6px;grid-template-columns:minmax(0,1fr) 92px}}
    .upload-panel .copyrow button{{min-height:36px;padding:0 10px;font-size:.78rem;white-space:nowrap}}
    .upload-panel button.full{{min-height:42px;font-size:.92rem}}
    .announcement-panel{{padding:12px 14px;margin-top:14px}}
    .announcement-panel textarea{{min-height:88px;resize:vertical;font-size:.84rem;line-height:1.55}}
    .check-row{{display:flex;align-items:center;gap:9px;margin:0 0 10px;font-size:.82rem;font-weight:700}}
    .check-row input{{width:16px;height:16px;accent-color:var(--accent)}}
    .replenish-panel{{display:grid;gap:18px}}
    .replenish-toggle-row{{display:flex;align-items:center;justify-content:space-between;gap:20px;padding-bottom:16px;border-bottom:1px solid var(--rule)}}
    .replenish-toggle-copy{{display:grid;gap:5px;min-width:0}}
    .replenish-toggle-copy strong{{font-size:.94rem}}
    .replenish-toggle-copy span{{color:var(--muted);font-size:.78rem;line-height:1.55}}
    .switch-control{{position:relative;display:inline-flex;flex:none;width:46px;height:26px;cursor:pointer}}
    .switch-control input{{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}}
    .switch-track{{position:absolute;inset:0;border:1px solid #b8b0a6;border-radius:999px;background:#d9d4cd;transition:background .16s,border-color .16s}}
    .switch-track::after{{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 4px rgba(27,26,24,.24);transition:transform .16s}}
    .switch-control input:checked + .switch-track{{border-color:var(--good);background:var(--good)}}
    .switch-control input:checked + .switch-track::after{{transform:translateX(20px)}}
    .switch-control input:focus-visible + .switch-track{{outline:3px solid rgba(37,99,235,.2);outline-offset:2px}}
    .switch-control input:disabled + .switch-track{{opacity:.48;cursor:not-allowed}}
    .inventory-metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}}
    .inventory-metric{{min-width:0;border:1px solid var(--rule);background:#fff;padding:13px 14px}}
    .inventory-metric span{{display:block;margin-bottom:6px;color:var(--muted);font-size:.73rem}}
    .inventory-metric strong{{display:block;color:var(--ink);font-size:1rem;line-height:1.35;overflow-wrap:anywhere}}
    .inventory-metric strong.good{{color:var(--good)}}
    .inventory-metric strong.warn{{color:var(--warn)}}
    .replenish-state{{padding:11px 13px;border-left:3px solid var(--rule);background:#faf9f7;color:var(--muted);font-size:.8rem;line-height:1.58}}
    .replenish-state.active{{border-color:var(--good);background:var(--good-bg);color:var(--good)}}
    .replenish-state.error{{border-color:var(--bad);background:var(--bad-bg);color:var(--bad)}}
    .admin-summary{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin-top:18px}}
    .stat{{border:1px solid var(--rule);background:#fff;padding:13px 14px}}
    .stat .k{{display:block;color:var(--muted);font-size:.75rem;margin-bottom:7px}}
    .stat .v{{font-family:Georgia,"Times New Roman",serif;font-size:1.35rem;color:var(--ink)}}
    .toolbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:16px}}
    .toolbar .admin-actions{{margin-top:0}}
    .admin-tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 12px;border-bottom:1px solid var(--rule)}}
    .admin-tab{{min-height:36px;border-radius:6px 6px 0 0;background:transparent;color:var(--muted);border:1px solid transparent;border-bottom:0;padding:0 14px;font-size:.82rem}}
    .admin-tab.active,.admin-tab:hover{{background:#fff;color:var(--ink);border-color:var(--rule)}}
    .admin-tab-panel[hidden]{{display:none}}
    .accounts-toolbar{{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-top:16px}}
    .accounts-toolbar label{{display:grid;gap:5px;margin:0;color:var(--muted);font-size:.74rem}}
    .accounts-toolbar select{{min-width:150px;min-height:38px;border:1px solid var(--rule);border-radius:6px;background:#fff;color:var(--ink);padding:8px 10px;font-size:.82rem}}
    .accounts-toolbar .model-input{{width:180px;min-height:38px;padding:8px 10px;font-size:.82rem}}
    .accounts-toolbar .pagination{{margin-left:auto}}
    .accounts-feedback{{margin-top:12px}}
    .accounts-feedback:empty{{display:none}}
    .accounts-table table{{min-width:1280px}}
    .account-email{{display:block;color:var(--ink);font-weight:700;word-break:break-all}}
    .account-id{{display:block;margin-top:5px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.74rem}}
    .account-badge{{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:4px 9px;font-size:.72rem;font-weight:700;white-space:nowrap}}
    .account-badge::before{{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}}
    .account-badge.ready,.account-badge.alive{{color:var(--good);background:var(--good-bg)}}
    .account-badge.unverified,.account-badge.leased{{color:var(--warn);background:var(--warn-bg)}}
    .account-badge.invalid,.account-badge.dead{{color:var(--bad);background:var(--bad-bg)}}
    .account-badge.delivered{{color:#2563a8;background:rgba(37,99,168,.10)}}
    .account-badge.neutral{{color:var(--muted);background:#f1efeb}}
    .account-detail{{display:block;margin-top:6px;color:var(--muted);font-size:.74rem;line-height:1.45;overflow-wrap:anywhere}}
    .account-error{{max-width:340px;color:var(--bad);font-size:.76rem;line-height:1.45;overflow-wrap:anywhere}}
    .account-error.none{{color:var(--muted)}}
    .model-test-cell{{display:grid;gap:7px;min-width:180px}}
    .model-test-cell button{{justify-self:start;min-height:30px;padding:0 10px;font-size:.72rem}}
    .account-migration-panel{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;padding:12px 14px;border:1px solid var(--rule);background:#fff}}
    .account-migration-copy{{display:grid;gap:3px;min-width:220px;margin-right:auto}}
    .account-migration-copy strong{{font-size:.84rem}}
    .account-migration-copy span{{color:var(--muted);font-size:.72rem;line-height:1.4}}
    .account-migration-panel input[type=file]{{max-width:290px;font-size:.76rem}}
    .table-tools{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:16px}}
    .search-input{{width:min(360px,100%);min-height:38px;padding:9px 11px;font-size:.84rem}}
    .segmented{{display:inline-flex;border:1px solid var(--rule);border-radius:6px;background:#fff;overflow:hidden}}
    .segmented button{{min-height:36px;background:#fff;color:var(--ink);border-right:1px solid var(--rule);padding:0 12px;font-size:.78rem}}
    .segmented button:last-child{{border-right:0}}
    .segmented button.active,.segmented button:hover{{background:var(--ink);color:var(--paper)}}
    .table-count{{color:var(--muted);font-size:.8rem}}
    .pagination{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto}}
    .pagination select{{min-height:34px;border-radius:6px;border:1px solid var(--rule);background:#fff;color:var(--ink);padding:0 8px;font-size:.78rem}}
    tr.is-hidden,tr.is-page-hidden,.claim-row.is-hidden,.claim-row.is-page-hidden{{display:none}}
    tr.is-selected{{background:#fffdf7}}
    .admin-table{{margin-top:18px;overflow-x:auto}}
    table{{width:100%;border-collapse:collapse;margin-top:14px;background:#fff;border:1px solid var(--rule)}}
    .admin-table table{{min-width:1160px;margin-top:0;table-layout:fixed}}
    .admin-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}}
    .bulk-actions{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0}}
    .bulk-actions button:disabled{{opacity:.48;cursor:not-allowed}}
    .inline-form{{display:inline-flex;margin:0}}
    th,td{{border-bottom:1px solid var(--rule);padding:10px;text-align:left;font-size:.84rem;vertical-align:top}}
    th{{color:var(--muted);font-weight:600;background:#faf7f2}}
    .select-head,.select-cell{{width:44px;text-align:center;vertical-align:middle}}
    .select-head input,.select-cell input{{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}}
    td a{{color:var(--accent);text-decoration:none;overflow-wrap:anywhere}}
    .item-title{{font-weight:700;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .item-key{{display:inline-block;margin-top:5px;color:var(--ink);font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.78rem;line-height:1.35;overflow-wrap:anywhere;word-break:break-all;white-space:normal}}
    .item-meta{{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}}
    .file-cell{{display:grid;gap:5px;color:var(--muted);font-size:.78rem;line-height:1.45}}
    .file-cell b{{color:var(--ink);font-size:.9rem}}
    .file-preview{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}}
    .status-time{{display:block;margin-top:7px;color:var(--muted);font-size:.78rem;line-height:1.35}}
    .action-grid{{display:flex;gap:7px;flex-wrap:wrap;align-items:center}}
    .action-grid .btn,.action-grid button{{min-height:32px;padding:0 10px;font-size:.76rem}}
    .action-grid .inline-form{{display:inline-flex}}
    .address-list{{display:grid;gap:9px}}
    .address-actions{{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}}
    .claim-list{{display:grid;gap:8px;margin-top:14px}}
    .claim-tools{{margin-top:4px}}
    .claim-row{{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr) auto;gap:10px;align-items:center;border:1px solid var(--rule);background:#fff;border-radius:7px;padding:10px}}
    .claim-key{{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.82rem;color:var(--ink);word-break:break-all}}
    .claim-info{{display:grid;gap:4px;min-width:0;font-size:.82rem}}
    .claim-title{{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .claim-meta{{color:var(--muted);font-size:.76rem;line-height:1.45}}
    .claim-actions{{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}}
    .claim-actions .btn,.claim-actions button{{min-height:30px;padding:0 8px;font-size:.72rem}}
    footer{{display:flex;justify-content:space-between;gap:14px;margin-top:42px;padding-top:20px;border-top:1px solid var(--rule);color:var(--muted);font-size:.74rem}}
    .online{{display:inline-flex;align-items:center;gap:7px}}
    .online::before{{content:"";width:6px;height:6px;border-radius:50%;background:#22c55e}}
    @media(max-width:820px){{.admin-summary{{grid-template-columns:repeat(2,minmax(0,1fr))}}.key-entry{{grid-template-columns:1fr}}.service-strip{{grid-template-columns:1fr}}}}
    @media(max-width:640px){{body{{overflow-x:hidden}}body.admin-page header{{display:block}}body.admin-page header nav{{justify-content:flex-start;margin-top:12px}}body.login-page .wrap{{padding-inline:16px}}body.login-page header{{display:grid;gap:10px}}body.pickup-page .wrap{{padding-top:36px}}.pickup-lead,.lookup-card,.announcement-card,.login-panel{{width:100%;max-width:calc(100vw - 36px)}}.lookup-card,.login-panel{{padding:20px 18px}}.lookup-title{{display:grid;grid-template-columns:1fr;gap:6px}}.lookup-title small{{justify-self:start}}.pickup-lead{{font-size:.92rem}}.login-foot{{display:grid;gap:8px}}.grid,.radio-grid{{grid-template-columns:1fr}}.copyrow{{grid-template-columns:1fr}}.result-row,.claim-row{{grid-template-columns:1fr}}.result-actions,.claim-actions{{justify-content:flex-start}}.wrap{{padding-inline:18px}}footer{{flex-direction:column}}.admin-summary{{grid-template-columns:1fr}}.segmented{{width:100%;display:grid;grid-template-columns:repeat(2,1fr)}}.search-input{{width:100%}}}}
    @media(max-width:640px){{.replenish-toggle-row{{align-items:flex-start}}.inventory-metrics{{grid-template-columns:1fr}}}}
  </style>
  {extra_style}
</head>
<body class="{body_class}">
  <div class="wrap">
    <header>
      <div class="wordmark"><span class="logo"></span>Download<span>Gate</span></div>
      <nav>{nav}</nav>
      <div class="eyebrow">Self-service · JSON 打包 / 卡密取件</div>
      <h1>{html.escape(title)}</h1>
    </header>
    {body}
    <script>
    async function copyText(value, btn){{
      const oldText = btn ? btn.textContent : "";
      try{{
        if(navigator.clipboard && window.isSecureContext){{
          await navigator.clipboard.writeText(value);
        }}else{{
          const temp = document.createElement('textarea');
          temp.value = value;
          temp.style.position = 'fixed';
          temp.style.left = '-9999px';
          document.body.appendChild(temp);
          temp.focus();
          temp.select();
          document.execCommand('copy');
          temp.remove();
        }}
        if(btn){{btn.textContent='已复制';setTimeout(()=>btn.textContent=oldText,1200);}}
      }}catch(e){{
        if(btn){{btn.textContent='复制失败';setTimeout(()=>btn.textContent=oldText,1400);}}
      }}
    }}
    function copyValue(input, btn){{
      if(input && input.select) input.select();
      copyText(input ? input.value : "", btn);
    }}
    </script>
    <footer><div class="online">系统在线</div><div>DownloadGate v{APP_VERSION}</div></footer>
  </div>
</body>
</html>"""
    return html_text.encode("utf-8")


def user_page(message: str = "", *, initial_key: str = "") -> bytes:
    note = f'<div class="note warn">{html.escape(message)}</div>' if message else ""
    initial_value = html.escape(initial_key)
    announcement = load_announcement()
    pool_closed = bool(announcement.get("pool_closed"))
    default_key_hint = "当前号池为空，未激活卡密暂不可取件；已取件用户可在有效期内继续下载。" if pool_closed else "单卡可分别下载 CPA、Sub2API、Cockpit 或 GrokCLI-2API；多卡会按四种格式分目录合并下载。"
    pool_closed_js = "true" if pool_closed else "false"
    pool_closed_message_js = json.dumps(pool_closed_message(), ensure_ascii=False)
    pool_closed_notice_html = (
        '<div class="pool-closed-alert">号池为空模式已开启：未激活卡密不能取件。'
        '<span>已经取件的卡密可在 24 小时有效期内继续下载；点击核验后系统会判断该卡密是否已激活。</span></div>'
        if pool_closed
        else ""
    )
    pool_closed_modal_html = (
        '<div id="poolClosedModal" class="pool-modal" role="dialog" aria-modal="true" aria-labelledby="poolClosedTitle" hidden>'
        '<div class="pool-modal-card">'
        '<strong id="poolClosedTitle">无法取件</strong>'
        '<p id="poolClosedModalText">当前号池为空，未激活卡密暂不可取件。已取件用户可在有效期内继续下载。</p>'
        '<div class="pool-modal-actions"><button type="button" onclick="closePoolClosedModal()">知道了</button></div>'
        '</div></div>'
        if pool_closed
        else ""
    )
    announcement_html = ""
    if announcement.get("enabled") and announcement.get("content"):
        announcement_html = f"""
<section class="announcement-card" aria-label="取件公告">
  <strong>{html.escape(str(announcement.get("title") or "公告"))}</strong>
  <div class="announcement-body">{text_lines_html(str(announcement.get("content") or ""))}</div>
</section>"""
    pickup_styles = """<style>
    body.pickup-page {
      --ink: #172033;
      --paper: #f5f7fb;
      --accent: #2563eb;
      --muted: #667085;
      --surface: #eef2f7;
      --rule: #d9e1ec;
      --good: #16794d;
      --good-bg: #ecf8f1;
      --warn: #9a6700;
      --warn-bg: #fff8e6;
      --bad: #b42318;
      --bad-bg: #fff1f0;
      background: #f5f7fb;
      color: var(--ink);
    }
    body.pickup-page .wrap {
      width: min(1120px, 100%);
      padding: 0 24px 32px;
    }
    body.pickup-page header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 8px 24px;
      margin: 0;
      padding: 22px 0 30px;
      border-bottom: 1px solid var(--rule);
    }
    body.pickup-page header .wordmark {
      gap: 9px;
      font-family: Inter, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      font-size: 1.08rem;
      font-weight: 800;
    }
    body.pickup-page header .logo {
      width: 24px;
      height: 24px;
      border-radius: 7px;
      background: var(--accent);
      box-shadow: inset 0 0 0 6px rgba(255, 255, 255, .22);
    }
    body.pickup-page header .wordmark span {
      color: var(--accent);
    }
    body.pickup-page header nav {
      justify-content: flex-end;
      margin: 0;
    }
    body.pickup-page header nav a {
      color: #475467;
      font-size: .82rem;
      font-weight: 700;
    }
    body.pickup-page header .eyebrow {
      grid-column: 1 / -1;
      margin-top: 42px;
      color: var(--accent);
      font-size: .72rem;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    body.pickup-page header h1 {
      grid-column: 1 / -1;
      margin-top: 2px;
      font-family: Inter, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      font-size: 2.15rem;
      font-weight: 800;
      line-height: 1.25;
      letter-spacing: 0;
    }
    body.pickup-page .pickup-intro {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 28px;
      padding: 16px 0 20px;
    }
    body.pickup-page .pickup-lead {
      max-width: 720px;
      margin: 0;
      color: #475467;
      font-size: .94rem;
      line-height: 1.75;
    }
    body.pickup-page .pickup-version {
      flex: none;
      padding-top: 3px;
      color: #98a2b3;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: .74rem;
    }
    body.pickup-page .pickup-facts {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-top: 1px solid var(--rule);
      border-bottom: 1px solid var(--rule);
    }
    body.pickup-page .pickup-fact {
      min-width: 0;
      padding: 15px 18px;
      border-right: 1px solid var(--rule);
    }
    body.pickup-page .pickup-fact:first-child {
      padding-left: 0;
    }
    body.pickup-page .pickup-fact:last-child {
      border-right: 0;
    }
    body.pickup-page .pickup-fact span {
      display: block;
      margin-bottom: 4px;
      color: #98a2b3;
      font-size: .72rem;
    }
    body.pickup-page .pickup-fact strong {
      display: block;
      color: #344054;
      font-size: .86rem;
      line-height: 1.45;
    }
    body.pickup-page .live-pool {
      display: grid;
      grid-template-columns: minmax(180px, .65fr) minmax(0, 1.35fr);
      gap: 22px;
      align-items: center;
      margin-top: 18px;
      padding: 16px 18px;
      border: 1px solid var(--rule);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 6px 18px rgba(16, 24, 40, .035);
    }
    body.pickup-page .live-pool-main {
      display: grid;
      gap: 3px;
    }
    body.pickup-page .live-pool-main span,
    body.pickup-page .live-pool-meta {
      color: #7a8699;
      font-size: .74rem;
      line-height: 1.5;
    }
    body.pickup-page .live-pool-main strong {
      color: var(--good);
      font-size: 1.35rem;
      line-height: 1.25;
    }
    body.pickup-page .live-pool-detail {
      display: grid;
      gap: 8px;
    }
    body.pickup-page .live-pool-heading {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      color: #344054;
      font-size: .8rem;
      font-weight: 700;
    }
    body.pickup-page .live-pool-track {
      height: 7px;
      overflow: hidden;
      border-radius: 999px;
      background: #e8edf4;
    }
    body.pickup-page .live-pool-track span {
      display: block;
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: var(--good);
      transition: width .2s ease;
    }
    body.pickup-page .announcement-card {
      margin: 20px 0 0;
      padding: 14px 16px;
      border: 1px solid #bfdbfe;
      border-left: 3px solid var(--accent);
      border-radius: 7px;
      background: #eff6ff;
      color: var(--ink);
    }
    body.pickup-page .announcement-card strong {
      margin-bottom: 5px;
      font-size: .86rem;
    }
    body.pickup-page .announcement-body {
      color: #475467;
      font-size: .82rem;
      line-height: 1.65;
    }
    body.pickup-page .pickup-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.72fr) minmax(270px, .78fr);
      gap: 32px;
      align-items: start;
      margin-top: 24px;
    }
    body.pickup-page .lookup-card {
      position: relative;
      margin: 0;
      padding: 28px;
      overflow: visible;
      border: 1px solid var(--rule);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 10px 28px rgba(16, 24, 40, .06);
    }
    body.pickup-page .lookup-card::before {
      display: none;
    }
    body.pickup-page .lookup-title {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 22px;
    }
    body.pickup-page .lookup-heading {
      display: grid;
      gap: 6px;
    }
    body.pickup-page .lookup-heading strong {
      color: var(--ink);
      font-size: 1.18rem;
      line-height: 1.35;
    }
    body.pickup-page .lookup-heading span {
      color: var(--muted);
      font-size: .82rem;
      font-weight: 500;
      line-height: 1.55;
    }
    body.pickup-page .lookup-format {
      flex: none;
      color: var(--accent);
      font-size: .74rem;
      font-weight: 800;
      line-height: 1.5;
    }
    body.pickup-page .key-field {
      display: grid;
      gap: 9px;
    }
    body.pickup-page .field-label-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
    }
    body.pickup-page .field-label-row label {
      display: block;
      margin: 0;
      color: #344054;
      font-size: .84rem;
      font-weight: 800;
    }
    body.pickup-page .field-label-row span {
      color: #98a2b3;
      font-size: .72rem;
    }
    body.pickup-page .key-entry {
      display: block;
    }
    body.pickup-page .key-entry textarea {
      min-height: 150px;
      padding: 15px 16px;
      resize: vertical;
      border: 1px solid #cbd5e1;
      border-radius: 7px;
      background: #fbfcfe;
      color: var(--ink);
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      font-size: .88rem;
      font-weight: 600;
      line-height: 1.65;
      letter-spacing: 0;
    }
    body.pickup-page .key-entry textarea::placeholder {
      color: #98a2b3;
      font-family: Inter, "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
      font-weight: 500;
    }
    body.pickup-page .key-entry textarea:focus {
      border-color: var(--accent);
      background: #fff;
      box-shadow: 0 0 0 3px rgba(37, 99, 235, .12);
    }
    body.pickup-page .batch-hint {
      position: relative;
      margin: 10px 0 0;
      padding: 0 0 0 16px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--muted);
      font-size: .78rem;
      line-height: 1.55;
    }
    body.pickup-page .batch-hint::before {
      content: "";
      position: absolute;
      top: .55em;
      left: 2px;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #94a3b8;
    }
    body.pickup-page .batch-hint.is-batch {
      border: 0;
      background: transparent;
      color: #1d4ed8;
    }
    body.pickup-page .batch-hint.is-batch::before {
      background: var(--accent);
    }
    body.pickup-page .batch-hint strong {
      color: inherit;
    }
    body.pickup-page .claim-actions {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 18px;
    }
    body.pickup-page .claim-actions.is-batch {
      grid-template-columns: 1fr;
    }
    body.pickup-page #claimBtn,
    body.pickup-page #claimSubBtn,
    body.pickup-page #claimCockpitBtn,
    body.pickup-page #claimGrokCliBtn {
      width: 100%;
      min-height: 48px;
      margin-top: 0;
      border-radius: 7px;
      font-size: .9rem;
      font-weight: 800;
      white-space: normal;
    }
    body.pickup-page #claimBtn {
      background: var(--accent);
      color: #fff;
      box-shadow: 0 5px 14px rgba(37, 99, 235, .18);
    }
    body.pickup-page #claimBtn:hover {
      background: #1d4ed8;
    }
    body.pickup-page #claimSubBtn,
    body.pickup-page #claimCockpitBtn,
    body.pickup-page #claimGrokCliBtn {
      border: 1px solid var(--accent);
      background: #fff;
      color: var(--accent);
      box-shadow: none;
    }
    body.pickup-page #claimSubBtn:hover,
    body.pickup-page #claimCockpitBtn:hover,
    body.pickup-page #claimGrokCliBtn:hover {
      background: #eff6ff;
    }
    body.pickup-page #claimBtn:disabled,
    body.pickup-page #claimSubBtn:disabled,
    body.pickup-page #claimCockpitBtn:disabled,
    body.pickup-page #claimGrokCliBtn:disabled {
      background: #93a9d8;
      color: #fff;
      box-shadow: none;
    }
    body.pickup-page #hint:empty {
      display: none;
    }
    body.pickup-page .note {
      margin-top: 14px;
      padding: 11px 13px;
      border: 1px solid;
      border-left-width: 3px;
      border-radius: 6px;
      font-size: .8rem;
      line-height: 1.6;
    }
    body.pickup-page .note.ok {
      border-color: #a7e2c2;
      border-left-color: var(--good);
      background: var(--good-bg);
      color: #11633f;
    }
    body.pickup-page .note.warn {
      border-color: #f0d58a;
      border-left-color: #d19a14;
      background: var(--warn-bg);
      color: #805400;
    }
    body.pickup-page .note.err {
      border-color: #f2b8b5;
      border-left-color: var(--bad);
      background: var(--bad-bg);
      color: var(--bad);
    }
    body.pickup-page .pool-closed-alert {
      margin: 14px 0 0;
      padding: 12px 13px;
      border: 1px solid #f0d58a;
      border-left: 3px solid #d19a14;
      border-radius: 6px;
      background: var(--warn-bg);
      color: #805400;
      font-size: .8rem;
      line-height: 1.55;
    }
    body.pickup-page .pool-closed-alert span {
      color: #946b17;
      font-size: .76rem;
    }
    body.pickup-page .lookup-footnote {
      margin-top: 18px;
      padding-top: 16px;
      border-top: 1px solid #edf0f5;
      color: #7a8699;
      font-size: .76rem;
      line-height: 1.6;
    }
    body.pickup-page .pickup-side {
      padding-left: 28px;
      border-left: 1px solid var(--rule);
    }
    body.pickup-page .pickup-side-title {
      margin-bottom: 6px;
      color: var(--ink);
      font-size: .96rem;
      font-weight: 800;
    }
    body.pickup-page .pickup-side-copy {
      color: var(--muted);
      font-size: .78rem;
      line-height: 1.6;
    }
    body.pickup-page .guide-list {
      display: grid;
      gap: 0;
      margin-top: 14px;
    }
    body.pickup-page .guide-item {
      display: grid;
      grid-template-columns: 32px minmax(0, 1fr);
      gap: 11px;
      padding: 15px 0;
      border-bottom: 1px solid var(--rule);
    }
    body.pickup-page .guide-item:last-child {
      border-bottom: 0;
    }
    body.pickup-page .guide-index {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border: 1px solid #bfdbfe;
      border-radius: 6px;
      background: #eff6ff;
      color: #1d4ed8;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: .72rem;
      font-weight: 800;
    }
    body.pickup-page .guide-copy {
      min-width: 0;
    }
    body.pickup-page .guide-copy strong {
      display: block;
      margin-bottom: 4px;
      color: #344054;
      font-size: .82rem;
    }
    body.pickup-page .guide-copy span {
      display: block;
      color: var(--muted);
      font-size: .76rem;
      line-height: 1.6;
    }
    body.pickup-page .result-region:empty {
      display: none;
    }
    body.pickup-page .result-region {
      margin-top: 24px;
    }
    body.pickup-page .result {
      margin: 0;
      overflow: hidden;
      border: 1px solid #a7e2c2;
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 8px 24px rgba(16, 24, 40, .05);
    }
    body.pickup-page .result .hd {
      padding: 18px 20px;
      border-bottom: 1px solid #d8eee1;
      background: #f5fcf8;
    }
    body.pickup-page .result-heading {
      display: grid;
      gap: 4px;
      min-width: 0;
    }
    body.pickup-page .result-heading small {
      color: var(--good);
      font-size: .7rem;
      font-weight: 800;
    }
    body.pickup-page .result-heading strong {
      color: var(--ink);
      font-size: .94rem;
      overflow-wrap: anywhere;
    }
    body.pickup-page .result .tag {
      color: var(--good);
      background: #e5f6ec;
    }
    body.pickup-page .result .rows {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0 28px;
      padding: 10px 20px;
    }
    body.pickup-page .result .row {
      display: grid;
      grid-template-columns: 82px minmax(0, 1fr);
      gap: 12px;
      min-width: 0;
      padding: 10px 0;
      border-bottom: 1px solid #edf0f5;
      font-size: .8rem;
    }
    body.pickup-page .result .row .k {
      width: auto;
      color: #7a8699;
    }
    body.pickup-page .result .row .v {
      min-width: 0;
      color: #344054;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    body.pickup-page .result .row-wide {
      grid-column: 1 / -1;
    }
    body.pickup-page .result .row-wide .v {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    body.pickup-page .result .downloads {
      gap: 12px;
      padding: 14px 20px 20px;
    }
    body.pickup-page .result .downloads .note {
      margin: 0;
    }
    body.pickup-page .result .download-actions {
      display: flex;
      gap: 9px;
      flex-wrap: wrap;
    }
    body.pickup-page .result .download-actions .btn,
    body.pickup-page .result .download-actions button {
      min-height: 42px;
      padding: 0 16px;
      border-radius: 6px;
      font-size: .78rem;
      font-weight: 800;
    }
    body.pickup-page .result .download-actions .btn {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    body.pickup-page .result .download-actions .btn:hover {
      border-color: #1d4ed8;
      background: #1d4ed8;
    }
    body.pickup-page .result .download-actions .secondary {
      border: 1px solid var(--rule);
      background: #fff;
      color: #344054;
    }
    body.pickup-page .result .download-actions .secondary:hover {
      border-color: #94a3b8;
      background: #f8fafc;
      color: var(--ink);
    }
    body.pickup-page .pool-modal {
      background: rgba(15, 23, 42, .48);
    }
    body.pickup-page .pool-modal-card {
      border-color: var(--rule);
      background: #fff;
      color: var(--ink);
    }
    body.pickup-page .pool-modal-card p {
      color: var(--muted);
    }
    body.pickup-page .pool-modal-actions button {
      background: var(--accent);
      color: #fff;
    }
    body.pickup-page footer {
      margin-top: 34px;
      padding-top: 18px;
      border-color: var(--rule);
      color: #98a2b3;
    }
    @media (max-width: 820px) {
      body.pickup-page .pickup-grid {
        grid-template-columns: 1fr;
        gap: 24px;
      }
      body.pickup-page .pickup-side {
        padding: 22px 0 0;
        border-top: 1px solid var(--rule);
        border-left: 0;
      }
      body.pickup-page .guide-list {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0 22px;
      }
    }
    @media (max-width: 640px) {
      body.pickup-page .wrap {
        width: 100%;
        padding: 0 16px 24px;
      }
      body.pickup-page header {
        gap: 8px 14px;
        padding: 18px 0 24px;
      }
      body.pickup-page header .eyebrow {
        margin-top: 28px;
      }
      body.pickup-page header h1 {
        font-size: 1.75rem;
      }
      body.pickup-page .pickup-intro {
        display: grid;
        gap: 8px;
        padding: 14px 0 18px;
      }
      body.pickup-page .pickup-lead,
      body.pickup-page .lookup-card,
      body.pickup-page .announcement-card {
        width: 100%;
        max-width: none;
      }
      body.pickup-page .pickup-version {
        display: none;
      }
      body.pickup-page .announcement-card {
        margin-top: 14px;
      }
      body.pickup-page .pickup-facts {
        display: none;
      }
      body.pickup-page .live-pool {
        grid-template-columns: 1fr;
        gap: 12px;
        margin-top: 14px;
      }
      body.pickup-page .pickup-grid {
        margin-top: 18px;
      }
      body.pickup-page .lookup-card {
        padding: 20px 17px;
      }
      body.pickup-page .lookup-title {
        display: grid;
        gap: 8px;
        margin-bottom: 18px;
      }
      body.pickup-page .lookup-format {
        justify-self: start;
      }
      body.pickup-page .field-label-row {
        display: grid;
        gap: 4px;
      }
      body.pickup-page .key-entry textarea {
        height: 116px;
        min-height: 108px;
      }
      body.pickup-page .claim-actions {
        grid-template-columns: 1fr;
      }
      body.pickup-page .guide-list {
        grid-template-columns: 1fr;
      }
      body.pickup-page .result .hd {
        align-items: flex-start;
        padding: 16px;
      }
      body.pickup-page .result .rows {
        grid-template-columns: 1fr;
        padding: 8px 16px;
      }
      body.pickup-page .result .row-wide {
        grid-column: auto;
      }
      body.pickup-page .result .downloads {
        padding: 12px 16px 16px;
      }
      body.pickup-page .result .download-actions {
        display: grid;
        grid-template-columns: 1fr;
      }
      body.pickup-page .result .download-actions .btn,
      body.pickup-page .result .download-actions button {
        width: 100%;
        min-width: 0;
        white-space: normal;
        text-align: center;
      }
    }
  </style>"""
    body = f"""
<div class="pickup-intro">
  <p class="pickup-lead">粘贴卡密或取件链接即可领取账号文件。CPA、Sub2API、Cockpit 与 GrokCLI-2API 是四种独立格式，但都来自同一个已验活账号。</p>
  <span class="pickup-version">v{APP_VERSION}</span>
</div>
<section class="pickup-facts" aria-label="取件规则">
  <div class="pickup-fact"><span>CPA</span><strong>CPA-xai-邮箱.json</strong></div>
  <div class="pickup-fact"><span>Sub2API</span><strong>SUB2API-grok-邮箱.json</strong></div>
  <div class="pickup-fact"><span>Cockpit</span><strong>auth.json</strong></div>
  <div class="pickup-fact"><span>GrokCLI-2API</span><strong>grokcli-2api-auth-邮箱.json</strong></div>
</section>
<section id="livePool" class="live-pool" aria-label="实时号池状态">
  <div class="live-pool-main">
    <span>近期验活可兑换</span>
    <strong id="livePoolAvailable">--</strong>
    <small id="livePoolStatus" class="live-pool-meta">正在读取号池...</small>
  </div>
  <div class="live-pool-detail">
    <div class="live-pool-heading"><span>Grok 账号池</span><span id="livePoolCandidate">候选库存 --</span></div>
    <div class="live-pool-track" aria-hidden="true"><span id="livePoolBar"></span></div>
    <div id="livePoolRule" class="live-pool-meta">账号由后台独立验活；领取仅使用近期已验证库存。</div>
  </div>
</section>
{announcement_html}
{pool_closed_modal_html}
<main class="pickup-grid">
  <section class="lookup-card" aria-labelledby="pickupFormTitle">
    <div class="lookup-title">
      <div class="lookup-heading">
        <strong id="pickupFormTitle">输入卡密领取文件</strong>
        <span>支持卡密、完整取件链接，以及带 key 参数的链接。</span>
      </div>
      <small class="lookup-format">CPA / Sub2API / Cockpit / GrokCLI-2API / ZIP</small>
    </div>
    <div class="key-field">
      <div class="field-label-row">
        <label for="q">卡密或取件链接</label>
        <span>批量领取时每行输入一个</span>
      </div>
      <div class="key-entry">
        <textarea id="q" rows="5" autocomplete="off" spellcheck="false" aria-describedby="keyCountHint" placeholder="例如：DG-XXXX-XXXX-XXXX\n或粘贴完整取件链接">{initial_value}</textarea>
      </div>
    </div>
    <div id="keyCountHint" class="batch-hint">{html.escape(default_key_hint)}</div>
    {pool_closed_notice_html}
    <div id="claimActions" class="claim-actions">
      <button id="claimBtn" type="button" onclick="claim('cpa')">下载 CPA JSON</button>
      <button id="claimSubBtn" type="button" onclick="claim('sub2api')">下载 Sub2API JSON</button>
      <button id="claimCockpitBtn" type="button" onclick="claim('cockpit')">下载 Cockpit auth.json</button>
      <button id="claimGrokCliBtn" type="button" onclick="claim('grokcli2api')">下载 GrokCLI-2API JSON</button>
    </div>
    <div id="hint" aria-live="polite">{note}</div>
    <div class="lookup-footnote">选择所需格式后会自动下载；领取成功后结果区仍可分别下载其他格式。</div>
  </section>
  <aside class="pickup-side" aria-label="领取说明">
    <h2 class="pickup-side-title">领取说明</h2>
    <p class="pickup-side-copy">领取前请确认当前浏览器可正常保存下载文件。</p>
    <div class="guide-list">
      <div class="guide-item">
        <span class="guide-index">01</span>
        <div class="guide-copy"><strong>四种格式分开下载</strong><span>四个文件对应同一个账号且不重复消耗库存；GrokCLI-2API JSON 可直接在其管理台“账号导入”中上传。</span></div>
      </div>
      <div class="guide-item">
        <span class="guide-index">02</span>
        <div class="guide-copy"><strong>批量合并下载</strong><span>多个卡密每行一个，成功项会整理到一个临时 ZIP 中。</span></div>
      </div>
      <div class="guide-item">
        <span class="guide-index">03</span>
        <div class="guide-copy"><strong>卡密取件</strong><span>首次领取后 24 小时内可在任意浏览器凭同一卡密重新下载。</span></div>
      </div>
      <div class="guide-item">
        <span class="guide-index">04</span>
        <div class="guide-copy"><strong>24 小时有效</strong><span>有效期从首次领取开始计算，过期后原卡密不能再次下载。</span></div>
      </div>
    </div>
  </aside>
</main>
<section id="res" class="result-region" aria-live="polite"></section>
<script>
const q = document.querySelector('#q');
const claimBtn = document.querySelector('#claimBtn');
const claimSubBtn = document.querySelector('#claimSubBtn');
const claimCockpitBtn = document.querySelector('#claimCockpitBtn');
const claimGrokCliBtn = document.querySelector('#claimGrokCliBtn');
const claimActions = document.querySelector('#claimActions');
const res = document.querySelector('#res');
const hint = document.querySelector('#hint');
const keyCountHint = document.querySelector('#keyCountHint');
const poolClosedModal = document.querySelector('#poolClosedModal');
const poolClosedModalText = document.querySelector('#poolClosedModalText');
const livePoolAvailable = document.querySelector('#livePoolAvailable');
const livePoolCandidate = document.querySelector('#livePoolCandidate');
const livePoolBar = document.querySelector('#livePoolBar');
const livePoolStatus = document.querySelector('#livePoolStatus');
const livePoolRule = document.querySelector('#livePoolRule');
const defaultClaimText = claimBtn ? claimBtn.textContent : '下载 CPA JSON';
const defaultSubClaimText = claimSubBtn ? claimSubBtn.textContent : '下载 Sub2API JSON';
const defaultCockpitClaimText = claimCockpitBtn ? claimCockpitBtn.textContent : '下载 Cockpit auth.json';
const defaultGrokCliClaimText = claimGrokCliBtn ? claimGrokCliBtn.textContent : '下载 GrokCLI-2API JSON';
const poolClosed = {pool_closed_js};
const poolClosedMessage = {pool_closed_message_js};
let livePoolKnown = false;
let livePoolAvailableCount = 0;
async function refreshLivePool(){{
  try{{
    const response = await fetch('/api/pool-summary', {{cache:'no-store', credentials:'same-origin'}});
    if(!response.ok) throw new Error('pool unavailable');
    const payload = await response.json();
    const pool = payload && payload.pool ? payload.pool : {{}};
    const known = pool.availableKnown === true;
    const available = Math.max(0, Number(pool.availableCount || 0));
    const candidate = Math.max(0, Number(pool.candidateCount || 0));
    const ttl = Math.max(0, Number(pool.verificationTtlMinutes || 0));
    const percent = candidate > 0 ? Math.min(100, Math.round(available * 100 / candidate)) : 0;
    livePoolAvailable.textContent = known ? `${{available}} 个` : '--';
    livePoolCandidate.textContent = known ? `候选库存 ${{candidate}}` : '候选库存 --';
    livePoolBar.style.width = `${{known ? percent : 0}}%`;
    livePoolKnown = known;
    livePoolAvailableCount = available;
    livePoolStatus.textContent = known
      ? (available > 0 ? '号池在线，可提交卡密兑换' : '暂无近期验活库存，新卡暂不可领取')
      : '号池状态暂时读取失败';
    livePoolRule.textContent = known
      ? `近 ${{ttl || 60}} 分钟账号存活验证通过；额度或模型权限不足不阻止凭据打包。`
      : '账号由后台独立验活；领取仅使用近期已验证库存。';
    updateKeyHint();
  }}catch(_error){{
    livePoolAvailable.textContent = '--';
    livePoolCandidate.textContent = '候选库存 --';
    livePoolBar.style.width = '0%';
    livePoolStatus.textContent = '号池状态暂时读取失败';
    livePoolKnown = false;
    updateKeyHint();
  }}
}}
function esc(s){{return String(s ?? '').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function normalizeKey(s){{return String(s || '').replace(/\\s+/g,'').toUpperCase();}}
function extractKey(s){{
  const raw = String(s || '').trim();
  if(!raw) return '';
  try{{
    const url = new URL(raw, location.origin);
    const fromQuery = url.searchParams.get('key') || url.searchParams.get('KEY');
    if(fromQuery) return normalizeKey(fromQuery);
  }}catch(e){{}}
  const labelled = raw.match(/(?:key|卡密)\\s*[:：=]\\s*([A-Za-z0-9_-]{{4,}})/i);
  if(labelled) return normalizeKey(labelled[1]);
  const dg = raw.match(/DG-[A-Za-z0-9_-]+/i);
  if(dg) return normalizeKey(dg[0]);
  return normalizeKey(raw);
}}
function uniqueKeys(keys){{
  const seen = new Set();
  const out = [];
  for(const key of keys){{
    const normalized = normalizeKey(key);
    if(normalized && !seen.has(normalized)){{
      seen.add(normalized);
      out.push(normalized);
    }}
  }}
  return out;
}}
function extractKeys(s){{
  const raw = String(s || '').trim();
  if(!raw) return [];
  const keys = [];
  const dgMatches = raw.match(/DG-[A-Za-z0-9_-]+/gi) || [];
  for(const item of dgMatches) keys.push(item);
  for(const part of raw.split(/[\\s,，;；]+/)){{
    if(!part) continue;
    try{{
      const url = new URL(part, location.origin);
      const fromQuery = url.searchParams.get('key') || url.searchParams.get('KEY');
      if(fromQuery) keys.push(fromQuery);
    }}catch(e){{}}
  }}
  for(const line of raw.split(/\\r?\\n|[,，;；]+/)){{
    const key = extractKey(line);
    if(key) keys.push(key);
  }}
  return uniqueKeys(keys);
}}
function setClaimButtonText(text){{
  if(claimBtn && !claimBtn.disabled) claimBtn.textContent = text;
}}
function setClaimButtonMode(isBatch){{
  if(claimActions) claimActions.classList.toggle('is-batch', isBatch);
  if(claimSubBtn) claimSubBtn.hidden = isBatch;
  if(claimCockpitBtn) claimCockpitBtn.hidden = isBatch;
  if(claimGrokCliBtn) claimGrokCliBtn.hidden = isBatch;
}}
function closePoolClosedModal(){{
  if(poolClosedModal) poolClosedModal.hidden = true;
  document.body.classList.remove('modal-open');
}}
function showPoolClosedModal(message){{
  if(!poolClosedModal) return false;
  if(poolClosedModalText) poolClosedModalText.textContent = message || poolClosedMessage;
  poolClosedModal.hidden = false;
  document.body.classList.add('modal-open');
  return true;
}}
if(poolClosedModal){{
  poolClosedModal.addEventListener('click', (event) => {{
    if(event.target === poolClosedModal) closePoolClosedModal();
  }});
  document.addEventListener('keydown', (event) => {{
    if(event.key === 'Escape') closePoolClosedModal();
  }});
}}
function updateKeyHint(){{
  const keys = extractKeys(q.value);
  const noVerifiedStock = livePoolKnown && livePoolAvailableCount <= 0;
  if(!keyCountHint) return keys;
  setClaimButtonMode(keys.length > 1);
  keyCountHint.classList.toggle('is-batch', keys.length > 1 || poolClosed);
  if(keys.length > 1){{
    keyCountHint.innerHTML = poolClosed
      ? `已识别 <strong>${{keys.length}}</strong> 个卡密。当前号池为空：未激活卡密暂不可取件；已取件卡密可合并下载。`
      : (noVerifiedStock
          ? `已识别 <strong>${{keys.length}}</strong> 个卡密。近期验活库存为 0：新卡暂不可领取，已领取卡密仍可合并下载。`
          : `已识别 <strong>${{keys.length}}</strong> 个卡密，每个账号的 CPA、Sub2API、Cockpit 与 GrokCLI-2API 文件会分目录合并成 1 个 ZIP。`);
    setClaimButtonText('批量下载四格式 ZIP');
  }}else if(keys.length === 1){{
    keyCountHint.innerHTML = poolClosed
      ? '已识别 <strong>1</strong> 个卡密。当前号池为空：未激活卡密暂不可取件；已取件卡密可下载。'
      : (noVerifiedStock
          ? '已识别 <strong>1</strong> 个卡密。近期验活库存为 0：新卡暂不可领取，已领取卡密仍可下载。'
          : '已识别 <strong>1</strong> 个卡密，请分别选择 CPA、Sub2API、Cockpit 或 GrokCLI-2API。');
    setClaimButtonText(defaultClaimText);
  }}else{{
    keyCountHint.textContent = poolClosed
      ? poolClosedMessage
      : (noVerifiedStock
          ? '近期验活库存为 0；新卡暂不可领取，已领取卡密仍可重新下载。'
          : '单卡可分别下载 CPA、Sub2API、Cockpit 或 GrokCLI-2API；多卡会按四种格式分目录合并下载。');
    setClaimButtonText(defaultClaimText);
  }}
  return keys;
}}
function trigger(url, name){{setTimeout(()=>{{window.location.assign(url);}},250);}}
async function claim(downloadKind='cpa'){{
  if((claimBtn && claimBtn.disabled) || (claimSubBtn && claimSubBtn.disabled) || (claimCockpitBtn && claimCockpitBtn.disabled) || (claimGrokCliBtn && claimGrokCliBtn.disabled)) return;
  const keys = updateKeyHint();
  const value = keys[0] || '';
  q.value = keys.join('\\n');
  if(!value){{hint.innerHTML='<div class="note warn">请输入卡密 KEY。</div>';return;}}
  performClaim(keys, downloadKind);
}}
async function performClaim(keys, downloadKind='cpa'){{
  if((claimBtn && claimBtn.disabled) || (claimSubBtn && claimSubBtn.disabled) || (claimCockpitBtn && claimCockpitBtn.disabled) || (claimGrokCliBtn && claimGrokCliBtn.disabled)) return;
  const value = keys[0] || '';
  const oldText = claimBtn ? claimBtn.textContent : '';
  const oldSubText = claimSubBtn ? claimSubBtn.textContent : '';
  const oldCockpitText = claimCockpitBtn ? claimCockpitBtn.textContent : '';
  const oldGrokCliText = claimGrokCliBtn ? claimGrokCliBtn.textContent : '';
  const batch = keys.length > 1;
  const activeButton = batch || downloadKind === 'cpa'
    ? claimBtn
    : (downloadKind === 'sub2api'
        ? claimSubBtn
        : (downloadKind === 'cockpit' ? claimCockpitBtn : claimGrokCliBtn));
  if(claimBtn){{claimBtn.disabled = true; claimBtn.setAttribute('aria-busy', 'true');}}
  if(claimSubBtn){{claimSubBtn.disabled = true; claimSubBtn.setAttribute('aria-busy', 'true');}}
  if(claimCockpitBtn){{claimCockpitBtn.disabled = true; claimCockpitBtn.setAttribute('aria-busy', 'true');}}
  if(claimGrokCliBtn){{claimGrokCliBtn.disabled = true; claimGrokCliBtn.setAttribute('aria-busy', 'true');}}
  if(activeButton) activeButton.textContent = '正在分配';
  hint.innerHTML='<div class="note warn">正在分配近期已验活账号并生成文件...</div>';res.innerHTML='';
  try{{
    const r = await fetch(batch ? '/api/claim-batch' : '/api/claim', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(batch ? {{keys}} : {{key:value}})}}).then(x=>x.json());
    if(r.error){{
      hint.innerHTML='<div class="note err">'+esc(r.error)+'</div>';
      if(poolClosed) showPoolClosedModal(r.error);
      return;
    }}
    if(r.batch){{
      const keyList = Array.isArray(r.keys) ? r.keys : keys;
      const keyHtml = keyList.map((item) => `<span class="tag">${{esc(item)}}</span>`).join(' ');
      const partialErrors = Array.isArray(r.errors) ? r.errors : [];
      hint.innerHTML = r.partial
        ? '<div class="note warn">部分卡密领取成功并已开始下载；失败项：'+partialErrors.map(esc).join('；')+'</div>'
        : '<div class="note ok">批量卡密有效，已合并打包并开始下载。</div>';
      res.innerHTML = `<article class="card result">
        <div class="hd"><div class="result-heading"><small>领取成功</small><strong>已合并 ${{esc(r.key_count)}} 个卡密</strong></div><span class="tag">CPA + Sub2API + Cockpit + GrokCLI-2API ZIP</span></div>
        <div class="rows">
          <div class="row"><span class="k">文件名</span><span class="v">${{esc(r.zip_name)}}</span></div>
          <div class="row"><span class="k">交付类型</span><span class="v">CPA、Sub2API、Cockpit 与 GrokCLI-2API 分目录批量 ZIP</span></div>
          <div class="row"><span class="k">文件数</span><span class="v">${{r.file_count}}</span></div>
          <div class="row"><span class="k">大小</span><span class="v">${{esc(r.size_text)}}</span></div>
          <div class="row row-wide"><span class="k">卡密</span><span class="v">${{keyHtml}}</span></div>
          <div class="row row-wide"><span class="k">链接有效</span><span class="v">临时链接约 10 分钟；原卡密仍按首次领取后 24 小时有效</span></div>
        </div>
        <div class="downloads">
          <div class="note warn">每个卡密目录内分别存放 cpa/、sub2api/、cockpit/ 和 grokcli-2api/ 四种文件。</div>
          <div class="download-actions">
            <a class="btn" href="${{esc(r.download_url)}}" download>下载合并 ZIP</a>
            <button class="secondary" type="button" data-copy="${{esc(r.download_url)}}" onclick="copyText(this.dataset.copy,this)">复制下载链接</button>
          </div>
        </div>
      </article>`;
      trigger(r.download_url, r.zip_name);
      return;
    }}
    const downloadName = r.file_name || r.zip_name || 'account.json';
    const downloadLabel = r.cockpit_download_url ? '下载 CPA JSON' : (r.download_format === 'json' ? '下载 JSON' : '下载 ZIP');
    const subDownload = r.sub_download_url
      ? `<a class="btn secondary" href="${{esc(r.sub_download_url)}}" download="${{esc(r.sub_file_name || 'SUB2API-grok-account.json')}}">下载 Sub2API JSON</a>`
      : '';
    const cockpitDownload = r.cockpit_download_url
      ? `<a class="btn secondary" href="${{esc(r.cockpit_download_url)}}" download="auth.json">下载 Cockpit auth.json</a>`
      : '';
    const grokcliDownload = r.grokcli_download_url
      ? `<a class="btn secondary" href="${{esc(r.grokcli_download_url)}}" download="${{esc(r.grokcli_file_name || 'grokcli-2api-auth-account.json')}}">下载 GrokCLI-2API JSON</a>`
      : '';
    const selectedDownloads = {{
      cpa: {{url:r.download_url, name:downloadName, label:'CPA JSON'}},
      sub2api: {{url:r.sub_download_url, name:r.sub_file_name || 'SUB2API-grok-account.json', label:'Sub2API JSON'}},
      cockpit: {{url:r.cockpit_download_url, name:r.cockpit_file_name || 'auth.json', label:'Cockpit auth.json'}},
      grokcli2api: {{url:r.grokcli_download_url, name:r.grokcli_file_name || 'grokcli-2api-auth-account.json', label:'GrokCLI-2API JSON'}}
    }};
    const selected = selectedDownloads[downloadKind] && selectedDownloads[downloadKind].url
      ? selectedDownloads[downloadKind]
      : selectedDownloads.cpa;
    hint.innerHTML='<div class="note ok">卡密有效，已开始下载 '+esc(selected.label)+'。24 小时内可凭同一卡密重新下载。</div>';
    res.innerHTML = `<article class="card result">
      <div class="hd"><div class="result-heading"><small>领取成功</small><strong>卡密 ${{esc(r.key)}}</strong></div><span class="tag">${{r.sub_download_url ? 'CPA / Sub2API / Cockpit / GrokCLI-2API' : (r.download_format === 'json' ? 'JSON 文件' : 'ZIP 文件')}}</span></div>
      <div class="rows">
        <div class="row"><span class="k">CPA 文件名</span><span class="v">${{esc(downloadName)}}</span></div>
        <div class="row"><span class="k">Sub2API 文件名</span><span class="v">${{esc(r.sub_file_name || '-')}}</span></div>
        <div class="row"><span class="k">Cockpit 文件名</span><span class="v">${{esc(r.cockpit_file_name || 'auth.json')}}</span></div>
        <div class="row"><span class="k">GrokCLI-2API 文件名</span><span class="v">${{esc(r.grokcli_file_name || '-')}}</span></div>
        <div class="row"><span class="k">文件数</span><span class="v">${{r.file_count}}</span></div>
        <div class="row"><span class="k">大小</span><span class="v">${{esc(r.size_text)}}</span></div>
        <div class="row"><span class="k">取件时间</span><span class="v">${{esc(r.bound_at || '刚刚')}}</span></div>
        <div class="row"><span class="k">有效至</span><span class="v">${{esc(r.expires_at || '首次取件后 24 小时')}}</span></div>
      </div>
      <div class="downloads">
        <div class="note warn">浏览器没有自动下载时，可手动下载或复制链接。</div>
        <div class="download-actions">
          <a class="btn" href="${{esc(r.download_url)}}" download>${{downloadLabel}}</a>
          ${{subDownload}}
          ${{cockpitDownload}}
          ${{grokcliDownload}}
          <button class="secondary" type="button" data-copy="${{esc(r.download_url)}}" onclick="copyText(this.dataset.copy,this)">复制下载链接</button>
        </div>
      </div>
    </article>`;
    trigger(selected.url, selected.name);
  }}catch(e){{
    hint.innerHTML='<div class="note err">请求失败：'+esc(e.message)+'</div>';
  }}finally{{
    if(claimBtn){{claimBtn.disabled = false; claimBtn.textContent = oldText; claimBtn.removeAttribute('aria-busy');}}
    if(claimSubBtn){{claimSubBtn.disabled = false; claimSubBtn.textContent = oldSubText || defaultSubClaimText; claimSubBtn.removeAttribute('aria-busy');}}
    if(claimCockpitBtn){{claimCockpitBtn.disabled = false; claimCockpitBtn.textContent = oldCockpitText || defaultCockpitClaimText; claimCockpitBtn.removeAttribute('aria-busy');}}
    if(claimGrokCliBtn){{claimGrokCliBtn.disabled = false; claimGrokCliBtn.textContent = oldGrokCliText || defaultGrokCliClaimText; claimGrokCliBtn.removeAttribute('aria-busy');}}
    updateKeyHint();
  }}
}}
q.addEventListener('keydown', (event) => {{
  if(event.key === 'Enter' && (event.ctrlKey || event.metaKey)){{event.preventDefault();claim();}}
}});
q.addEventListener('input', updateKeyHint);
refreshLivePool();
setInterval(() => {{ if(!document.hidden) refreshLivePool(); }}, 10000);
document.addEventListener('visibilitychange', () => {{ if(!document.hidden) refreshLivePool(); }});
updateKeyHint();
if(q.value.trim()){{
  if(window.history && window.history.replaceState) window.history.replaceState(null, document.title, '/');
  if(!poolClosed) setTimeout(()=>claim(),300);
}}
</script>
"""
    return page_shell("账号文件取件", body, page_class="pickup-page", extra_style=pickup_styles)


def login_page(error: str = "") -> bytes:
    note = f'<div class="note err">{html.escape(error)}</div>' if error else ""
    body = f"""
<form class="panel login-panel" method="post" action="{ADMIN_PATH}/login">
  <div class="login-card-head">
    <span class="login-version">v{APP_VERSION}</span>
    <strong>管理员登录</strong>
    <span>上传交付文件、生成卡密和管理取件记录。</span>
  </div>
  <label>管理员密码
    <div class="password-row">
      <input id="adminPassword" name="password" type="password" autocomplete="current-password" placeholder="输入管理员密码" autofocus>
      <button class="secondary compact" type="button" onclick="togglePassword(this)">显示</button>
    </div>
  </label>
  <button class="full" type="submit">登录后台</button>
  <div class="login-foot">
    <a href="/">返回取件页</a>
    <span>DownloadGate v{APP_VERSION}</span>
  </div>
  {note}
</form>
<script>
function togglePassword(btn){{
  const input = document.querySelector('#adminPassword');
  if(!input) return;
  const visible = input.type === 'text';
  input.type = visible ? 'password' : 'text';
  btn.textContent = visible ? '显示' : '隐藏';
  input.focus();
}}
</script>
"""
    return page_shell("管理员登录", body, page_class="login-page")


def skipped_note_html(items: list[str], *, title: str = "已跳过") -> str:
    items = [str(item) for item in (items or []) if str(item or "").strip()]
    if not items:
        return ""
    rows = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    count = len(items)
    return f"""
<div class="note warn">
  <div class="note-title">{html.escape(title)} {count} 个重复/无效文件</div>
  <details>
    <summary>查看明细</summary>
    <ul class="skip-list">{rows}</ul>
  </details>
</div>"""


def admin_result_card(result: dict | None) -> str:
    if not result:
        return ""
    raw_key = str(result.get("key") or "")
    key = html.escape(raw_key or "库存待分配")
    title = html.escape(str(result.get("title") or ""))
    download = html.escape(str(result.get("download_url") or ""))
    claim = html.escape(str(result.get("claim_url") or ""))
    is_json = str(result.get("download_format") or "").lower() == "json"
    file_name = html.escape(
        str(result.get("json_name") or result.get("zip_name") or next(iter(result.get("files") or []), ""))
    )
    file_count = int(result.get("file_count") or 0)
    size_text = html.escape(str(result.get("size_text") or ""))
    errors = [str(item) for item in (result.get("errors") or [])]
    warnings = [str(item) for item in (result.get("warnings") or [])]
    skipped = skipped_note_html(errors, title="已跳过")
    warning_html = ""
    if warnings:
        warning_html = f'<div class="note warn">提示：{html.escape("；".join(warnings))}</div>'
    claim_block = (
        f"""<div>
      <span class="mini">用户取件地址</span>
      <div class="copyrow"><input value="{claim}" readonly onclick="this.select()"><button class="secondary" type="button" onclick="copyValue(this.previousElementSibling,this)">复制取件地址</button></div>
    </div>"""
        if raw_key
        else '<div class="note warn">库存包未绑定卡密，用户输入新卡密时会自动分配。</div>'
    )
    return f"""
<section class="card result">
  <div class="hd">
    <div><span class="mini">{'JSON 已就绪' if is_json else '上传成功，已压缩为 ZIP'}</span><div class="bigkey">{key}</div></div>
    <span class="tag">{file_count} 个 JSON</span>
  </div>
  <div class="rows">
    <div class="row"><span class="k">标题</span><span class="v">{title}</span></div>
    <div class="row"><span class="k">{'JSON 文件' if is_json else '压缩包'}</span><span class="v">{file_name} · {size_text}</span></div>
  </div>
  <div class="downloads stack">
    {claim_block}
    <div>
      <span class="mini">管理员下载地址</span>
      <div class="copyrow"><input value="{download}" readonly onclick="this.select()"><button class="secondary" type="button" onclick="copyValue(this.previousElementSibling,this)">复制下载地址</button></div>
    </div>
    <a class="btn" href="{download}" target="_blank" download>管理员下载 {'JSON' if is_json else 'ZIP'}</a>
    {warning_html}
    {skipped}
  </div>
</section>"""


def admin_results_card(
    results: list[dict],
    *,
    mini_text: str = "本次上传完成",
    big_text: str = "",
    tag_text: str = "单文件独立",
    force_list: bool = False,
) -> str:
    results = [item for item in results if item]
    if not results:
        return ""
    if len(results) == 1 and not force_list:
        return admin_result_card(results[0])

    keys = "\n".join(str(item.get("key") or "") for item in results)
    claims = "\n".join(str(item.get("claim_url") or "") for item in results)
    keys_attr = html.escape(keys, quote=True)
    claims_attr = html.escape(claims, quote=True)
    keys_disabled = "disabled" if not keys.strip() else ""
    claims_disabled = "disabled" if not claims.strip() else ""
    key_button_text = "复制本次卡密" if mini_text == "本次上传完成" else "复制这些卡密"
    claim_button_text = "复制本次取件地址" if mini_text == "本次上传完成" else "复制这些取件地址"
    skipped_items: list[str] = []
    for item in results:
        skipped_items.extend(str(err) for err in (item.get("errors") or []) if str(err or "").strip())
    skipped_html = skipped_note_html(skipped_items, title="已跳过")
    rows = ""
    for item in results:
        raw_key = str(item.get("key") or "")
        raw_title = str(item.get("title") or "") or "JSON 交付包"
        raw_claim = str(item.get("claim_url") or "")
        raw_download = str(item.get("download_url") or "")
        key = html.escape(raw_key or "库存待分配")
        key_attr = html.escape(raw_key, quote=True)
        title = html.escape(raw_title)
        claim = html.escape(raw_claim)
        claim_attr = html.escape(raw_claim, quote=True)
        download_attr = html.escape(raw_download, quote=True)
        file_count = int(item.get("file_count") or 0)
        size_text = html.escape(str(item.get("size_text") or ""))
        rows += f"""<div class="result-row">
      <div class="result-key">{key}</div>
      <div class="result-info">
        <div class="result-title" title="{title}">{title}</div>
        <div class="result-meta">{file_count} 个 JSON · {size_text}</div>
        <div class="result-link" title="{claim}">{claim}</div>
      </div>
      <div class="result-actions">
        <button class="secondary" type="button" data-copy="{key_attr}" onclick="copyText(this.dataset.copy,this)" {'disabled' if not raw_key else ''}>复制卡密</button>
        <button class="secondary" type="button" data-copy="{claim_attr}" onclick="copyText(this.dataset.copy,this)" {'disabled' if not raw_claim else ''}>复制取件</button>
        <a class="btn secondary download-mini" href="{download_attr}" target="_blank" download>下载</a>
      </div>
    </div>"""
    return f"""
<section class="card result">
  <div class="hd">
    <div><span class="mini">{html.escape(mini_text)}</span><div class="bigkey">{html.escape(big_text or f"已生成 {len(results)} 个卡密")}</div></div>
    <span class="tag">{html.escape(tag_text)}</span>
  </div>
  <div class="downloads stack">
    <div class="download-actions">
      <button class="secondary" type="button" data-copy="{keys_attr}" onclick="copyText(this.dataset.copy,this)" {keys_disabled}>{html.escape(key_button_text)}</button>
      <button class="secondary" type="button" data-copy="{claims_attr}" onclick="copyText(this.dataset.copy,this)" {claims_disabled}>{html.escape(claim_button_text)}</button>
    </div>
    <div class="result-list">{rows}</div>
    {skipped_html}
  </div>
</section>"""


def admin_result_from_bundle(handler: BaseHTTPRequestHandler, bundle_id: str) -> dict | None:
    bundle_id = str(bundle_id or "").strip()
    if not bundle_id:
        return None
    manifest = load_manifest()
    bundle = manifest.get("bundles", {}).get(bundle_id)
    if not bundle:
        return None
    result = dict(bundle)
    result["download_url"] = admin_download_url(handler, bundle_id)
    result_key = normalize_key(str(result.get("key") or ""))
    result["claim_url"] = (
        absolute_url(handler, f"/?key={quote(result_key, safe='')}")
        if result_key
        else ""
    )
    result["size_text"] = format_size(int(result.get("size") or 0))
    return result


def admin_results_from_ids(handler: BaseHTTPRequestHandler, bundle_ids: list[str]) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for bundle_id in bundle_ids:
        bundle_id = str(bundle_id or "").strip()
        if not bundle_id or bundle_id in seen:
            continue
        seen.add(bundle_id)
        result = admin_result_from_bundle(handler, bundle_id)
        if result:
            results.append(result)
    return results


def admin_auto_replenish_panel() -> str:
    status, error = load_auto_replenish_status()
    config = status.get("config") if isinstance(status.get("config"), dict) else {}

    def number(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    enabled = bool(config.get("enabled", False)) and not error
    threshold = max(1, number(config.get("threshold"), 100))
    replenish_count = max(1, number(config.get("replenish_count"), 100))
    candidate_stock = max(
        0, number(status.get("candidate_stock"), number(status.get("available_stock"), 0))
    )
    verified_stock = max(0, number(status.get("verified_stock"), candidate_stock))
    unverified_stock = max(
        0, number(status.get("unverified_stock"), candidate_stock - verified_stock)
    )
    prevalidate_ttl_minutes = max(0, number(status.get("prevalidate_ttl_minutes"), 60))
    active_task = status.get("active_task") if isinstance(status.get("active_task"), dict) else None
    latest_task = status.get("latest_auto_task") if isinstance(status.get("latest_auto_task"), dict) else None
    task = active_task or latest_task
    checked = "checked" if enabled else ""
    disabled = "disabled" if error else ""
    candidate_class = "warn" if candidate_stock < threshold else "good"
    verified_class = "good" if verified_stock > 0 else "warn"
    unverified_class = "warn" if unverified_stock > 0 else "good"

    status_labels = {
        "initializing": "准备中",
        "queued": "排队中",
        "running": "运行中",
        "stopping": "停止中",
        "completed": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "stopped": "已停止",
    }
    reason_labels = {
        "not_started": "等待首次库存检查",
        "triggered": "已触发补货任务",
        "active_task": "补货任务运行中",
        "stock_sufficient": "当前库存充足",
        "disabled": "自动补货已关闭",
        "cooldown": "补货任务冷却中",
        "enqueue_failed": "创建补货任务失败",
        "check_failed": "库存检查失败",
    }
    if error:
        state_class = "error"
        state_text = f"无法连接 Console：{html.escape(error)}"
    elif task:
        task_id = number(task.get("id"), 0)
        task_status = str(task.get("status") or "")
        completed = max(0, number(task.get("completed_count"), 0))
        failed = max(0, number(task.get("failed_count"), 0))
        target = max(0, number(task.get("target_count"), 0))
        task_label = status_labels.get(task_status, task_status or "未知状态")
        state_class = "active" if active_task else ""
        state_text = (
            f"任务 #{task_id} · {html.escape(task_label)} · "
            f"成功 {completed}/{target}，失败 {failed}"
        )
    else:
        state_class = ""
        reason = str(status.get("last_reason") or "not_started")
        state_text = reason_labels.get(reason, reason)

    return f"""
<form class="panel replenish-panel" method="post" action="{ADMIN_PATH}/auto-replenish">
  <div class="panel-title"><span>账号库存自动补货</span><small>Console 实时状态</small></div>
  <div class="replenish-toggle-row">
    <div class="replenish-toggle-copy">
      <strong>{'已开启' if enabled else '已关闭'}</strong>
      <span>候选库存低于阈值时自动创建注册任务；近期可交付库存按账号凭据存活计算，额度和模型权限仅作为附加状态。关闭开关不会中断已经运行的补货任务。</span>
    </div>
    <input type="hidden" name="enabled" value="0">
    <label class="switch-control" title="开启或关闭自动补货">
      <input type="checkbox" name="enabled" value="1" aria-label="自动补货开关" {checked} {disabled} onchange="this.form.requestSubmit()">
      <span class="switch-track" aria-hidden="true"></span>
    </label>
  </div>
  <div class="inventory-metrics" aria-label="自动补货状态">
    <div class="inventory-metric"><span>近期账号存活可交付（{prevalidate_ttl_minutes} 分钟）</span><strong class="{verified_class}">{verified_stock}</strong></div>
    <div class="inventory-metric"><span>未通过账号存活验证</span><strong class="{unverified_class}">{unverified_stock}</strong></div>
    <div class="inventory-metric"><span>候选库存（补货口径）</span><strong class="{candidate_class}">{candidate_stock}</strong></div>
    <div class="inventory-metric"><span>候选库存触发阈值</span><strong>&lt; {threshold}</strong></div>
    <div class="inventory-metric"><span>每次补货</span><strong>{replenish_count}</strong></div>
  </div>
  <div class="replenish-state {state_class}">{state_text}</div>
  <div class="grid">
    <label>触发阈值
      <input name="threshold" type="number" min="1" max="100000" value="{threshold}" required {disabled}>
    </label>
    <label>每次补货数量
      <input name="replenish_count" type="number" min="1" max="5000" value="{replenish_count}" required {disabled}>
    </label>
  </div>
  <button type="submit" {disabled}>保存补货设置</button>
</form>"""


def admin_page(
    handler: BaseHTTPRequestHandler,
    notice: str = "",
    result: dict | None = None,
    results: list[dict] | None = None,
    issued_batch: str = "",
    issued_platform: str = "",
) -> bytes:
    manifest = load_manifest()
    auto_replenish_panel = admin_auto_replenish_panel()
    cards = sorted(
        [
            card
            for card in (manifest.get("cards") or {}).values()
            if isinstance(card, dict) and not bool(card.get("deleted"))
        ],
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    card_counts = {
        status: sum(1 for card in cards if card_status_view(card)[0] == status)
        for status in ("issued", "provisioning", "claimed", "retryable", "failed", "void")
    }
    issued_batch = str(issued_batch or "").strip()[:80]
    issued_platform = str(issued_platform or "").strip().lower()
    issued_keys = [
        str(card.get("key") or "")
        for card in reversed(cards)
        if issued_batch
        and card.get("batch") == issued_batch
        and (not issued_platform or card.get("platform") == issued_platform)
    ]
    issued_result = ""
    if issued_keys:
        issued_text = html.escape("\n".join(issued_keys))
        issued_result = f"""<section class="card result">
  <div class="hd"><div><span class="mini">批量预发行完成</span><div class="bigkey">{html.escape(issued_batch)}</div></div><span class="tag">{html.escape(issued_platform or 'grok')} · {len(issued_keys)} 张</span></div>
  <label>卡密列表<textarea id="issuedCardKeys" rows="10" readonly onclick="this.select()">{issued_text}</textarea></label>
  <button type="button" onclick="copyText(document.querySelector('#issuedCardKeys').value,this)">复制全部卡密</button>
</section>"""
    def card_status_badge(card: dict) -> str:
        status, label = card_status_view(card)
        return (
            f'<span class="tag card-status {status}">'
            f'{html.escape(label)}</span>'
        )

    card_rows = "".join(
        f"<tr><td><span class=\"item-key\">{html.escape(str(card.get('key') or ''))}</span></td>"
        f"<td>{html.escape(str(card.get('platform') or 'grok'))}</td>"
        f"<td>{card_status_badge(card)}</td>"
        f"<td>{html.escape(str(card.get('batch') or '-'))}</td>"
        f"<td>{html.escape(str(card.get('created_at') or '-'))}</td>"
        f"<td>{html.escape(str(card.get('claimed_at') or card.get('last_error') or '-'))}</td></tr>"
        for card in cards[:200]
    )
    platform_options = "".join(
        f'<option value="{html.escape(platform, quote=True)}">{html.escape(platform)}</option>'
        for platform in CARD_PLATFORMS
    )
    cards_panel = f"""
<p class="lead">账号由后台独立自动验活，预发行卡密不预占账号。用户首次取件时仅从近期已验证的账号池分配并原子打包；同一卡密只对应一个账号和一个交付包。</p>
<section class="admin-summary" aria-label="卡密统计">
  <div class="stat"><span class="k">未使用卡密</span><span class="v">{card_counts['issued']}</span></div>
  <div class="stat"><span class="k">正在分配</span><span class="v">{card_counts['provisioning']}</span></div>
  <div class="stat"><span class="k">领取成功</span><span class="v">{card_counts['claimed']}</span></div>
  <div class="stat"><span class="k">领取超时可重试</span><span class="v">{card_counts['retryable']}</span></div>
  <div class="stat"><span class="k">领取失败</span><span class="v">{card_counts['failed']}</span></div>
  <div class="stat"><span class="k">已作废</span><span class="v">{card_counts['void']}</span></div>
</section>
<form class="panel upload-panel" method="post" action="{ADMIN_PATH}/cards/issue">
  <div class="panel-title"><span>批量生成预发行空卡</span><small>领取时才分配账号</small></div>
  <div class="grid">
    <label>数量<input name="count" type="number" min="1" max="{MAX_ISSUE_CARDS}" value="10" required></label>
    <label>批次<input name="batch" maxlength="80" placeholder="例如：7月第二批"></label>
    <label>目标平台<select name="platform" required>{platform_options}</select></label>
  </div>
  <button class="full" type="submit">生成预发行卡密</button>
</form>
<form class="panel upload-panel" method="post" action="{ADMIN_PATH}/cards/bulk"
      onsubmit="return confirm('确认批量处理这些卡密？作废后将立即无法领取。已领取卡会保留交付审计记录。')">
  <div class="panel-title"><span>批量销卡 / 删除卡密</span><small>每行一个卡密，也支持粘贴取件链接</small></div>
  <label>卡密列表
    <textarea name="card_keys" rows="7" required placeholder="DG-XXXX-XXXX-XXXX&#10;DG-YYYY-YYYY-YYYY"></textarea>
  </label>
  <div class="grid">
    <label>处理方式
      <select name="mode" required>
        <option value="revoke">批量作废（保留记录）</option>
        <option value="delete">删除未领取卡（已领取卡仅作废）</option>
      </select>
    </label>
  </div>
  <div class="note warn">作废会立即阻止领取和原下载链接；删除模式只隐藏并清理未领取卡，已领取卡保留交付记录用于防重复。</div>
  <button class="danger full" type="submit">执行批量销卡</button>
</form>
{issued_result}
<div class="admin-table"><table>
  <thead><tr><th>卡密</th><th>平台</th><th>状态</th><th>批次</th><th>发行时间</th><th>领取时间 / 最近错误</th></tr></thead>
  <tbody>{card_rows or '<tr><td colspan="6">暂无卡密记录</td></tr>'}</tbody>
</table></div>"""
    current_results = results or ([result] if result else [])
    current_result_ids = {
        str(item.get("id") or "").strip()
        for item in current_results
        if str(item.get("id") or "").strip()
    }
    bundles = sorted(
        manifest.get("bundles", {}).values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    total_count = len(bundles)
    replaced_count = sum(1 for item in bundles if item.get("replaced_by"))
    expired_count = sum(1 for item in bundles if not item.get("replaced_by") and bundle_is_expired(item))
    claimed_count = sum(
        1
        for item in bundles
        if item.get("bound_at") and not item.get("replaced_by") and not bundle_is_expired(item)
    )
    stock_count = sum(
        1
        for item in bundles
        if not item.get("bound_at") and not item.get("replaced_by") and not normalize_key(str(item.get("key") or ""))
    )
    unused_count = sum(
        1
        for item in bundles
        if not item.get("bound_at") and not item.get("replaced_by") and normalize_key(str(item.get("key") or ""))
    )
    total_size_text = html.escape(format_size(sum(int(item.get("size") or 0) for item in bundles)))
    summary_html = f"""<section class="admin-summary" aria-label="后台统计">
  <div class="stat"><span class="k">全部交付包</span><span class="v">{total_count}</span></div>
  <div class="stat"><span class="k">库存待分配</span><span class="v">{stock_count}</span></div>
  <div class="stat"><span class="k">未取件</span><span class="v">{unused_count}</span></div>
  <div class="stat"><span class="k">已取件</span><span class="v">{claimed_count}</span></div>
  <div class="stat"><span class="k">已过期</span><span class="v">{expired_count}</span></div>
  <div class="stat"><span class="k">已替换</span><span class="v">{replaced_count}</span></div>
  <div class="stat"><span class="k">ZIP 总容量</span><span class="v">{total_size_text}</span></div>
</section>"""
    rows = ""
    for item in bundles:
        item_id = str(item.get("id") or "")
        item_id_attr = html.escape(item_id, quote=True)
        raw_key = str(item.get("key") or "")
        raw_title = str(item.get("title") or "")
        key = html.escape(raw_key)
        key_attr = html.escape(raw_key, quote=True)
        title = html.escape(raw_title)
        download = html.escape(admin_download_url(handler, item_id))
        claim = html.escape(absolute_url(handler, f"/?key={quote(str(item.get('key') or ''), safe='')}"))
        claim_data_attr = "" if item.get("replaced_by") or bundle_is_expired(item) else claim
        if item.get("replaced_by"):
            status_key = "replaced"
            status_label = "已替换"
            status = '<span class="tag replaced">已替换</span>'
            status_time = "同卡密已被新上传替换"
        elif bundle_is_expired(item):
            status_key = "expired"
            status_label = "已过期"
            status = '<span class="tag expired">已过期</span>'
            expires_text = bundle_expires_text(item)
            status_time = f"取件有效期已于 {expires_text} 结束" if expires_text else "首次取件后 24 小时已结束"
        elif item.get("bound_at"):
            status_key = "claimed"
            status_label = "已取件"
            status = '<span class="tag">已取件</span>'
            expires_text = bundle_expires_text(item)
            status_time = f"{item.get('bound_at') or ''} · 有效至 {expires_text}" if expires_text else str(item.get("bound_at") or "")
        elif not normalize_key(raw_key):
            status_key = "stock"
            status_label = "库存"
            status = '<span class="tag">库存</span>'
            status_time = "等待用户输入新卡密后自动分配"
        else:
            status_key = "unused"
            status_label = "未取件"
            status = '<span class="tag">未取件</span>'
            status_time = "等待用户取件"
        files = [str(name) for name in (item.get("files") or [])]
        file_search = " ".join(files)
        created_at = str(item.get("created_at") or "-")
        created_at_html = html.escape(created_at)
        search_attr = html.escape(" ".join([raw_title, raw_key, status_label, created_at, file_search]).lower(), quote=True)
        is_current_upload = "1" if item_id in current_result_ids else "0"
        file_count = int(item.get("file_count") or len(files) or 0)
        if files:
            file_preview_text = "、".join(files[:2])
            if len(files) > 2:
                file_preview_text += f" 等 {len(files)} 个"
        else:
            file_preview_text = "-"
        file_preview = html.escape(file_preview_text)
        status_time_html = html.escape(status_time)
        if item.get("replaced_by"):
            claim_action = '<button class="secondary compact" type="button" disabled>取件已替换</button>'
        elif not normalize_key(raw_key):
            claim_action = '<button class="secondary compact" type="button" disabled>库存待分配</button>'
        elif bundle_is_expired(item):
            claim_action = '<button class="secondary compact" type="button" disabled>取件已过期</button>'
        else:
            claim_action = f'<button class="secondary compact" type="button" data-copy="{claim}" onclick="copyText(this.dataset.copy,this)">复制取件</button>'
        delete_form = f"""<form class="inline-form" method="post" action="{ADMIN_PATH}/cleanup" onsubmit="return confirm('\\u786e\\u8ba4\\u5220\\u9664\\u8fd9\\u4e2a\\u4ea4\\u4ed8\\u5305\\uff1f\\u5bf9\\u5e94 ZIP \\u548c\\u5361\\u5bc6\\u8bb0\\u5f55\\u90fd\\u4f1a\\u5220\\u9664\\u3002')">
        <input type="hidden" name="action" value="delete">
        <input type="hidden" name="bundle_id" value="{item_id_attr}">
        <button class="secondary compact danger" type="submit">&#21024;&#38500;</button>
      </form>"""
        rows += f"""<tr data-id="{item_id_attr}" data-status="{status_key}" data-new="{is_current_upload}" data-search="{search_attr}" data-key="{key_attr}" data-claim="{claim_data_attr}" data-download="{download}">
  <td class="select-cell"><input class="row-select" type="checkbox" value="{item_id_attr}" aria-label="选择 {key_attr}"></td>
  <td><div class="item-title" title="{title}">{title}</div><span class="item-key">{key}</span><div class="item-meta"><button class="secondary compact" type="button" data-copy="{key_attr}" onclick="copyText(this.dataset.copy,this)">复制卡密</button></div></td>
  <td>{status}<span class="status-time">{status_time_html}</span></td>
  <td><span class="status-time">{created_at_html}</span></td>
  <td><div class="file-cell"><b>{file_count}</b><span class="file-preview" title="{file_preview}">{file_preview}</span></div></td>
  <td>{html.escape(format_size(int(item.get("size") or 0)))}</td>
  <td><div class="action-grid">
    {claim_action}
    <button class="secondary compact" type="button" data-copy="{download}" onclick="copyText(this.dataset.copy,this)">复制下载</button>
    <a class="btn secondary compact" href="{download}" target="_blank" download>下载 ZIP</a>
    {delete_form}
  </div></td>
</tr>"""
    cleanup_actions = f"""<div class="admin-actions">
  <form class="inline-form" method="post" action="{ADMIN_PATH}/cleanup" onsubmit="return confirm('\\u786e\\u8ba4\\u6e05\\u7a7a\\u672a\\u53d6\\u4ef6\\u7684\\u4ea4\\u4ed8\\u5305\\uff1f')">
    <input type="hidden" name="action" value="clear_unused">
    <button class="secondary compact" type="submit">&#28165;&#31354;&#26410;&#21462;&#20214;</button>
  </form>
  <form class="inline-form" method="post" action="{ADMIN_PATH}/cleanup" onsubmit="return confirm('确认清理已过期的交付包和文件？')">
    <input type="hidden" name="action" value="clear_expired">
    <button class="secondary compact" type="submit">&#28165;&#29702;&#24050;&#36807;&#26399;</button>
  </form>
  <form class="inline-form" method="post" action="{ADMIN_PATH}/cleanup" onsubmit="return confirm('\\u786e\\u8ba4\\u6e05\\u7a7a\\u5df2\\u66ff\\u6362\\u7684\\u65e7\\u5305\\uff1f')">
    <input type="hidden" name="action" value="clear_replaced">
    <button class="secondary compact" type="submit">&#28165;&#31354;&#24050;&#26367;&#25442;</button>
  </form>
  <form class="inline-form" method="post" action="{ADMIN_PATH}/cleanup" onsubmit="return confirm('\\u786e\\u8ba4\\u6e05\\u7a7a\\u5168\\u90e8\\u8bb0\\u5f55\\u548c ZIP \\u6587\\u4ef6\\uff1f\\u8fd9\\u4e2a\\u64cd\\u4f5c\\u4e0d\\u4f1a\\u5220\\u9664\\u540e\\u53f0\\u5bc6\\u7801\\u3002')">
    <input type="hidden" name="action" value="clear_all">
    <button class="secondary compact danger" type="submit">&#28165;&#31354;&#20840;&#37096;</button>
  </form>
</div>"""
    table_tools = f"""<div class="toolbar">
  <div class="table-tools">
    <button class="compact" type="button" data-admin-tab-open="upload">上传交付</button>
    <button id="copyVisibleKeys" class="secondary compact" type="button">复制当前卡密</button>
    <a class="btn secondary compact" href="{ADMIN_PATH}/export.csv" download>导出 CSV</a>
    <form id="bulkDeleteForm" class="bulk-actions" method="post" action="{ADMIN_PATH}/cleanup">
      <input type="hidden" name="action" value="delete_selected">
      <input id="bulkDeleteIds" type="hidden" name="bundle_ids" value="">
      <button id="copySelectedKeys" class="secondary compact" type="button" disabled>复制选中卡密</button>
      <button id="copySelectedClaims" class="secondary compact" type="button" disabled>复制选中取件</button>
      <button id="bulkDeleteButton" class="secondary compact danger" type="submit" disabled>删除选中</button>
      <span id="selectedCount" class="table-count">已选 0 条</span>
    </form>
    <input id="adminSearch" class="search-input" placeholder="搜索标题、卡密或文件名" autocomplete="off">
    <div class="segmented" role="group" aria-label="状态筛选">
      <button type="button" class="active" data-filter="all">全部</button>
      <button type="button" data-filter="uploaded">刚上传</button>
      <button type="button" data-filter="stock">库存</button>
      <button type="button" data-filter="unused">未取件</button>
      <button type="button" data-filter="claimed">已取件</button>
      <button type="button" data-filter="expired">已过期</button>
      <button type="button" data-filter="replaced">已替换</button>
    </div>
    <span id="adminCount" class="table-count">当前显示 {total_count} 条</span>
    <div class="pagination" aria-label="列表分页">
      <button id="adminPrevPage" class="secondary compact" type="button">上一页</button>
      <span id="adminPageInfo" class="table-count">第 1/1 页</span>
      <button id="adminNextPage" class="secondary compact" type="button">下一页</button>
      <select id="adminPageSize" aria-label="每页条数">
        <option value="20">20 / 页</option>
        <option value="50" selected>50 / 页</option>
        <option value="100">100 / 页</option>
        <option value="0">全部</option>
      </select>
    </div>
  </div>
  {cleanup_actions}
</div>"""
    table = f"""<div class="admin-table"><table>
  <thead><tr><th class="select-head"><input id="selectVisibleRows" type="checkbox" aria-label="选择当前显示记录"></th><th style="width:27%">标题 / 卡密</th><th style="width:11%">状态</th><th style="width:13%">上传时间</th><th style="width:16%">文件</th><th style="width:8%">大小</th><th style="width:20%">操作</th></tr></thead>
  <tbody id="adminRows">{rows or '<tr><td colspan="7">暂无上传记录</td></tr>'}</tbody>
</table></div>"""
    recent_claims = sorted(
        [
            item
            for item in bundles
            if item.get("bound_at") and not item.get("replaced_by")
        ],
        key=lambda item: str(item.get("bound_at") or ""),
        reverse=True,
    )
    claim_rows = ""
    for item in recent_claims:
        item_id = str(item.get("id") or "")
        raw_key = str(item.get("key") or "")
        raw_title = str(item.get("title") or "") or "JSON 交付包"
        files = [str(name) for name in (item.get("files") or [])]
        key = html.escape(raw_key)
        key_attr = html.escape(raw_key, quote=True)
        title = html.escape(raw_title)
        claim_url = absolute_url(handler, f"/?key={quote(raw_key, safe='')}")
        download_url = admin_download_url(handler, item_id)
        claim = html.escape(claim_url, quote=True)
        download = html.escape(download_url, quote=True)
        raw_bound_at = str(item.get("bound_at") or "-")
        raw_bound_ip = str(item.get("bound_ip") or "-")
        raw_ua = str(item.get("bound_user_agent") or "")
        bound_at = html.escape(raw_bound_at)
        bound_ip = html.escape(raw_bound_ip)
        ua = html.escape(raw_ua[:90])
        expired = bundle_is_expired(item)
        expires_text = bundle_expires_text(item)
        expires_label = f"已过期 {expires_text}" if expired else f"有效至 {expires_text}" if expires_text else "首次取件后 24 小时有效"
        expires_html = html.escape(expires_label)
        claim_search = html.escape(
            " ".join([raw_key, raw_title, raw_bound_at, raw_bound_ip, raw_ua, expires_label, " ".join(files)]).lower(),
            quote=True,
        )
        file_count = int(item.get("file_count") or len(item.get("files") or []) or 0)
        size_text = html.escape(format_size(int(item.get("size") or 0)))
        claim_rows += f"""<div class="claim-row" data-claim-search="{claim_search}">
  <div class="claim-key">{key}</div>
  <div class="claim-info">
    <div class="claim-title" title="{title}">{title}</div>
    <div class="claim-meta">取件时间 {bound_at} · {expires_html} · {file_count} 个 JSON · {size_text}</div>
    <div class="claim-meta">IP {bound_ip}{' · ' + ua if ua else ''}</div>
  </div>
  <div class="claim-actions">
    <button class="secondary" type="button" data-copy="{key_attr}" onclick="copyText(this.dataset.copy,this)">复制卡密</button>
    <button class="secondary" type="button" data-copy="{claim}" onclick="copyText(this.dataset.copy,this)">复制取件</button>
    <button class="secondary" type="button" data-copy="{download}" onclick="copyText(this.dataset.copy,this)">复制下载</button>
    <a class="btn secondary" href="{download}" target="_blank" download>下载</a>
  </div>
</div>"""
    recent_claims_html = f"""<section class="card">
  <div class="hd">
    <div><span class="mini">最近提取记录</span><div class="bigkey">已取件 {len(recent_claims)} 条</div></div>
    <span class="tag">按取件时间排序</span>
  </div>
  <div class="downloads">
    <div class="table-tools claim-tools">
      <input id="claimsSearch" class="search-input" placeholder="搜索卡密、标题、IP 或文件名" autocomplete="off">
      <span id="claimsCount" class="table-count">当前显示 {len(recent_claims)} 条</span>
      <div class="pagination" aria-label="最近提取分页">
        <button id="claimsPrevPage" class="secondary compact" type="button">上一页</button>
        <span id="claimsPageInfo" class="table-count">第 1/1 页</span>
        <button id="claimsNextPage" class="secondary compact" type="button">下一页</button>
        <select id="claimsPageSize" aria-label="每页条数">
          <option value="20" selected>20 / 页</option>
          <option value="50">50 / 页</option>
          <option value="100">100 / 页</option>
          <option value="0">全部</option>
        </select>
      </div>
    </div>
    <div class="claim-list">{claim_rows or '<div class="note warn">暂无取件记录。</div>'}</div>
  </div>
</section>"""
    announcement = load_announcement()
    announcement_enabled = "checked" if announcement.get("enabled") else ""
    pool_closed_checked = "checked" if announcement.get("pool_closed") else ""
    announcement_title = html.escape(str(announcement.get("title") or "公告"), quote=True)
    announcement_content = html.escape(str(announcement.get("content") or ""))
    announcement_updated = str(announcement.get("updated_at") or "")
    announcement_updated_html = (
        f'<small>上次更新 {html.escape(announcement_updated)}</small>' if announcement_updated else "<small>取件页顶部显示</small>"
    )
    announcement_form = f"""<form class="panel announcement-panel" method="post" action="{ADMIN_PATH}/announcement">
  <div class="panel-title">
    <span>取件页公告</span>
    {announcement_updated_html}
  </div>
  <label class="check-row"><input type="checkbox" name="enabled" value="1" {announcement_enabled}><span>在取件页显示公告</span></label>
  <label class="check-row"><input type="checkbox" name="pool_closed" value="1" {pool_closed_checked}><span>号池为空模式：未激活卡密禁止取件，已取件用户仍可下载</span></label>
  <div class="grid">
    <label>公告标题
      <input name="title" maxlength="60" value="{announcement_title}" placeholder="例如：取件说明 / 最新公告">
    </label>
    <label>显示位置
      <input value="取件页输入框上方" readonly>
    </label>
  </div>
  <label>公告内容
    <textarea name="content" maxlength="3000" placeholder="支持多行纯文本，系统会自动过滤 HTML。">{announcement_content}</textarea>
  </label>
  <div class="download-actions">
    <button type="submit">保存公告</button>
    <button class="secondary" type="submit" name="announcement_action" value="disable">关闭公告</button>
  </div>
</form>"""
    note = f'<div class="note ok">{html.escape(notice)}</div>' if notice else ""
    result_html = admin_results_card(current_results)
    if not result_html:
        recent_ids = [
            str(item.get("id") or "")
            for item in bundles
            if item.get("id") and not item.get("replaced_by")
        ][:10]
        recent_results = admin_results_from_ids(handler, recent_ids)
        result_html = admin_results_card(
            recent_results,
            mini_text="最近生成卡密",
            big_text=f"最近 {len(recent_results)} 个卡密",
            tag_text="历史记录",
            force_list=True,
        )
    body = f"""
<div class="admin-tabs" role="tablist" aria-label="后台功能">
  <button class="admin-tab active" type="button" data-admin-tab="upload" role="tab" aria-selected="true">上传生成</button>
  <button class="admin-tab" type="button" data-admin-tab="cards" role="tab" aria-selected="false">预发行卡</button>
  <button class="admin-tab" type="button" data-admin-tab="inventory" role="tab" aria-selected="false">自动补货</button>
  <button class="admin-tab" type="button" data-admin-tab="accounts" role="tab" aria-selected="false">账户列表</button>
  <button class="admin-tab" type="button" data-admin-tab="list" role="tab" aria-selected="false">交付列表</button>
  <button class="admin-tab" type="button" data-admin-tab="claims" role="tab" aria-selected="false">最近提取</button>
  <button class="admin-tab" type="button" data-admin-tab="announcement" role="tab" aria-selected="false">公告设置</button>
</div>
<section class="admin-tab-panel" data-admin-panel="upload">
<p class="lead">上传一个或多个 JSON 文件，服务端会校验 JSON 格式并压缩为 ZIP。卡密由后台生成，首次取件后 24 小时内可重复下载。</p>
<form id="uploadForm" class="panel upload-panel" method="post" action="{ADMIN_PATH}/upload" enctype="multipart/form-data">
  <div class="panel-title">
    <span>上传 JSON 并生成卡密</span>
    <small>自动压缩 ZIP</small>
  </div>
  <div class="radio-grid" role="radiogroup" aria-label="打包方式">
    <label class="radio-card">
      <input type="radio" name="pack_mode" value="bundle">
      <span>合并打包<small>多个 JSON 合成一个 ZIP，生成一个卡密。</small></span>
    </label>
    <label class="radio-card">
      <input type="radio" name="pack_mode" value="split" checked>
      <span>单文件独立<small>每个 JSON 单独生成一个 ZIP 和卡密。</small></span>
    </label>
    <label class="radio-card">
      <input type="radio" name="pack_mode" value="pool">
      <span>入库存<small>不绑定卡密，用户取件时按最早上传自动分配。</small></span>
    </label>
  </div>
  <div class="grid">
    <label>标题
      <input name="title" placeholder="例如：7月账号包 / 客户A交付包">
    </label>
    <label>卡密 KEY
      <div class="copyrow"><input id="cardKey" name="key" placeholder="默认单文件独立，留空自动生成卡密"><button class="secondary" type="button" onclick="generateKey(this)">生成卡密</button></div>
    </label>
  </div>
  <label>JSON 文件
    <input name="files" type="file" accept=".json,application/json" multiple required>
  </label>
  <button id="uploadBtn" class="full" type="submit">上传并压缩</button>
  <div id="uploadHint">{note}</div>
</form>
{result_html}
</section>
<section class="admin-tab-panel" data-admin-panel="cards" hidden>
{cards_panel}
</section>
<section class="admin-tab-panel" data-admin-panel="inventory" hidden>
{auto_replenish_panel}
</section>
<section class="admin-tab-panel" data-admin-panel="accounts" hidden>
<p class="lead">查看 Console 账号池的脱敏状态。此页面不会读取或展示密码、SSO、access token、refresh token 等凭据。</p>
<section class="admin-summary" aria-label="账户统计">
  <div class="stat"><span class="k">总账号</span><span id="accountsSummaryTotal" class="v">-</span></div>
  <div class="stat"><span class="k">可交付</span><span id="accountsSummaryReady" class="v">-</span></div>
  <div class="stat"><span class="k">未验活</span><span id="accountsSummaryUnverified" class="v">-</span></div>
  <div class="stat"><span class="k">已失效</span><span id="accountsSummaryInvalid" class="v">-</span></div>
  <div class="stat"><span class="k">已交付</span><span id="accountsSummaryDelivered" class="v">-</span></div>
  <div class="stat"><span class="k">当前占用</span><span id="accountsSummaryLeased" class="v">-</span></div>
</section>
<div class="accounts-toolbar">
  <label>搜索账号
    <input id="accountsSearch" class="search-input" placeholder="邮箱或账号 ID" autocomplete="off">
  </label>
  <label>状态筛选
    <select id="accountsStatusFilter">
      <option value="all">全部状态</option>
      <option value="ready">可交付</option>
      <option value="unverified">未验活</option>
      <option value="invalid">已失效</option>
      <option value="delivered">已交付</option>
      <option value="leased">当前占用</option>
    </select>
  </label>
  <label>手动测试模型
    <input id="accountsModelInput" class="model-input" value="grok-4.5" maxlength="100" autocomplete="off">
  </label>
  <button id="accountsExport" class="secondary compact" type="button">导出当前筛选</button>
  <button id="accountsRefresh" class="secondary compact" type="button">刷新列表</button>
  <span id="accountsCount" class="table-count">尚未加载</span>
  <div class="pagination" aria-label="账户列表分页">
    <button id="accountsPrevPage" class="secondary compact" type="button">上一页</button>
    <span id="accountsPageInfo" class="table-count">第 1/1 页</span>
    <button id="accountsNextPage" class="secondary compact" type="button">下一页</button>
    <select id="accountsPageSize" aria-label="账户列表每页条数">
      <option value="10">10 / 页</option>
      <option value="25" selected>25 / 页</option>
      <option value="50">50 / 页</option>
      <option value="100">100 / 页</option>
      <option value="200">200 / 页</option>
    </select>
  </div>
</div>
<div class="account-migration-panel">
  <div class="account-migration-copy">
    <strong>账号迁移导入</strong>
    <span>导出包包含密码、SSO 和 OAuth Token；导入会自动按账号主体、SSO、邮箱去重，并在写入前备份数据库。</span>
  </div>
  <input id="accountsImportFile" type="file" accept=".json,application/json">
  <button id="accountsImportPreview" class="secondary compact" type="button">预检导入</button>
  <button id="accountsImport" class="compact" type="button">确认导入</button>
</div>
<div id="accountsFeedback" class="accounts-feedback" role="status" aria-live="polite"></div>
<div class="admin-table accounts-table"><table>
  <thead><tr><th style="width:16%">账号</th><th style="width:9%">库存状态</th><th style="width:9%">CPA 凭据</th><th style="width:8%">账号存活</th><th style="width:11%">最近验活</th><th style="width:11%">注册时间</th><th style="width:18%">模型测试</th><th style="width:18%">失败原因</th></tr></thead>
  <tbody id="accountsRows"><tr><td colspan="8">打开账户列表后加载数据</td></tr></tbody>
</table></div>
</section>
<section class="admin-tab-panel" data-admin-panel="list" hidden>
{summary_html}
{table_tools}
{table}
</section>
<section class="admin-tab-panel" data-admin-panel="claims" hidden>
{recent_claims_html}
</section>
<section class="admin-tab-panel" data-admin-panel="announcement" hidden>
<p class="lead">公告会显示在取件页输入框上方。内容按纯文本展示，HTML 会自动转义。</p>
{announcement_form}
</section>
<script>
const adminTabs = Array.from(document.querySelectorAll('[data-admin-tab]'));
const adminPanels = Array.from(document.querySelectorAll('[data-admin-panel]'));
function openAdminTab(name, updateHash=true){{
  const target = name || 'upload';
  adminTabs.forEach((btn) => {{
    const active = btn.dataset.adminTab === target;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  }});
  adminPanels.forEach((panel) => {{
    panel.hidden = panel.dataset.adminPanel !== target;
  }});
  if(updateHash && history.replaceState){{
    const clean = location.pathname + (location.search || '') + (target === 'upload' ? '' : '#'+target);
    history.replaceState(null, document.title, clean);
  }}
}}
adminTabs.forEach((btn) => btn.addEventListener('click', () => {{
  const target = btn.dataset.adminTab || 'upload';
  openAdminTab(target);
  if(target === 'accounts') loadAccounts();
}}));
document.querySelectorAll('[data-admin-tab-open]').forEach((btn) => {{
  btn.addEventListener('click', () => openAdminTab(btn.dataset.adminTabOpen || 'upload'));
}});
const initialAdminTab = location.hash === '#announcement' ? 'announcement' : location.hash === '#claims' ? 'claims' : location.hash === '#accounts' ? 'accounts' : location.hash === '#inventory' ? 'inventory' : location.hash === '#cards' ? 'cards' : location.hash === '#list' ? 'list' : 'upload';
openAdminTab(initialAdminTab, false);
if('scrollRestoration' in history) history.scrollRestoration = 'manual';
if(!location.hash) window.addEventListener('load', () => setTimeout(() => window.scrollTo(0, 0), 0));
async function generateKey(btn){{
  const oldText = btn.textContent;
  btn.textContent = '生成中...';
  btn.disabled = true;
  try{{
    const r = await fetch('{ADMIN_PATH}/api/key', {{method:'POST'}}).then(x=>x.json());
    if(r.key) document.querySelector('#cardKey').value = r.key;
  }}finally{{
    btn.disabled = false;
    btn.textContent = oldText;
  }}
}}
const uploadForm = document.querySelector('#uploadForm');
const uploadBtn = document.querySelector('#uploadBtn');
const uploadHint = document.querySelector('#uploadHint');
function adminEsc(s){{return String(s ?? '').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function renderUploadIssue(data, fallback){{
  const skipped = Array.isArray(data && data.skipped) ? data.skipped : [];
  if(skipped.length){{
    const main = data.error || `没有可导入的新文件，已跳过 ${{skipped.length}} 个重复/无效文件`;
    const items = skipped.map(x => '<li>'+adminEsc(x)+'</li>').join('');
    return '<div class="note warn"><div class="note-title">'+adminEsc(main)+'</div><details><summary>查看明细</summary><ul class="skip-list">'+items+'</ul></details></div>';
  }}
  return '<div class="note err">'+adminEsc((data && data.error) || fallback || '上传失败')+'</div>';
}}
if(uploadForm){{
  uploadForm.addEventListener('submit', async (event) => {{
    event.preventDefault();
    const oldText = uploadBtn ? uploadBtn.textContent : '';
    if(uploadBtn){{uploadBtn.disabled = true; uploadBtn.textContent = '上传中...';}}
    if(uploadHint) uploadHint.innerHTML = '<div class="note warn">正在上传并压缩，请稍等...</div>';
    try{{
      const r = await fetch(uploadForm.action, {{
        method: 'POST',
        body: new FormData(uploadForm),
        headers: {{'X-Requested-With': 'fetch', 'Accept': 'application/json'}},
      }});
      const data = await r.json().catch(() => ({{error: '服务端返回异常'}}));
      if(!r.ok || data.error){{
        if(uploadHint) uploadHint.innerHTML = renderUploadIssue(data, '上传失败');
        return;
      }}
      window.location.replace(data.redirect_url || '{ADMIN_PATH}');
    }}catch(e){{
      if(uploadHint) uploadHint.innerHTML = '<div class="note err">上传失败：'+adminEsc(e.message)+'</div>';
    }}finally{{
      if(uploadBtn){{uploadBtn.disabled = false; uploadBtn.textContent = oldText;}}
    }}
  }});
}}
const accountsSearch = document.querySelector('#accountsSearch');
const accountsStatusFilter = document.querySelector('#accountsStatusFilter');
const accountsRefresh = document.querySelector('#accountsRefresh');
const accountsRows = document.querySelector('#accountsRows');
const accountsFeedback = document.querySelector('#accountsFeedback');
const accountsCount = document.querySelector('#accountsCount');
const accountsPrevPage = document.querySelector('#accountsPrevPage');
const accountsNextPage = document.querySelector('#accountsNextPage');
const accountsPageInfo = document.querySelector('#accountsPageInfo');
const accountsPageSize = document.querySelector('#accountsPageSize');
const accountsModelInput = document.querySelector('#accountsModelInput');
const accountsExport = document.querySelector('#accountsExport');
const accountsImportFile = document.querySelector('#accountsImportFile');
const accountsImportPreview = document.querySelector('#accountsImportPreview');
const accountsImport = document.querySelector('#accountsImport');
const accountsSummaryFields = {{
  total: document.querySelector('#accountsSummaryTotal'),
  ready: document.querySelector('#accountsSummaryReady'),
  unverified: document.querySelector('#accountsSummaryUnverified'),
  invalid: document.querySelector('#accountsSummaryInvalid'),
  delivered: document.querySelector('#accountsSummaryDelivered'),
  leased: document.querySelector('#accountsSummaryLeased'),
}};
let accountsPage = 1;
let accountsPages = 1;
let accountsLoaded = false;
let accountsLoading = false;
let accountsReloadPending = false;
let accountsSearchTimer = null;
function accountPrimaryStatus(item){{
  const inventory = String(item.inventory_status || '').toLowerCase();
  const inventoryLabels = {{ready:'可交付',unverified:'未验活',invalid:'已失效',delivered:'已交付',leased:'当前占用'}};
  if(inventoryLabels[inventory]) return [inventory, inventoryLabels[inventory]];
  if(item.delivered) return ['delivered', '已交付'];
  if(item.leased) return ['leased', '当前占用'];
  const lifecycle = String(item.lifecycle_status || '').toLowerCase();
  const validity = String(item.validity_status || '').toLowerCase();
  if(['invalid','expired','disabled','dead'].includes(lifecycle) || ['invalid','expired','dead'].includes(validity)){{
    return ['invalid', '已失效'];
  }}
  if(item.credential_ready && item.account_alive) return ['ready', '可交付'];
  return ['unverified', '未验活'];
}}
function accountBadge(kind, label){{
  return '<span class="account-badge '+adminEsc(kind || 'neutral')+'">'+adminEsc(label || '-')+'</span>';
}}
function renderAccountRows(items){{
  if(!accountsRows) return;
  if(!items.length){{
    accountsRows.innerHTML = '<tr><td colspan="8">没有符合条件的账号</td></tr>';
    return;
  }}
  accountsRows.innerHTML = items.map((item) => {{
    const state = accountPrimaryStatus(item);
    const cpaReady = !!(item.credential_ready || item.recently_verified || state[0] === 'ready');
    const cpaKind = cpaReady ? 'ready' : (String(item.cpa_status || '').toLowerCase() === 'failed' ? 'invalid' : 'unverified');
    const cpaLabel = cpaReady ? '凭据就绪' : (item.cpa_status || '未就绪');
    const checkedAt = item.probe_checked_at || item.last_checked_at || '-';
    const hasProbe = !!(item.probe_checked_at || item.last_checked_at);
    const aliveKind = item.account_alive ? 'alive' : (hasProbe ? 'dead' : 'neutral');
    const aliveLabel = item.account_alive ? '存活' : (hasProbe ? '未通过' : '未验活');
    const stateDetail = [item.status, item.lifecycle_status, item.validity_status, item.plan_state].filter(Boolean).join(' · ') || '-';
    const failure = [item.failure_kind, item.last_error].filter(Boolean).join(' · ');
    const failureClass = failure ? 'account-error' : 'account-error none';
    const modelTested = !!item.model_test_checked_at;
    const modelKind = !modelTested ? 'neutral' : (item.model_test_ok ? 'ready' : (['quota_exhausted','rate_limited'].includes(String(item.model_test_failure_kind || '')) ? 'unverified' : 'invalid'));
    const modelLabel = !modelTested ? '尚未测试' : (item.model_test_ok ? '模型可用' : '测试未通过');
    const modelDetail = modelTested ? [
      item.model_test_model || '-',
      Number(item.model_test_status) ? 'HTTP '+Number(item.model_test_status) : '',
      Number(item.model_test_latency_ms) ? Number(item.model_test_latency_ms)+' ms' : '',
      item.model_test_transport || '',
      item.model_test_checked_at || '',
    ].filter(Boolean).join(' · ') : '手动操作，不影响取件验活库存';
    const modelError = [item.model_test_failure_kind, item.model_test_error].filter(Boolean).join(' · ');
    return '<tr>'+
      '<td><span class="account-email">'+adminEsc(item.email || '-')+'</span><span class="account-id">ID '+adminEsc(item.id || '-')+'</span></td>'+
      '<td>'+accountBadge(state[0], state[1])+'<span class="account-detail">'+adminEsc(stateDetail)+'</span></td>'+
      '<td>'+accountBadge(cpaKind, cpaLabel)+'<span class="account-detail">'+adminEsc(item.cpa_status || '-')+'</span></td>'+
      '<td>'+accountBadge(aliveKind, aliveLabel)+'</td>'+
      '<td>'+adminEsc(checkedAt)+'</td>'+
      '<td>'+adminEsc(item.created_at || '-')+'</td>'+
      '<td><div class="model-test-cell">'+accountBadge(modelKind, modelLabel)+'<span class="account-detail">'+adminEsc(modelDetail)+'</span>'+(modelError ? '<span class="account-error">'+adminEsc(modelError)+'</span>' : '')+'<button class="secondary compact" type="button" data-account-model-test="'+adminEsc(item.id || '')+'">模型测试</button></div></td>'+
      '<td><div class="'+failureClass+'">'+adminEsc(failure || '无错误记录')+'</div></td>'+
    '</tr>';
  }}).join('');
}}
function renderAccountSummary(summary){{
  Object.entries(accountsSummaryFields).forEach(([name, node]) => {{
    if(node) node.textContent = String(Number(summary && summary[name]) || 0);
  }});
}}
async function loadAccounts(force=false){{
  if(accountsLoading){{
    if(force) accountsReloadPending = true;
    return;
  }}
  if(accountsLoaded && !force) return;
  accountsLoading = true;
  if(accountsRefresh){{accountsRefresh.disabled = true; accountsRefresh.textContent = '加载中...';}}
  if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note warn">正在从 Console 加载脱敏账户状态...</div>';
  try{{
    const params = new URLSearchParams({{
      platform: 'grok',
      q: String(accountsSearch ? accountsSearch.value : '').trim(),
      status: String(accountsStatusFilter ? accountsStatusFilter.value : 'all'),
      page: String(accountsPage),
      page_size: String(Number(accountsPageSize ? accountsPageSize.value : 25) || 25),
    }});
    const response = await fetch('{ADMIN_PATH}/api/accounts?'+params.toString(), {{
      headers: {{'Accept': 'application/json'}},
      cache: 'no-store',
    }});
    const data = await response.json().catch(() => ({{error: '服务端返回了无效 JSON'}}));
    if(!response.ok || !data.ok) throw new Error(data.error || `HTTP ${{response.status}}`);
    accountsPage = Math.max(1, Number(data.page) || 1);
    accountsPages = Math.max(1, Number(data.pages) || 1);
    const total = Math.max(0, Number(data.total) || 0);
    const pageSize = Math.max(1, Number(data.page_size) || 25);
    const start = total ? (accountsPage - 1) * pageSize + 1 : 0;
    const end = total ? Math.min(total, start + (Array.isArray(data.items) ? data.items.length : 0) - 1) : 0;
    renderAccountRows(Array.isArray(data.items) ? data.items : []);
    renderAccountSummary(data.summary || {{}});
    if(accountsCount) accountsCount.textContent = total ? `当前显示 ${{start}}-${{end}} / ${{total}} 条` : '当前显示 0 条';
    if(accountsPageInfo) accountsPageInfo.textContent = `第 ${{accountsPage}}/${{accountsPages}} 页`;
    if(accountsPrevPage) accountsPrevPage.disabled = accountsPage <= 1;
    if(accountsNextPage) accountsNextPage.disabled = accountsPage >= accountsPages;
    if(accountsFeedback) accountsFeedback.innerHTML = '';
    accountsLoaded = true;
  }}catch(error){{
    if(accountsRows) accountsRows.innerHTML = '<tr><td colspan="8">账户列表暂不可用，请稍后刷新</td></tr>';
    if(accountsCount) accountsCount.textContent = '加载失败';
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note err">账户列表暂不可用：'+adminEsc(error && error.message ? error.message : error)+'</div>';
    if(accountsPrevPage) accountsPrevPage.disabled = true;
    if(accountsNextPage) accountsNextPage.disabled = true;
    accountsLoaded = false;
  }}finally{{
    accountsLoading = false;
    if(accountsRefresh){{accountsRefresh.disabled = false; accountsRefresh.textContent = '刷新列表';}}
    if(accountsReloadPending){{
      accountsReloadPending = false;
      loadAccounts(true);
    }}
  }}
}}
if(accountsSearch){{
  accountsSearch.addEventListener('input', () => {{
    clearTimeout(accountsSearchTimer);
    accountsSearchTimer = setTimeout(() => {{accountsPage = 1; loadAccounts(true);}}, 280);
  }});
}}
if(accountsStatusFilter) accountsStatusFilter.addEventListener('change', () => {{accountsPage = 1; loadAccounts(true);}});
if(accountsRefresh) accountsRefresh.addEventListener('click', () => loadAccounts(true));
if(accountsPrevPage) accountsPrevPage.addEventListener('click', () => {{
  if(accountsPage > 1){{accountsPage -= 1; loadAccounts(true);}}
}});
if(accountsNextPage) accountsNextPage.addEventListener('click', () => {{
  if(accountsPage < accountsPages){{accountsPage += 1; loadAccounts(true);}}
}});
if(accountsPageSize) accountsPageSize.addEventListener('change', () => {{accountsPage = 1; loadAccounts(true);}});
if(accountsExport) accountsExport.addEventListener('click', () => {{
  const params = new URLSearchParams({{
    platform: 'grok',
    q: String(accountsSearch ? accountsSearch.value : '').trim(),
    status: String(accountsStatusFilter ? accountsStatusFilter.value : 'all'),
  }});
  const anchor = document.createElement('a');
  anchor.href = '{ADMIN_PATH}/api/accounts/export?'+params.toString();
  anchor.download = '';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note warn">正在生成账号迁移包。文件包含敏感凭据，请妥善保存。</div>';
}});
async function selectedAccountMigration(){{
  const file = accountsImportFile && accountsImportFile.files ? accountsImportFile.files[0] : null;
  if(!file) throw new Error('请先选择账号迁移 JSON 文件');
  if(file.size > {MAX_UPLOAD_BYTES}) throw new Error('迁移文件超过大小限制');
  const document = JSON.parse(await file.text());
  if(!document || document.schema !== '{ACCOUNT_MIGRATION_SCHEMA}' || !Array.isArray(document.accounts)){{
    throw new Error('不是受支持的账号迁移文件');
  }}
  if(document.accounts.length < 1 || document.accounts.length > {MAX_ACCOUNT_MIGRATION_ITEMS}){{
    throw new Error('迁移文件账号数量不符合限制');
  }}
  return document;
}}
function accountImportSummary(data){{
  return '源记录 '+Number(data.source_count || 0)+' · 去重后 '+Number(data.unique_count || 0)+
    ' · 新增 '+Number(data.inserted || 0)+' · 更新 '+Number(data.updated || 0)+
    ' · 未变化 '+Number(data.unchanged || 0)+' · 去重 '+Number(data.duplicates_removed || 0);
}}
async function runAccountImport(dryRun){{
  const document = await selectedAccountMigration();
  if(!dryRun && !confirm('确认导入 '+document.accounts.length+' 条账号记录？现有同账号凭据可能被更新，系统会先自动备份数据库。')) return;
  const buttons = [accountsImportPreview, accountsImport].filter(Boolean);
  buttons.forEach((button) => button.disabled = true);
  if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note warn">'+(dryRun ? '正在预检迁移文件...' : '正在备份数据库并导入账号...')+'</div>';
  try{{
    const response = await fetch('{ADMIN_PATH}/api/accounts/import?dry_run='+(dryRun ? 'true' : 'false'), {{
      method: 'POST',
      headers: {{'Accept':'application/json','Content-Type':'application/json'}},
      body: JSON.stringify(document),
    }});
    const data = await response.json().catch(() => ({{error:'服务端返回了无效 JSON'}}));
    if(!response.ok || !data.ok) throw new Error(data.error || `HTTP ${{response.status}}`);
    if(!dryRun) await loadAccounts(true);
    const backup = data.backup ? ' · 备份 '+String(data.backup) : '';
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note ok">'+adminEsc((dryRun ? '预检完成：' : '导入完成：')+accountImportSummary(data)+backup)+'</div>';
  }}finally{{
    buttons.forEach((button) => button.disabled = false);
  }}
}}
if(accountsImportPreview) accountsImportPreview.addEventListener('click', () => {{
  runAccountImport(true).catch((error) => {{
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note err">预检失败：'+adminEsc(error && error.message ? error.message : error)+'</div>';
  }});
}});
if(accountsImport) accountsImport.addEventListener('click', () => {{
  runAccountImport(false).catch((error) => {{
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note err">导入失败：'+adminEsc(error && error.message ? error.message : error)+'</div>';
  }});
}});
if(accountsRows) accountsRows.addEventListener('click', async (event) => {{
  const button = event.target.closest('[data-account-model-test]');
  if(!button || button.disabled) return;
  const accountId = String(button.dataset.accountModelTest || '').trim();
  const model = String(accountsModelInput ? accountsModelInput.value : 'grok-4.5').trim();
  if(!/^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,99}}$/.test(model)){{
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note err">模型名称格式不正确。</div>';
    return;
  }}
  const oldText = button.textContent;
  button.disabled = true;
  button.textContent = '测试中...';
  if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note warn">正在测试账号 ID '+adminEsc(accountId)+' 的 '+adminEsc(model)+'，此操作不会改变取件库存判定...</div>';
  try{{
    const response = await fetch('{ADMIN_PATH}/api/accounts/'+encodeURIComponent(accountId)+'/model-test', {{
      method: 'POST',
      headers: {{'Accept':'application/json','Content-Type':'application/json'}},
      body: JSON.stringify({{model}}),
    }});
    const data = await response.json().catch(() => ({{error:'服务端返回了无效 JSON'}}));
    if(!response.ok || !data.ok || !data.test) throw new Error(data.error || `HTTP ${{response.status}}`);
    const test = data.test;
    await loadAccounts(true);
    const kind = test.model_available ? 'ok' : 'warn';
    const text = test.model_available
      ? '模型测试通过：'+String(test.model || model)+' · HTTP '+String(test.status || 200)+' · '+String(test.latency_ms || 0)+' ms'
      : '模型测试未通过：'+String(test.failure_kind || test.error || 'unknown');
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note '+kind+'">'+adminEsc(text)+'</div>';
  }}catch(error){{
    if(accountsFeedback) accountsFeedback.innerHTML = '<div class="note err">模型测试失败：'+adminEsc(error && error.message ? error.message : error)+'</div>';
  }}finally{{
    button.disabled = false;
    button.textContent = oldText;
  }}
}});
const adminSearch = document.querySelector('#adminSearch');
const adminCount = document.querySelector('#adminCount');
const adminRows = Array.from(document.querySelectorAll('#adminRows tr[data-status]'));
const filterButtons = Array.from(document.querySelectorAll('[data-filter]'));
const copyVisibleKeys = document.querySelector('#copyVisibleKeys');
const copySelectedKeys = document.querySelector('#copySelectedKeys');
const copySelectedClaims = document.querySelector('#copySelectedClaims');
const selectVisibleRows = document.querySelector('#selectVisibleRows');
const selectedCount = document.querySelector('#selectedCount');
const bulkDeleteForm = document.querySelector('#bulkDeleteForm');
const bulkDeleteIds = document.querySelector('#bulkDeleteIds');
const bulkDeleteButton = document.querySelector('#bulkDeleteButton');
const adminPrevPage = document.querySelector('#adminPrevPage');
const adminNextPage = document.querySelector('#adminNextPage');
const adminPageInfo = document.querySelector('#adminPageInfo');
const adminPageSizeSelect = document.querySelector('#adminPageSize');
const rowSelectors = Array.from(document.querySelectorAll('.row-select'));
let activeFilter = 'all';
let adminPage = 1;
let adminPageSize = Number(adminPageSizeSelect ? adminPageSizeSelect.value : 50) || 50;
function getFilteredRows(){{
  return adminRows.filter((row) => !row.classList.contains('is-hidden'));
}}
function getVisibleRows(){{
  return adminRows.filter((row) => !row.classList.contains('is-hidden') && !row.classList.contains('is-page-hidden'));
}}
function getSelectedRows(){{
  return adminRows.filter((row) => {{
    const checkbox = row.querySelector('.row-select');
    return checkbox && checkbox.checked;
  }});
}}
function uniqueDatasetValues(rows, name){{
  const seen = new Set();
  const values = [];
  rows.forEach((row) => {{
    const value = row.dataset[name] || '';
    if(value && !seen.has(value)){{
      seen.add(value);
      values.push(value);
    }}
  }});
  return values;
}}
function flashButton(btn, text){{
  if(!btn) return;
  const oldText = btn.textContent;
  btn.textContent = text;
  setTimeout(() => btn.textContent = oldText, 1200);
}}
function updateSelectionUi(){{
  const selected = getSelectedRows();
  const visible = getVisibleRows();
  adminRows.forEach((row) => {{
    const checkbox = row.querySelector('.row-select');
    row.classList.toggle('is-selected', !!(checkbox && checkbox.checked));
  }});
  if(selectedCount) selectedCount.textContent = `已选 ${{selected.length}} 条`;
  [copySelectedKeys, copySelectedClaims, bulkDeleteButton].forEach((btn) => {{
    if(btn) btn.disabled = selected.length === 0;
  }});
  if(selectVisibleRows){{
    const visibleSelected = visible.filter((row) => {{
      const checkbox = row.querySelector('.row-select');
      return checkbox && checkbox.checked;
    }}).length;
    selectVisibleRows.checked = visible.length > 0 && visibleSelected === visible.length;
    selectVisibleRows.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
    selectVisibleRows.disabled = visible.length === 0;
  }}
}}
function copySelectedDataset(name, btn, emptyText){{
  const values = uniqueDatasetValues(getSelectedRows(), name);
  if(!values.length){{
    flashButton(btn, emptyText);
    return;
  }}
  copyText(values.join('\\n'), btn);
}}
function pageCountFor(rows){{
  if(adminPageSize <= 0) return 1;
  return Math.max(1, Math.ceil(rows.length / adminPageSize));
}}
function applyPagination(resetPage){{
  const filtered = getFilteredRows();
  const totalPages = pageCountFor(filtered);
  if(resetPage) adminPage = 1;
  adminPage = Math.min(Math.max(adminPage, 1), totalPages);
  let start = 0;
  let end = filtered.length;
  if(adminPageSize > 0){{
    start = (adminPage - 1) * adminPageSize;
    end = start + adminPageSize;
  }}
  adminRows.forEach((row) => row.classList.add('is-page-hidden'));
  filtered.forEach((row, index) => {{
    const show = adminPageSize <= 0 || (index >= start && index < end);
    row.classList.toggle('is-page-hidden', !show);
  }});
  const shown = getVisibleRows().length;
  if(adminCount){{
    if(!filtered.length){{
      adminCount.textContent = '当前显示 0 条';
    }}else if(adminPageSize <= 0){{
      adminCount.textContent = `当前显示 ${{filtered.length}} 条`;
    }}else{{
      adminCount.textContent = `当前显示 ${{start + 1}}-${{Math.min(end, filtered.length)}} / ${{filtered.length}} 条`;
    }}
  }}
  if(adminPageInfo) adminPageInfo.textContent = `第 ${{adminPage}}/${{totalPages}} 页`;
  if(adminPrevPage) adminPrevPage.disabled = adminPage <= 1 || filtered.length === 0;
  if(adminNextPage) adminNextPage.disabled = adminPage >= totalPages || filtered.length === 0;
  updateSelectionUi();
}}
function applyAdminFilter(){{
  const term = String(adminSearch ? adminSearch.value : '').trim().toLowerCase();
  adminRows.forEach((row) => {{
    const status = row.dataset.status || '';
    const searchable = row.dataset.search || '';
    const okStatus = activeFilter === 'all' || (activeFilter === 'uploaded' ? row.dataset.new === '1' : status === activeFilter);
    const okSearch = !term || searchable.includes(term);
    const show = okStatus && okSearch;
    row.classList.toggle('is-hidden', !show);
  }});
  applyPagination(true);
}}
if(adminSearch) adminSearch.addEventListener('input', applyAdminFilter);
rowSelectors.forEach((checkbox) => {{
  checkbox.addEventListener('change', updateSelectionUi);
}});
if(selectVisibleRows){{
  selectVisibleRows.addEventListener('change', () => {{
    const checked = selectVisibleRows.checked;
    getVisibleRows().forEach((row) => {{
      const checkbox = row.querySelector('.row-select');
      if(checkbox) checkbox.checked = checked;
    }});
    updateSelectionUi();
  }});
}}
if(copySelectedKeys){{
  copySelectedKeys.addEventListener('click', () => copySelectedDataset('key', copySelectedKeys, '无卡密'));
}}
if(copySelectedClaims){{
  copySelectedClaims.addEventListener('click', () => copySelectedDataset('claim', copySelectedClaims, '无取件'));
}}
if(bulkDeleteForm){{
  bulkDeleteForm.addEventListener('submit', (event) => {{
    const ids = uniqueDatasetValues(getSelectedRows(), 'id');
    if(!ids.length){{
      event.preventDefault();
      flashButton(bulkDeleteButton, '未选择');
      return;
    }}
    if(!confirm(`确认删除选中的 ${{ids.length}} 个交付包？同卡密历史记录和对应 ZIP 也会一起删除。`)){{
      event.preventDefault();
      return;
    }}
    if(bulkDeleteIds) bulkDeleteIds.value = ids.join(',');
  }});
}}
if(copyVisibleKeys){{
  copyVisibleKeys.addEventListener('click', () => {{
    const seen = new Set();
    const keys = getVisibleRows()
      .map((row) => row.dataset.key || '')
      .filter((key) => {{
        if(!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      }});
    if(!keys.length){{
      const oldText = copyVisibleKeys.textContent;
      copyVisibleKeys.textContent = '无卡密';
      setTimeout(() => copyVisibleKeys.textContent = oldText, 1200);
      return;
    }}
    copyText(keys.join('\\n'), copyVisibleKeys);
  }});
}}
filterButtons.forEach((btn) => {{
  btn.addEventListener('click', () => {{
    activeFilter = btn.dataset.filter || 'all';
    filterButtons.forEach((item) => item.classList.toggle('active', item === btn));
    applyAdminFilter();
  }});
}});
if(adminPrevPage){{
  adminPrevPage.addEventListener('click', () => {{
    adminPage -= 1;
    applyPagination(false);
  }});
}}
if(adminNextPage){{
  adminNextPage.addEventListener('click', () => {{
    adminPage += 1;
    applyPagination(false);
  }});
}}
if(adminPageSizeSelect){{
  adminPageSizeSelect.addEventListener('change', () => {{
    adminPageSize = Number(adminPageSizeSelect.value) || 0;
    applyPagination(true);
  }});
}}
const claimsSearch = document.querySelector('#claimsSearch');
const claimsCount = document.querySelector('#claimsCount');
const claimsRows = Array.from(document.querySelectorAll('.claim-row[data-claim-search]'));
const claimsPrevPage = document.querySelector('#claimsPrevPage');
const claimsNextPage = document.querySelector('#claimsNextPage');
const claimsPageInfo = document.querySelector('#claimsPageInfo');
const claimsPageSizeSelect = document.querySelector('#claimsPageSize');
let claimsPage = 1;
let claimsPageSize = Number(claimsPageSizeSelect ? claimsPageSizeSelect.value : 20) || 20;
function getFilteredClaims(){{
  return claimsRows.filter((row) => !row.classList.contains('is-hidden'));
}}
function claimsPageCountFor(rows){{
  if(claimsPageSize <= 0) return 1;
  return Math.max(1, Math.ceil(rows.length / claimsPageSize));
}}
function applyClaimsPagination(resetPage){{
  const filtered = getFilteredClaims();
  const totalPages = claimsPageCountFor(filtered);
  if(resetPage) claimsPage = 1;
  claimsPage = Math.min(Math.max(claimsPage, 1), totalPages);
  let start = 0;
  let end = filtered.length;
  if(claimsPageSize > 0){{
    start = (claimsPage - 1) * claimsPageSize;
    end = start + claimsPageSize;
  }}
  claimsRows.forEach((row) => row.classList.add('is-page-hidden'));
  filtered.forEach((row, index) => {{
    const show = claimsPageSize <= 0 || (index >= start && index < end);
    row.classList.toggle('is-page-hidden', !show);
  }});
  if(claimsCount){{
    if(!filtered.length){{
      claimsCount.textContent = '当前显示 0 条';
    }}else if(claimsPageSize <= 0){{
      claimsCount.textContent = `当前显示 ${{filtered.length}} 条`;
    }}else{{
      claimsCount.textContent = `当前显示 ${{start + 1}}-${{Math.min(end, filtered.length)}} / ${{filtered.length}} 条`;
    }}
  }}
  if(claimsPageInfo) claimsPageInfo.textContent = `第 ${{claimsPage}}/${{totalPages}} 页`;
  if(claimsPrevPage) claimsPrevPage.disabled = claimsPage <= 1 || filtered.length === 0;
  if(claimsNextPage) claimsNextPage.disabled = claimsPage >= totalPages || filtered.length === 0;
}}
function applyClaimsFilter(){{
  const term = String(claimsSearch ? claimsSearch.value : '').trim().toLowerCase();
  claimsRows.forEach((row) => {{
    const searchable = row.dataset.claimSearch || '';
    row.classList.toggle('is-hidden', !!term && !searchable.includes(term));
  }});
  applyClaimsPagination(true);
}}
if(claimsSearch) claimsSearch.addEventListener('input', applyClaimsFilter);
if(claimsPrevPage){{
  claimsPrevPage.addEventListener('click', () => {{
    claimsPage -= 1;
    applyClaimsPagination(false);
  }});
}}
if(claimsNextPage){{
  claimsNextPage.addEventListener('click', () => {{
    claimsPage += 1;
    applyClaimsPagination(false);
  }});
}}
if(claimsPageSizeSelect){{
  claimsPageSizeSelect.addEventListener('change', () => {{
    claimsPageSize = Number(claimsPageSizeSelect.value) || 0;
    applyClaimsPagination(true);
  }});
}}
applyAdminFilter();
applyClaimsFilter();
if(initialAdminTab === 'accounts') loadAccounts();
if(window.history && window.history.replaceState && (location.search.includes('result=') || location.search.includes('results=') || location.search.includes('notice='))){{
  window.history.replaceState(null, document.title, '{ADMIN_PATH}');
}}
</script>
"""
    return page_shell("管理员上传", body, admin=True)


def absolute_url(handler: BaseHTTPRequestHandler, path: str) -> str:
    host = handler.headers.get("Host") or f"127.0.0.1:{handler.server.server_port}"
    proto = (handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    if proto not in {"http", "https"}:
        proto = "http"
    return f"{proto}://{host}{path}"


def admin_download_url(handler: BaseHTTPRequestHandler, bundle_id: str) -> str:
    return absolute_url(handler, bundle_download_path(bundle_id, admin=True))


def bundle_status(bundle: dict) -> tuple[str, str]:
    if bundle.get("replaced_by"):
        return "replaced", "已替换"
    if bundle_is_expired(bundle):
        return "expired", "已过期"
    if not normalize_key(str(bundle.get("key") or "")):
        return "stock", "库存"
    if bundle.get("bound_at"):
        return "claimed", "已取件"
    return "unused", "未取件"


def admin_export_csv(handler: BaseHTTPRequestHandler, status_filter: str = "all") -> bytes:
    manifest = load_manifest()
    bundles = sorted(
        (manifest.get("bundles") or {}).values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    status_filter = str(status_filter or "all").strip().lower()
    if status_filter not in {"all", "stock", "unused", "claimed", "expired", "replaced"}:
        status_filter = "all"

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "卡密",
            "平台",
            "状态",
            "标题",
            "取件地址",
            "管理员下载地址",
            "上传时间",
            "取件时间",
            "取件 IP",
            "文件数",
            "大小",
            "ZIP 文件名",
            "JSON 文件名",
        ]
    )
    for bundle in bundles:
        status_key, status_label = bundle_status(bundle)
        if status_filter != "all" and status_filter != status_key:
            continue
        bundle_id = str(bundle.get("id") or "")
        key = str(bundle.get("key") or "")
        claim_url = "" if status_key in {"stock", "replaced", "expired"} else absolute_url(handler, f"/?key={quote(key, safe='')}")
        download_url = admin_download_url(handler, bundle_id) if bundle_id else ""
        files = [str(name) for name in (bundle.get("files") or [])]
        writer.writerow(
            [
                key,
                str(bundle.get("platform") or "grok"),
                status_label,
                str(bundle.get("title") or ""),
                claim_url,
                download_url,
                str(bundle.get("created_at") or ""),
                str(bundle.get("bound_at") or ""),
                str(bundle.get("bound_ip") or ""),
                int(bundle.get("file_count") or len(files) or 0),
                format_size(int(bundle.get("size") or 0)),
                str(bundle.get("zip_name") or ""),
                " | ".join(files),
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def parse_cookies(handler: BaseHTTPRequestHandler) -> SimpleCookie:
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie") or "")
    return cookie


def is_admin(handler: BaseHTTPRequestHandler) -> bool:
    token = (parse_cookies(handler).get("dg_session") or {}).value if parse_cookies(handler).get("dg_session") else ""
    if not token:
        return False
    expires_at = SESSIONS.get(token)
    if not expires_at or expires_at < time.time():
        SESSIONS.pop(token, None)
        return False
    SESSIONS[token] = time.time() + SESSION_TTL_SECONDS
    return True


def make_session(handler: BaseHTTPRequestHandler) -> None:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + SESSION_TTL_SECONDS
    handler.send_header("Set-Cookie", f"dg_session={token}; HttpOnly; Path=/; SameSite=Lax")


def clear_session(handler: BaseHTTPRequestHandler) -> None:
    token_cookie = parse_cookies(handler).get("dg_session")
    if token_cookie:
        SESSIONS.pop(token_cookie.value, None)
    handler.send_header("Set-Cookie", "dg_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax")


def clear_client_cookie_headers() -> dict[str, str]:
    return {"Set-Cookie": "dg_client=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax"}


def request_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or handler.client_address[0]


def cleanup_batch_downloads() -> None:
    now = time.time()
    for token, item in list(BATCH_DOWNLOADS.items()):
        if float(item.get("expires_at") or 0) < now:
            BATCH_DOWNLOADS.pop(token, None)


def safe_zip_component(value: str, fallback: str = "item") -> str:
    keep = []
    for ch in str(value or fallback):
        if ch.isalnum() or ch in ".-_()[] ":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip(" .")[:80] or fallback


def safe_zip_member_path(value: str, fallback: str = "data.json") -> str:
    parts: list[str] = []
    for part in str(value or fallback).replace("\\", "/").split("/"):
        cleaned = safe_zip_component(part, "")
        if cleaned and cleaned not in {".", ".."}:
            parts.append(cleaned)
    return "/".join(parts[-4:]) or fallback


def unique_zip_member_name(name: str, used_names: set[str]) -> str:
    marker = name.casefold()
    if marker not in used_names:
        used_names.add(marker)
        return name
    prefix, sep, filename = name.rpartition("/")
    suffix = Path(filename).suffix or ".json"
    stem = Path(filename).stem or "data"
    for counter in range(2, 10000):
        candidate_name = f"{stem}-{counter:02d}{suffix}"
        candidate = f"{prefix}{sep}{candidate_name}" if prefix else candidate_name
        marker = candidate.casefold()
        if marker not in used_names:
            used_names.add(marker)
            return candidate
    fallback = f"{prefix}{sep}data-{secrets.token_hex(4)}.json" if prefix else f"data-{secrets.token_hex(4)}.json"
    used_names.add(fallback.casefold())
    return fallback


def prepare_claim_items(
    handler: BaseHTTPRequestHandler,
    manifest: dict,
    raw_keys: list[str],
) -> tuple[list[dict], list[dict], bool]:
    keys: list[str] = []
    seen: set[str] = set()
    for raw_key in raw_keys:
        key = normalize_key(str(raw_key or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    if not keys:
        return [], [{"message": "请输入卡密 KEY", "status": HTTPStatus.BAD_REQUEST}], False
    if len(keys) > MAX_BATCH_KEYS:
        return [], [{"message": f"一次最多支持 {MAX_BATCH_KEYS} 个卡密批量取件", "status": HTTPStatus.BAD_REQUEST}], False

    pool_closed = bool(load_announcement().get("pool_closed"))
    items: list[dict] = []
    errors: list[dict] = []
    changed = False
    for key in keys:
        card = (manifest.get("cards") or {}).get(key)
        if not isinstance(card, dict) or card.get("status") == "void":
            errors.append({"message": f"{key}: 卡密不存在或已作废", "status": HTTPStatus.NOT_FOUND})
            continue
        if pool_closed and not str(card.get("bundle_id") or ""):
            errors.append({"message": f"{key}: {pool_closed_message()}", "status": HTTPStatus.FORBIDDEN})
            continue
        try:
            bundle_id, bundle = provision_card_bundle(manifest, key)
        except Exception as exc:
            errors.append({"message": f"{key}: 自动验活打包失败：{exc}", "status": HTTPStatus.SERVICE_UNAVAILABLE})
            continue
        if not bundle_payload_exists(bundle_id, bundle):
            errors.append({"message": f"{key}: 交付文件不存在，请联系管理员", "status": HTTPStatus.NOT_FOUND})
            continue
        claimed_at = str(bundle.get("bound_at") or "")
        if claimed_at and bundle_is_expired(bundle):
            expires_text = bundle_expires_text(bundle)
            errors.append(
                {
                    "message": f"{key}: 卡密首次取件后 24 小时有效，已于 {expires_text or '当前时间'} 过期",
                    "status": HTTPStatus.FORBIDDEN,
                }
            )
            continue
        if not claimed_at and pool_closed:
            errors.append({"message": f"{key}: {pool_closed_message()}", "status": HTTPStatus.FORBIDDEN})
            continue
        items.append(
            {
                "key": key,
                "bundle_id": str(bundle_id),
                "bundle": bundle,
                "claim": not bool(claimed_at),
            }
        )

    if items:
        with MANIFEST_LOCK:
            sync_manifest(manifest, load_manifest())
            valid_items: list[dict] = []
            for item in items:
                bundle = manifest.setdefault("bundles", {}).get(item["bundle_id"])
                if not isinstance(bundle, dict):
                    errors.append(
                        {
                            "key": item["key"],
                            "message": f"{item['key']}: 交付包记录不存在",
                            "status": HTTPStatus.NOT_FOUND,
                        }
                    )
                    continue
                item["bundle"] = bundle
                valid_items.append(item)
                if item.get("claim") and not bundle.get("bound_at"):
                    bundle["bound_client"] = ""
                    bundle["bound_at"] = now_text()
                    bundle["bound_ip"] = request_ip(handler)
                    bundle["bound_user_agent"] = (handler.headers.get("User-Agent") or "")[:180]
                    card = manifest.setdefault("cards", {}).get(item["key"])
                    if isinstance(card, dict):
                        card["status"] = "claimed"
                        card["claimed_at"] = str(card.get("claimed_at") or now_text())
                    changed = True
                elif bundle.get("bound_client"):
                    bundle["bound_client"] = ""
                    changed = True
            if changed:
                save_manifest(manifest)
            items = valid_items
    return items, errors, changed


def create_batch_download(items: list[dict]) -> dict:
    cleanup_batch_downloads()
    token = secrets.token_urlsafe(24)
    batch = {
        "token": token,
        "expires_at": time.time() + BATCH_DOWNLOAD_TTL_SECONDS,
        "zip_name": f"DG-BATCH-{time.strftime('%Y%m%d-%H%M%S', time.localtime())}-{len(items)}.zip",
        "items": [
            {
                "key": item["key"],
                "bundle_id": item["bundle_id"],
                "title": str(item["bundle"].get("title") or ""),
            }
            for item in items
        ],
    }
    BATCH_DOWNLOADS[token] = batch
    return batch


def build_batch_zip_bytes(manifest: dict, batch: dict) -> bytes:
    buffer = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for index, item in enumerate(batch.get("items") or [], 1):
            key = str(item.get("key") or "")
            bundle_id = str(item.get("bundle_id") or "")
            bundle = manifest.get("bundles", {}).get(bundle_id) or {}
            folder = f"{index:02d}-{safe_zip_component(key)}"
            source_path = bundle_payload_path(bundle_id, bundle)
            if not source_path or not source_path.exists():
                raise FileNotFoundError(f"missing delivery file for bundle {bundle_id}")
            if source_path.suffix.lower() == ".json":
                member_name = safe_zip_member_path(bundle_download_name(bundle_id, bundle))
                raw = source_path.read_bytes()
                if normalize_card_platform(bundle.get("platform")) == "grok":
                    arcname = unique_zip_member_name(f"{folder}/cpa/{member_name}", used_names)
                    target.writestr(arcname, raw)
                    document = json.loads(raw.decode("utf-8-sig"))
                    sub_raw = json.dumps(
                        sub2api_payload(document), ensure_ascii=False, indent=2
                    ).encode("utf-8")
                    sub_name = unique_zip_member_name(
                        f"{folder}/sub2api/{sub2api_filename(document)}", used_names
                    )
                    target.writestr(sub_name, sub_raw)
                    cockpit_raw = json.dumps(
                        cockpit_auth_payload(document), ensure_ascii=False, indent=2
                    ).encode("utf-8")
                    cockpit_name = unique_zip_member_name(
                        f"{folder}/cockpit/auth.json", used_names
                    )
                    target.writestr(cockpit_name, cockpit_raw)
                    grokcli_path = bundle_variant_json_path(bundle_id, GROKCLI2API_VARIANT)
                    if grokcli_path and grokcli_path.exists():
                        grokcli_raw = grokcli_path.read_bytes()
                        variant_meta = (
                            bundle.get("variants", {}).get(GROKCLI2API_VARIANT, {})
                            if isinstance(bundle.get("variants"), dict)
                            else {}
                        )
                        grokcli_file_name = safe_filename(
                            str(variant_meta.get("file_name") or grokcli_2api_filename(document)),
                            "grokcli-2api-auth-account.json",
                        )
                    else:
                        grokcli_raw = json.dumps(
                            grokcli_2api_payload(document), ensure_ascii=False, indent=2
                        ).encode("utf-8")
                        grokcli_file_name = grokcli_2api_filename(document)
                    grokcli_name = unique_zip_member_name(
                        f"{folder}/grokcli-2api/{grokcli_file_name}", used_names
                    )
                    target.writestr(grokcli_name, grokcli_raw)
                else:
                    arcname = unique_zip_member_name(f"{folder}/{member_name}", used_names)
                    target.writestr(arcname, raw)
            else:
                with zipfile.ZipFile(source_path, "r") as source:
                    for member in source.infolist():
                        if member.is_dir():
                            continue
                        member_name = safe_zip_member_path(member.filename)
                        arcname = unique_zip_member_name(f"{folder}/{member_name}", used_names)
                        target.writestr(arcname, source.read(member.filename))
    return buffer.getvalue()


class DownloadGateHandler(BaseHTTPRequestHandler):
    server_version = f"DownloadGate/{APP_VERSION}"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{now_text()}] {self.address_string()} {fmt % args}")

    def send_html(self, content: bytes, status: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Content-Length", str(len(content)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, payload: dict, status: int = 200, extra_headers: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_plain(self, message: str, status: int = 404) -> None:
        data = str(message or "Not Found").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_csv(self, data: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json_download(self, data: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, *, cookie_action: str = "", status: int = 302) -> None:
        self.send_response(status)
        if cookie_action == "make":
            make_session(self)
        elif cookie_action == "clear":
            clear_session(self)
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Location", location)
        self.end_headers()

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("上传内容过大")
        return self.rfile.read(length)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if is_sensitive_request_path(path):
            self.send_plain("Not Found", HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        if path == "/api/pool-summary":
            self.send_json(
                public_pool_summary(),
                extra_headers={"Cache-Control": "no-store, max-age=0"},
            )
            return
        if path == "/":
            key = (query.get("key") or [""])[0]
            self.send_html(
                user_page(initial_key=key),
                extra_headers=clear_client_cookie_headers(),
            )
            return
        if path == f"{ADMIN_PATH}/api/accounts":
            if not is_admin(self):
                self.send_json(
                    {"ok": False, "error": "请先登录。"},
                    HTTPStatus.UNAUTHORIZED,
                    {"Cache-Control": "no-store, max-age=0"},
                )
                return
            self.handle_admin_accounts(parsed)
            return
        if path == f"{ADMIN_PATH}/api/accounts/export":
            if not is_admin(self):
                self.send_json(
                    {"ok": False, "error": "请先登录。"},
                    HTTPStatus.UNAUTHORIZED,
                    {"Cache-Control": "no-store, max-age=0"},
                )
                return
            self.handle_admin_accounts_export(parsed)
            return
        if path == f"{ADMIN_PATH}/export.csv":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            status_filter = (query.get("status") or ["all"])[0]
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            self.send_csv(admin_export_csv(self, status_filter), f"download-gate-{stamp}.csv")
            return
        if path.rstrip("/") == ADMIN_PATH:
            if is_admin(self):
                raw_results = (query.get("results") or [""])[0]
                result_ids = [item for item in raw_results.split(",") if item]
                if not result_ids:
                    result_ids = [(query.get("result") or [""])[0]]
                notice = (query.get("notice") or [""])[0]
                issued_batch = (query.get("issued_batch") or [""])[0]
                issued_platform = (query.get("issued_platform") or [""])[0]
                self.send_html(
                    admin_page(
                        self,
                        notice=notice,
                        results=admin_results_from_ids(self, result_ids),
                        issued_batch=issued_batch,
                        issued_platform=issued_platform,
                    )
                )
            else:
                self.send_html(login_page())
            return
        if path == f"{ADMIN_PATH}/logout":
            self.redirect(ADMIN_PATH, cookie_action="clear")
            return
        if path.startswith("/download-batch/") and path.endswith(".zip"):
            self.serve_batch_download(parsed)
            return
        if path.startswith("/download/") and (path.endswith(".zip") or path.endswith(".json")):
            self.serve_download(parsed)
            return
        self.send_html(user_page("页面不存在。"), HTTPStatus.NOT_FOUND)

    def handle_admin_accounts(self, parsed) -> None:
        try:
            payload = load_admin_accounts(parse_qs(parsed.query, keep_blank_values=True))
        except Exception as exc:
            message = str(exc).strip()[:500] or "Console 暂时不可用"
            self.send_json(
                {"ok": False, "error": f"无法加载 Console 账户列表：{message}"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        self.send_json(
            payload,
            extra_headers={"Cache-Control": "no-store, max-age=0"},
        )

    def handle_admin_accounts_export(self, parsed) -> None:
        try:
            filename, data = load_admin_account_export(
                parse_qs(parsed.query, keep_blank_values=True)
            )
        except Exception as exc:
            message = str(exc).strip()[:500] or "Console 暂时不可用"
            self.send_json(
                {"ok": False, "error": f"账号导出失败：{message}"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        self.send_json_download(data, filename)

    def handle_admin_accounts_import(self, parsed) -> None:
        try:
            document = json.loads(self.read_body().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_json(
                {"ok": False, "error": f"迁移文件 JSON 无效：{exc}"},
                HTTPStatus.BAD_REQUEST,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        dry_run = (parse_qs(parsed.query).get("dry_run") or ["false"])[0].lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            payload = import_admin_accounts(document, dry_run=dry_run)
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.BAD_REQUEST,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        except Exception as exc:
            message = str(exc).strip()[:500] or "Console 暂时不可用"
            status = (
                HTTPStatus.UNPROCESSABLE_ENTITY
                if any(marker in message for marker in ("HTTP 409", "HTTP 413", "HTTP 422"))
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self.send_json(
                {"ok": False, "error": f"账号导入失败：{message}"},
                status,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        self.send_json(
            payload,
            extra_headers={"Cache-Control": "no-store, max-age=0"},
        )

    def handle_admin_account_model_test(self, account_id: int) -> None:
        try:
            body = json.loads(self.read_body().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.send_json(
                {"ok": False, "error": f"请求 JSON 无效：{exc}"},
                HTTPStatus.BAD_REQUEST,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        if not isinstance(body, dict):
            self.send_json(
                {"ok": False, "error": "请求 JSON 必须是对象"},
                HTTPStatus.BAD_REQUEST,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        try:
            payload = run_admin_account_model_test(
                account_id,
                str(body.get("model") or "grok-4.5"),
            )
        except ValueError as exc:
            self.send_json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.BAD_REQUEST,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        except Exception as exc:
            message = str(exc).strip()[:500] or "Console 暂时不可用"
            self.send_json(
                {"ok": False, "error": f"模型测试失败：{message}"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"Cache-Control": "no-store, max-age=0"},
            )
            return
        self.send_json(
            payload,
            extra_headers={"Cache-Control": "no-store, max-age=0"},
        )

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        is_account_model_test = bool(
            re.fullmatch(
                re.escape(ADMIN_PATH) + r"/api/accounts/[1-9][0-9]*/model-test",
                path,
            )
        )
        is_account_import = path == f"{ADMIN_PATH}/api/accounts/import"
        if (
            path
            in {"/api/claim", "/api/claim-batch", f"{ADMIN_PATH}/auto-replenish"}
            or is_account_model_test
            or is_account_import
        ):
            self._do_POST()
            return
        with MANIFEST_LOCK:
            self._do_POST()

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        if is_sensitive_request_path(parsed.path):
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == f"{ADMIN_PATH}/login":
            self.handle_login()
            return
        if parsed.path == f"{ADMIN_PATH}/api/accounts/import":
            if not is_admin(self):
                self.send_json(
                    {"ok": False, "error": "请先登录。"},
                    HTTPStatus.UNAUTHORIZED,
                    {"Cache-Control": "no-store, max-age=0"},
                )
                return
            self.handle_admin_accounts_import(parsed)
            return
        account_model_test = re.fullmatch(
            re.escape(ADMIN_PATH) + r"/api/accounts/([1-9][0-9]*)/model-test",
            parsed.path,
        )
        if account_model_test:
            if not is_admin(self):
                self.send_json(
                    {"ok": False, "error": "请先登录。"},
                    HTTPStatus.UNAUTHORIZED,
                    {"Cache-Control": "no-store, max-age=0"},
                )
                return
            self.handle_admin_account_model_test(int(account_model_test.group(1)))
            return
        if parsed.path == f"{ADMIN_PATH}/announcement":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_announcement()
            return
        if parsed.path == f"{ADMIN_PATH}/auto-replenish":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_auto_replenish()
            return
        if parsed.path == f"{ADMIN_PATH}/upload":
            if not is_admin(self):
                if self.headers.get("X-Requested-With") == "fetch":
                    self.send_json({"error": "请先登录。"}, HTTPStatus.UNAUTHORIZED)
                    return
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_upload()
            return
        if parsed.path == f"{ADMIN_PATH}/cards/issue":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_issue_cards()
            return
        if parsed.path == f"{ADMIN_PATH}/cards/bulk":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_bulk_cards()
            return
        if parsed.path == f"{ADMIN_PATH}/cleanup":
            if not is_admin(self):
                self.send_html(login_page("请先登录。"), HTTPStatus.UNAUTHORIZED)
                return
            self.handle_cleanup()
            return
        if parsed.path == f"{ADMIN_PATH}/api/key":
            if not is_admin(self):
                self.send_json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
                return
            self.send_json({"ok": True, "key": generate_unique_card_key(load_manifest())})
            return
        if parsed.path == "/api/internal/bundles":
            self.handle_internal_bundle()
            return
        if parsed.path == "/api/claim":
            self.handle_claim()
            return
        if parsed.path == "/api/claim-batch":
            self.handle_claim_batch()
            return
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def handle_internal_bundle(self) -> None:
        if not INTERNAL_API_TOKEN:
            self.send_json({"error": "internal API is disabled"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        authorization = self.headers.get("Authorization") or ""
        if not hmac.compare_digest(authorization, f"Bearer {INTERNAL_API_TOKEN}"):
            self.send_json(
                {"error": "unauthorized"},
                HTTPStatus.UNAUTHORIZED,
                {"WWW-Authenticate": "Bearer"},
            )
            return
        try:
            payload = json.loads(self.read_body().decode("utf-8"))
        except ValueError as exc:
            self.send_json({"error": f"invalid JSON: {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            self.send_json({"error": "JSON body must be an object"}, HTTPStatus.BAD_REQUEST)
            return
        if str(payload.get("pack_mode") or "bundle") != "bundle":
            self.send_json({"error": "only bundle pack_mode is supported"}, HTTPStatus.BAD_REQUEST)
            return
        source_files = payload.get("files")
        if not isinstance(source_files, list) or not 1 <= len(source_files) <= 500:
            self.send_json({"error": "files must contain 1 to 500 items"}, HTTPStatus.BAD_REQUEST)
            return

        json_files: list[tuple[str, bytes]] = []
        try:
            for index, item in enumerate(source_files, 1):
                if not isinstance(item, dict) or "data" not in item:
                    raise ValueError(f"files[{index - 1}] must contain filename and data")
                filename = safe_filename(str(item.get("filename") or f"account-{index}.json"))
                raw = json.dumps(item["data"], ensure_ascii=False, indent=2).encode("utf-8")
                json_files.append((filename, raw))
        except (TypeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        manifest = load_manifest()
        title = str(payload.get("title") or "账户交付包").strip()[:80] or "账户交付包"
        requested_key = normalize_key(str(payload.get("key") or ""))
        request_fingerprint = delivery_request_fingerprint(title, json_files)
        if requested_key:
            existing_id = str(manifest.setdefault("keys", {}).get(requested_key) or "")
            existing_bundle = manifest.setdefault("bundles", {}).get(existing_id)
            if existing_id and isinstance(existing_bundle, dict):
                existing_fingerprint = str(existing_bundle.get("request_fingerprint") or "")
                if existing_fingerprint and not hmac.compare_digest(existing_fingerprint, request_fingerprint):
                    self.send_json(
                        {"error": "requested key already belongs to a different bundle request"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                zip_path = ZIP_DIR / f"{existing_id}.zip"
                if not zip_path.exists():
                    self.send_json(
                        {"error": "requested key exists but its ZIP is missing"},
                        HTTPStatus.CONFLICT,
                    )
                    return
                self.send_json(
                    {
                        "ok": True,
                        "bundle_id": existing_id,
                        "key": requested_key,
                        "claim_path": f"/?key={quote(requested_key, safe='')}",
                        "file_count": int(existing_bundle.get("file_count") or len(json_files)),
                        "size": int(existing_bundle.get("size") or 0),
                        "title": str(existing_bundle.get("title") or title),
                        "idempotent_replay": True,
                    }
                )
                return
        try:
            card_key = reserve_delivery_key(manifest, set(), requested_key)
            bundle_id = create_delivery_bundle(
                manifest,
                title=title,
                card_key=card_key,
                json_files=json_files,
                errors=[],
                bundle_id=deterministic_bundle_id(card_key) if requested_key else "",
                request_fingerprint=request_fingerprint if requested_key else "",
            )
            save_manifest(manifest)
        except Exception as exc:
            self.send_json({"error": f"bundle creation failed: {exc}"}, HTTPStatus.BAD_REQUEST)
            return

        bundle = manifest["bundles"][bundle_id]
        self.send_json(
            {
                "ok": True,
                "bundle_id": bundle_id,
                "key": card_key,
                "claim_path": f"/?key={quote(card_key, safe='')}",
                "file_count": int(bundle.get("file_count") or len(json_files)),
                "size": int(bundle.get("size") or 0),
                "title": title,
            },
            HTTPStatus.CREATED,
        )

    def handle_login(self) -> None:
        body = self.read_body().decode("utf-8", errors="replace")
        password = (parse_qs(body).get("password") or [""])[0]
        if hmac.compare_digest(password, ADMIN_PASSWORD):
            self.redirect(ADMIN_PATH, cookie_action="make")
            return
        self.send_html(login_page("密码错误。"), HTTPStatus.UNAUTHORIZED)

    def handle_issue_cards(self) -> None:
        params = parse_qs(self.read_body().decode("utf-8", errors="replace"))
        try:
            count = int((params.get("count") or ["0"])[0])
        except ValueError:
            count = 0
        batch = str((params.get("batch") or [""])[0]).strip()[:80]
        platform = str((params.get("platform") or ["grok"])[0]).strip().lower()
        required_model = str((params.get("required_model") or [""])[0]).strip()[:100]
        if not batch:
            batch = time.strftime("batch-%Y%m%d-%H%M%S", time.localtime())
        manifest = load_manifest()
        try:
            keys = issue_cards(
                manifest,
                count,
                batch,
                platform=platform,
                required_model=required_model,
            )
            save_manifest(manifest)
        except Exception as exc:
            notice = quote(f"生成预发行卡失败：{exc}", safe="")
            self.redirect(f"{ADMIN_PATH}?notice={notice}#cards", status=303)
            return
        notice = quote(
            f"已生成 {len(keys)} 张 {normalize_card_platform(platform)} 预发行卡，批次：{batch}",
            safe="",
        )
        self.redirect(
            f"{ADMIN_PATH}?notice={notice}&issued_batch={quote(batch, safe='')}"
            f"&issued_platform={quote(normalize_card_platform(platform), safe='')}#cards",
            status=303,
        )

    def handle_bulk_cards(self) -> None:
        params = parse_qs(
            self.read_body().decode("utf-8", errors="replace"),
            keep_blank_values=True,
        )
        mode = str((params.get("mode") or ["revoke"])[0]).strip().lower()
        card_keys = parse_card_keys_input((params.get("card_keys") or [""])[0])
        if not card_keys:
            notice = quote("没有识别到可处理的卡密", safe="")
            self.redirect(f"{ADMIN_PATH}?notice={notice}#cards", status=303)
            return

        manifest = load_manifest()
        existing = [key for key in card_keys if isinstance(manifest.get("cards", {}).get(key), dict)]
        backup_path = backup_manifest(f"cards-{mode}") if existing else None
        try:
            result = batch_manage_cards(manifest, card_keys, mode=mode)
            if result["changed"]:
                save_manifest(manifest)
        except Exception as exc:
            notice = quote(f"批量卡密操作失败：{exc}", safe="")
            self.redirect(f"{ADMIN_PATH}?notice={notice}#cards", status=303)
            return

        parts = [
            f"识别 {result['requested']} 张",
            f"作废 {result['revoked']} 张",
            f"删除未领取卡 {result['deleted']} 张",
        ]
        if result["claimed_preserved"]:
            parts.append(f"已领取卡仅作废并保留记录 {result['claimed_preserved']} 张")
        if result["busy"]:
            parts.append(f"正在分配暂未处理 {result['busy']} 张")
        if result["missing"]:
            parts.append(f"不存在 {result['missing']} 张")
        if result["unchanged"]:
            parts.append(f"无需重复处理 {result['unchanged']} 张")
        if backup_path:
            parts.append(f"已备份 {backup_path.name}")
        notice = quote("；".join(parts), safe="")
        self.redirect(f"{ADMIN_PATH}?notice={notice}#cards", status=303)

    def handle_auto_replenish(self) -> None:
        params = parse_qs(self.read_body().decode("utf-8", errors="replace"), keep_blank_values=True)
        enabled = (params.get("enabled") or ["0"])[-1] == "1"
        try:
            threshold = min(max(int((params.get("threshold") or ["100"])[0]), 1), 100000)
            replenish_count = min(max(int((params.get("replenish_count") or ["100"])[0]), 1), 5000)
            result = console_auto_replenish_request(
                method="POST",
                payload={
                    "enabled": enabled,
                    "threshold": threshold,
                    "replenish_count": replenish_count,
                },
            )
            config = result.get("config") if isinstance(result.get("config"), dict) else {}
            saved_threshold = int(config.get("threshold") or threshold)
            saved_count = int(config.get("replenish_count") or replenish_count)
            if config.get("enabled"):
                notice = f"自动补货已开启：库存低于 {saved_threshold} 时补货 {saved_count}"
            else:
                notice = "自动补货已关闭；当前运行任务不会中断"
        except Exception as exc:
            notice = f"自动补货设置失败：{exc}"
        self.redirect(f"{ADMIN_PATH}?notice={quote(notice, safe='')}#inventory", status=303)

    def handle_announcement(self) -> None:
        body = self.read_body().decode("utf-8", errors="replace")
        params = parse_qs(body, keep_blank_values=True)
        action = (params.get("announcement_action") or [""])[0]
        title = (params.get("title") or ["公告"])[0]
        content = (params.get("content") or [""])[0]
        enabled = bool((params.get("enabled") or [""])[0]) and action != "disable"
        pool_closed = bool((params.get("pool_closed") or [""])[0])
        if action == "disable":
            enabled = False
        save_announcement(
            {
                "enabled": enabled,
                "title": title,
                "content": content,
                "pool_closed": pool_closed,
            }
        )
        notice = "取件页公告已保存" if enabled and content.strip() else "取件页公告已关闭"
        self.redirect(f"{ADMIN_PATH}?notice={quote(notice, safe='')}#announcement", status=303)

    def handle_upload(self) -> None:
        wants_json = (
            self.headers.get("X-Requested-With") == "fetch"
            or "application/json" in (self.headers.get("Accept") or "")
        )
        if int(self.headers.get("Content-Length") or 0) > MAX_UPLOAD_BYTES:
            if wants_json:
                self.send_json({"error": "上传失败：总大小超过限制。"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            self.send_html(admin_page(self, "上传失败：总大小超过限制。"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        title = str(form.getfirst("title") or "JSON 交付包").strip()[:80] or "JSON 交付包"
        pack_mode = str(form.getfirst("pack_mode") or "split").strip().lower()
        if pack_mode not in {"bundle", "split", "pool"}:
            pack_mode = "split"
        custom_key = normalize_key(str(form.getfirst("key") or ""))

        manifest = load_manifest()
        uploaded_names = active_uploaded_filename_map(manifest)
        uploaded_identities = active_uploaded_identity_map(manifest)
        files_field = form["files"] if "files" in form else []
        fields = files_field if isinstance(files_field, list) else [files_field]
        json_files: list[tuple[str, bytes]] = []
        skipped: list[str] = []
        existing_result_ids: list[str] = []
        existing_result_seen: set[str] = set()
        current_duplicate_refs: list[dict[str, str]] = []
        seen_upload_names: set[str] = set()
        current_identities: dict[str, str] = {}
        def remember_existing(existing_item: dict | None) -> None:
            bundle_id = str((existing_item or {}).get("bundle_id") or "").strip()
            if bundle_id and bundle_id not in existing_result_seen:
                existing_result_seen.add(bundle_id)
                existing_result_ids.append(bundle_id)

        for index, field in enumerate(fields, 1):
            filename = safe_filename(getattr(field, "filename", "") or f"data-{index}.json")
            filename_marker = filename.casefold()
            skip_current = False
            if filename_marker in seen_upload_names:
                skipped.append(f"{filename}: 本次上传文件名重复")
                skip_current = True
            else:
                seen_upload_names.add(filename_marker)
            existing = uploaded_names.get(filename_marker)
            if existing:
                existing_label = existing_delivery_label(existing)
                skipped.append(f"{filename}: 文件名已存在（{existing_label}），请勿重复上传")
                remember_existing(existing)
                skip_current = True
            raw = field.file.read() if getattr(field, "file", None) else b""
            if not raw:
                skipped.append(f"{filename}: 空文件")
                continue
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except Exception as exc:
                skipped.append(f"{filename}: JSON 无效 ({exc})")
                continue
            identity = delivery_identity_from_json(payload, filename=filename)
            identity_key = str(identity.get("key") or "").strip().lower()
            if identity_key:
                existing_identity = uploaded_identities.get(identity_key)
                if existing_identity:
                    existing_label = existing_delivery_label(existing_identity)
                    skipped.append(
                        f"{filename}: 数据已存在（{identity.get('label') or identity_key}，{existing_label}）"
                    )
                    remember_existing(existing_identity)
                    skip_current = True
                previous_filename = current_identities.get(identity_key)
                if previous_filename:
                    current_duplicate_refs.append(
                        {
                            "filename": filename,
                            "previous_filename": previous_filename,
                            "label": str(identity.get("label") or identity_key),
                        }
                    )
                    skip_current = True
            if skip_current:
                continue
            if identity_key:
                current_identities[identity_key] = filename
            json_files.append((filename, raw))

        if not json_files:
            message = (
                f"没有可导入的新文件，已跳过 {len(skipped)} 个重复/无效文件"
                if skipped
                else "上传失败：没有可导入的新文件"
            )
            if existing_result_ids:
                results_value = quote(",".join(existing_result_ids), safe=",")
                notice = f"{message}，已显示 {len(existing_result_ids)} 个已存在卡密"
                redirect_url = f"{ADMIN_PATH}?results={results_value}&notice={quote(notice, safe='')}"
                if wants_json:
                    self.send_json(
                        {
                            "ok": True,
                            "bundle_ids": existing_result_ids,
                            "existing_bundle_ids": existing_result_ids,
                            "notice": notice,
                            "redirect_url": redirect_url,
                            "skipped": skipped,
                            "skipped_count": len(skipped),
                        }
                    )
                    return
                self.redirect(redirect_url, status=303)
                return
            if wants_json:
                self.send_json(
                    {
                        "error": message,
                        "skipped": skipped,
                        "skipped_count": len(skipped),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_html(admin_page(self, message), HTTPStatus.BAD_REQUEST)
            return

        result_ids: list[str] = []
        accepted_keys_by_filename: dict[str, str] = {}
        try:
            reserved_keys: set[str] = set()
            if pack_mode == "split":
                split_plan: list[tuple[str, bytes, str]] = []
                suffix_width = max(2, len(str(len(json_files))))
                for index, (filename, raw) in enumerate(json_files, 1):
                    preferred_key = f"{custom_key}-{index:0{suffix_width}d}" if custom_key else ""
                    split_plan.append((filename, raw, reserve_delivery_key(manifest, reserved_keys, preferred_key)))
            elif pack_mode == "bundle":
                bundle_card_key = reserve_delivery_key(manifest, reserved_keys, custom_key)
        except Exception as exc:
            message = f"上传失败：{exc}"
            if wants_json:
                self.send_json(
                    {
                        "error": message,
                        "skipped": skipped,
                        "skipped_count": len(skipped),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_html(admin_page(self, message), HTTPStatus.BAD_REQUEST)
            return

        try:
            if pack_mode == "split":
                for index, (filename, raw, card_key) in enumerate(split_plan, 1):
                    file_title = title if len(json_files) == 1 else f"{title} - {Path(filename).stem}"
                    bundle_id_for_file = create_delivery_bundle(
                        manifest,
                        title=file_title[:80],
                        card_key=card_key,
                        json_files=[(filename, raw)],
                        errors=skipped if index == 1 else [],
                    )
                    result_ids.append(bundle_id_for_file)
                    accepted_keys_by_filename[filename] = card_key
            elif pack_mode == "bundle":
                card_key = bundle_card_key
                result_ids.append(
                    create_delivery_bundle(
                        manifest,
                        title=title,
                        card_key=card_key,
                        json_files=json_files,
                        errors=skipped,
                    )
                )
                for filename, _ in json_files:
                    accepted_keys_by_filename[filename] = card_key
            else:
                for index, (filename, raw) in enumerate(json_files, 1):
                    file_title = title if len(json_files) == 1 else f"{title} - {Path(filename).stem}"
                    bundle_id_for_file = create_delivery_bundle(
                        manifest,
                        title=file_title[:80],
                        card_key="",
                        json_files=[(filename, raw)],
                        errors=skipped if index == 1 else [],
                    )
                    result_ids.append(bundle_id_for_file)
        except Exception as exc:
            message = f"上传失败：{exc}"
            if wants_json:
                self.send_json(
                    {
                        "error": message,
                        "skipped": skipped,
                        "skipped_count": len(skipped),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_html(admin_page(self, message), HTTPStatus.BAD_REQUEST)
            return
        for duplicate in current_duplicate_refs:
            filename = duplicate.get("filename") or "未命名文件"
            previous_filename = duplicate.get("previous_filename") or ""
            label = duplicate.get("label") or filename
            card_key = accepted_keys_by_filename.get(previous_filename, "")
            if card_key:
                skipped.append(
                    f"{filename}: 本次上传数据重复（{label}），对应本次卡密 {card_key}，来源 {previous_filename}"
                )
            else:
                skipped.append(f"{filename}: 本次上传数据重复（{label}，已在 {previous_filename}）")
        save_manifest(manifest)

        bundle_id = result_ids[0]
        display_result_ids = [*result_ids, *existing_result_ids]
        if pack_mode == "split":
            notice = f"已按单文件独立模式生成 {len(result_ids)} 个卡密"
        elif pack_mode == "pool":
            notice = f"已入库存 {len(result_ids)} 个交付包，用户取件时自动分配最早库存"
        else:
            notice = "上传打包完成，已生成卡密和下载地址"
        if existing_result_ids:
            notice = f"{notice}，另显示 {len(existing_result_ids)} 个已存在卡密"
        if current_duplicate_refs:
            notice = f"{notice}，{len(current_duplicate_refs)} 个本次重复文件已标注对应卡密"
        if skipped:
            notice = f"{notice}，已跳过 {len(skipped)} 个重复/无效文件"
        results_value = quote(",".join(display_result_ids), safe=",")
        redirect_url = f"{ADMIN_PATH}?results={results_value}&notice={quote(notice, safe='')}"
        if wants_json:
            self.send_json(
                {
                    "ok": True,
                    "bundle_id": bundle_id,
                    "bundle_ids": result_ids,
                    "display_bundle_ids": display_result_ids,
                    "existing_bundle_ids": existing_result_ids,
                    "notice": notice,
                    "redirect_url": redirect_url,
                    "skipped": skipped,
                    "skipped_count": len(skipped),
                }
            )
            return
        self.redirect(
            redirect_url,
            status=303,
        )

    def handle_cleanup(self) -> None:
        body = self.read_body().decode("utf-8", errors="replace")
        params = parse_qs(body)
        action = (params.get("action") or [""])[0]
        manifest = load_manifest()
        bundles = manifest.setdefault("bundles", {})
        manifest.setdefault("keys", {})
        deleted = 0
        backup_path: Path | None = None

        def ensure_cleanup_backup() -> Path | None:
            nonlocal backup_path
            if backup_path is None:
                backup_path = backup_manifest(f"cleanup-{action or 'unknown'}")
            return backup_path

        if action == "delete":
            bundle_id = (params.get("bundle_id") or [""])[0]
            if bundle_id in bundles:
                ensure_cleanup_backup()
            deleted = delete_bundle_from_manifest(manifest, bundle_id, include_related=True)
        elif action == "delete_selected":
            target_ids: list[str] = []
            seen_ids: set[str] = set()
            for raw_value in [*params.get("bundle_id", []), *params.get("bundle_ids", [])]:
                for raw_id in str(raw_value or "").replace("\n", ",").split(","):
                    bundle_id = raw_id.strip()
                    if bundle_id and bundle_id in bundles and bundle_id not in seen_ids:
                        seen_ids.add(bundle_id)
                        target_ids.append(bundle_id)
            if not target_ids:
                notice = "未选择要删除的交付包"
                self.redirect(f"{ADMIN_PATH}?notice={quote(notice, safe='')}", status=303)
                return
            ensure_cleanup_backup()
            for bundle_id in target_ids:
                deleted += delete_bundle_from_manifest(manifest, bundle_id, include_related=True)
        elif action == "clear_unused":
            targets = [
                bundle_id
                for bundle_id, bundle in list(bundles.items())
                if not bundle.get("bound_at")
            ]
            if targets:
                ensure_cleanup_backup()
            for bundle_id in targets:
                deleted += delete_bundle_from_manifest(manifest, bundle_id)
        elif action == "clear_replaced":
            targets = [
                bundle_id
                for bundle_id, bundle in list(bundles.items())
                if bundle.get("replaced_by")
            ]
            if targets:
                ensure_cleanup_backup()
            for bundle_id in targets:
                deleted += delete_bundle_from_manifest(manifest, bundle_id)
        elif action == "clear_expired":
            targets = [
                bundle_id
                for bundle_id, bundle in list(bundles.items())
                if bundle_is_expired(bundle)
            ]
            if targets:
                ensure_cleanup_backup()
            for bundle_id in targets:
                deleted += delete_bundle_from_manifest(manifest, bundle_id)
        elif action == "clear_all":
            if bundles:
                ensure_cleanup_backup()
            deleted = len(bundles)
            for card in manifest.setdefault("cards", {}).values():
                if isinstance(card, dict) and card.get("status") != "void":
                    card["status"] = "void"
                    card["voided_at"] = now_text()
                    card["bundle_id"] = ""
            manifest["bundles"] = {}
            manifest["keys"] = {}
        else:
            notice = "\u672a\u8bc6\u522b\u7684\u6e05\u7406\u52a8\u4f5c"
            self.redirect(f"{ADMIN_PATH}?notice={quote(notice, safe='')}", status=303)
            return

        orphan_files = clear_orphan_zips(manifest)
        save_manifest(manifest)
        notice = f"\u5df2\u6e05\u7406 {deleted} \u4e2a\u4ea4\u4ed8\u5305\uff0c{orphan_files} \u4e2a\u4ea4\u4ed8\u6587\u4ef6"
        if backup_path:
            notice = f"{notice}\uff0cmanifest \u5df2\u5907\u4efd\uff1a{backup_path.name}"
        self.redirect(f"{ADMIN_PATH}?notice={quote(notice, safe='')}", status=303)

    def handle_claim(self) -> None:
        admin_request = False
        client_headers = clear_client_cookie_headers()
        try:
            payload = json.loads(self.read_body().decode("utf-8-sig"))
        except Exception:
            self.send_json({"error": "请求 JSON 无效"}, HTTPStatus.BAD_REQUEST, client_headers)
            return
        key = normalize_key(str(payload.get("key") or ""))
        if not key:
            self.send_json({"error": "请输入卡密 KEY"}, HTTPStatus.BAD_REQUEST, client_headers)
            return
        with try_card_lock(key) as acquired:
            if not acquired:
                self.send_json(
                    {"error": "该卡密正在分配，请稍候后重试", "retryable": True},
                    HTTPStatus.CONFLICT,
                    client_headers,
                )
                return
            manifest = load_manifest()
            card = (manifest.get("cards") or {}).get(key)
            if not isinstance(card, dict) or card.get("status") == "void":
                self.send_json({"error": "卡密不存在或已作废"}, HTTPStatus.NOT_FOUND, client_headers)
                return
            if load_announcement().get("pool_closed") and not str(card.get("bundle_id") or ""):
                self.send_json({"error": pool_closed_message()}, HTTPStatus.FORBIDDEN, client_headers)
                return
            try:
                bundle_id, bundle = provision_card_bundle(manifest, key)
            except Exception as exc:
                error_text = str(exc)
                no_verified_stock = "no recently verified account is available" in error_text.lower()
                self.send_json(
                    {
                        "error": (
                            "暂无近期验活可交付账号，请等待后台验活完成后重试"
                            if no_verified_stock
                            else f"账号分配打包失败：{exc}"
                        )
                    },
                    HTTPStatus.CONFLICT if no_verified_stock else HTTPStatus.SERVICE_UNAVAILABLE,
                    client_headers,
                )
                return
            if not bundle_payload_exists(bundle_id, bundle):
                self.send_json({"error": "交付文件不存在，请管理员重新生成"}, HTTPStatus.NOT_FOUND, client_headers)
                return
            if not admin_request:
                with MANIFEST_LOCK:
                    sync_manifest(manifest, load_manifest())
                    card = (manifest.get("cards") or {}).get(key)
                    bundle = manifest.setdefault("bundles", {}).get(bundle_id)
                    if not isinstance(card, dict) or not isinstance(bundle, dict):
                        self.send_json({"error": "卡密交付记录不存在"}, HTTPStatus.NOT_FOUND, client_headers)
                        return
                    claimed_at = str(bundle.get("bound_at") or "")
                    if claimed_at and bundle_is_expired(bundle):
                        expires_text = bundle_expires_text(bundle)
                        self.send_json(
                            {"error": f"该卡密首次取件后 24 小时有效，已于 {expires_text or '当前时间'} 过期"},
                            HTTPStatus.FORBIDDEN,
                            client_headers,
                        )
                        return
                    if not claimed_at:
                        if load_announcement().get("pool_closed"):
                            self.send_json(
                                {"error": pool_closed_message()},
                                HTTPStatus.FORBIDDEN,
                                client_headers,
                            )
                            return
                        bundle["bound_client"] = ""
                        bundle["bound_at"] = now_text()
                        bundle["bound_ip"] = request_ip(self)
                        bundle["bound_user_agent"] = (self.headers.get("User-Agent") or "")[:180]
                        card["status"] = "claimed"
                        card["claimed_at"] = str(card.get("claimed_at") or now_text())
                        save_manifest(manifest)
                    elif bundle.get("bound_client"):
                        bundle["bound_client"] = ""
                        save_manifest(manifest)

        download_path = bundle_download_path(bundle_id, bundle)
        if not admin_request:
            download_path += f"?key={quote(key, safe='')}"
        download_name = bundle_download_name(bundle_id, bundle)
        download_format = "json" if download_name.lower().endswith(".json") else "zip"
        sub_download_url = ""
        sub_file_name = ""
        cockpit_download_url = ""
        grokcli_download_url = ""
        grokcli_file_name = ""
        if download_format == "json" and normalize_card_platform(bundle.get("platform")) == "grok":
            sub_path = bundle_download_path(bundle_id, bundle)
            sub_path += f"?format=sub2api&key={quote(key, safe='')}"
            sub_download_url = absolute_url(self, sub_path)
            sub_file_name = "SUB2API-grok-account.json"
            try:
                source_path = bundle_payload_path(bundle_id, bundle)
                if source_path:
                    sub_document = json.loads(source_path.read_text(encoding="utf-8-sig"))
                    sub_file_name = sub2api_filename(sub_document)
            except (OSError, ValueError):
                pass
            cockpit_path = bundle_download_path(bundle_id, bundle)
            cockpit_path += f"?format=cockpit&key={quote(key, safe='')}"
            cockpit_download_url = absolute_url(self, cockpit_path)
            grokcli_path = bundle_download_path(bundle_id, bundle)
            grokcli_path += f"?format={GROKCLI2API_VARIANT}&key={quote(key, safe='')}"
            grokcli_download_url = absolute_url(self, grokcli_path)
            variant_meta = (
                bundle.get("variants", {}).get(GROKCLI2API_VARIANT, {})
                if isinstance(bundle.get("variants"), dict)
                else {}
            )
            grokcli_file_name = safe_filename(
                str(variant_meta.get("file_name") or "grokcli-2api-auth-account.json"),
                "grokcli-2api-auth-account.json",
            )
        expires_text = bundle_expires_text(bundle)
        self.send_json(
            {
                "ok": True,
                "key": bundle.get("key"),
                "title": bundle.get("title"),
                "file_count": bundle.get("file_count"),
                "size": bundle.get("size"),
                "size_text": format_size(int(bundle.get("size") or 0)),
                "zip_name": bundle.get("zip_name") or download_name,
                "file_name": download_name,
                "download_format": download_format,
                "download_url": absolute_url(self, download_path),
                "sub_download_url": sub_download_url,
                "sub_file_name": sub_file_name,
                "cockpit_download_url": cockpit_download_url,
                "cockpit_file_name": "auth.json" if cockpit_download_url else "",
                "grokcli_download_url": grokcli_download_url,
                "grokcli_file_name": grokcli_file_name,
                "bound_at": bundle.get("bound_at") or "",
                "expires_at": expires_text,
            },
            extra_headers=client_headers,
        )

    def handle_claim_batch(self) -> None:
        client_headers = clear_client_cookie_headers()
        try:
            payload = json.loads(self.read_body().decode("utf-8-sig"))
        except Exception:
            self.send_json({"error": "请求 JSON 无效"}, HTTPStatus.BAD_REQUEST, client_headers)
            return

        raw_keys = payload.get("keys")
        if isinstance(raw_keys, list):
            keys = [str(item) for item in raw_keys]
        else:
            raw_text = str(raw_keys or payload.get("key") or "")
            for sep in [",", "，", ";", "；", "\r"]:
                raw_text = raw_text.replace(sep, "\n")
            keys = [item.strip() for item in raw_text.splitlines() if item.strip()]

        with lock_card_keys(keys):
            manifest = load_manifest()
            items, errors, _changed = prepare_claim_items(self, manifest, keys)
        if errors and not items:
            statuses = [int(item.get("status") or HTTPStatus.BAD_REQUEST) for item in errors]
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if int(HTTPStatus.SERVICE_UNAVAILABLE) in statuses
                else HTTPStatus.FORBIDDEN
                if int(HTTPStatus.FORBIDDEN) in statuses
                else HTTPStatus.NOT_FOUND
                if int(HTTPStatus.NOT_FOUND) in statuses
                else HTTPStatus.BAD_REQUEST
            )
            self.send_json(
                {
                    "error": "；".join(str(item.get("message") or "") for item in errors if item.get("message")),
                    "errors": [str(item.get("message") or "") for item in errors if item.get("message")],
                },
                status,
                client_headers,
            )
            return

        batch = create_batch_download(items)
        total_files = sum(
            4
            if bundle_payload_path(item["bundle_id"], item["bundle"])
            and bundle_payload_path(item["bundle_id"], item["bundle"]).suffix.lower() == ".json"
            and normalize_card_platform(item["bundle"].get("platform")) == "grok"
            else int(item["bundle"].get("file_count") or len(item["bundle"].get("files") or []) or 0)
            for item in items
        )
        total_size = sum(int(item["bundle"].get("size") or 0) for item in items)
        download_path = f"/download-batch/{quote(str(batch.get('token') or ''), safe='')}.zip"
        self.send_json(
            {
                "ok": True,
                "batch": True,
                "partial": bool(errors),
                "key_count": len(items),
                "keys": [item["key"] for item in items],
                "failed_count": len(errors),
                "errors": [str(item.get("message") or "") for item in errors if item.get("message")],
                "title": f"批量取件 {len(items)} 个卡密",
                "file_count": total_files,
                "size": total_size,
                "size_text": format_size(total_size),
                "zip_name": batch.get("zip_name"),
                "download_url": absolute_url(self, download_path),
                "expires_in": BATCH_DOWNLOAD_TTL_SECONDS,
            },
            HTTPStatus.MULTI_STATUS if errors else HTTPStatus.OK,
            client_headers,
        )

    def serve_batch_download(self, parsed) -> None:
        cleanup_batch_downloads()
        token = unquote(Path(parsed.path).name).removesuffix(".zip")
        batch = BATCH_DOWNLOADS.get(token)
        if not batch:
            self.send_html(user_page("批量下载链接已过期，请重新输入卡密取件。"), HTTPStatus.NOT_FOUND)
            return
        if float(batch.get("expires_at") or 0) < time.time():
            BATCH_DOWNLOADS.pop(token, None)
            self.send_html(user_page("批量下载链接已过期，请重新输入卡密取件。"), HTTPStatus.GONE)
            return

        client_headers = clear_client_cookie_headers()

        manifest = load_manifest()
        errors: list[str] = []
        for item in batch.get("items") or []:
            key = str(item.get("key") or "")
            bundle_id = str(item.get("bundle_id") or "")
            bundle = manifest.get("bundles", {}).get(bundle_id) or {}
            if not bundle or manifest.get("keys", {}).get(key) != bundle_id:
                errors.append(f"{key}: 卡密记录不存在")
                continue
            if not bundle_payload_exists(bundle_id, bundle):
                errors.append(f"{key}: 交付文件不存在")
                continue
            if not bundle.get("bound_at"):
                errors.append(f"{key}: 请重新输入卡密取件后再下载")
                continue
            if bundle_is_expired(bundle):
                errors.append(f"{key}: 卡密取件有效期已过")
                continue
        if errors:
            self.send_html(user_page("；".join(errors)), HTTPStatus.FORBIDDEN, client_headers)
            return

        try:
            data = build_batch_zip_bytes(manifest, batch)
        except Exception as exc:
            self.send_html(user_page(f"批量打包失败：{exc}"), HTTPStatus.INTERNAL_SERVER_ERROR, client_headers)
            return

        filename = str(batch.get("zip_name") or "DG-BATCH.zip")
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.types_map.get(".zip", "application/zip"))
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        for key, value in client_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def serve_download(self, parsed) -> None:
        bundle_id = unquote(Path(parsed.path).stem)
        query = parse_qs(parsed.query)
        manifest = load_manifest()
        bundle = manifest.get("bundles", {}).get(bundle_id)
        payload_path = bundle_payload_path(bundle_id, bundle)
        if not bundle or not payload_path or not payload_path.exists():
            self.send_html(user_page("下载地址不存在或已失效。"), HTTPStatus.NOT_FOUND)
            return
        admin_download = is_admin(self) and (query.get("admin") or [""])[0] == "1"
        client_headers: dict[str, str] = {}
        if not admin_download:
            client_headers = clear_client_cookie_headers()
            key = normalize_key((query.get("key") or [""])[0])
            expected_id = manifest.get("keys", {}).get(key)
            if expected_id != bundle_id:
                self.send_html(
                    user_page("请先在本浏览器输入卡密取件，不能直接打开下载地址。"),
                    HTTPStatus.FORBIDDEN,
                    client_headers,
                )
                return
            if not bundle.get("bound_at"):
                if load_announcement().get("pool_closed"):
                    self.send_html(
                        user_page(pool_closed_message(), initial_key=key),
                        HTTPStatus.FORBIDDEN,
                        client_headers,
                    )
                    return
                self.send_html(
                    user_page("卡密还没有完成首次取件，请先回到取件页输入卡密。", initial_key=key),
                    HTTPStatus.FORBIDDEN,
                    client_headers,
                )
                return
            if bundle_is_expired(bundle):
                expires_text = bundle_expires_text(bundle)
                self.send_html(
                    user_page(f"该卡密首次取件后 24 小时有效，已于 {expires_text or '当前时间'} 过期。"),
                    HTTPStatus.FORBIDDEN,
                    client_headers,
                )
                return
        data = payload_path.read_bytes()
        filename = bundle_download_name(bundle_id, bundle)
        variant = str((query.get("format") or [""])[0]).strip().lower()
        if variant in GROKCLI2API_VARIANT_ALIASES:
            if payload_path.suffix.lower() != ".json" or normalize_card_platform(bundle.get("platform")) != "grok":
                self.send_html(user_page("该交付文件不支持 GrokCLI-2API 格式。"), HTTPStatus.BAD_REQUEST, client_headers)
                return
            variant_path = bundle_variant_json_path(bundle_id, GROKCLI2API_VARIANT)
            variant_meta = (
                bundle.get("variants", {}).get(GROKCLI2API_VARIANT, {})
                if isinstance(bundle.get("variants"), dict)
                else {}
            )
            try:
                document = json.loads(data.decode("utf-8-sig"))
                if variant_path and variant_path.exists():
                    data = variant_path.read_bytes()
                else:
                    data = json.dumps(
                        grokcli_2api_payload(document), ensure_ascii=False, indent=2
                    ).encode("utf-8")
                filename = safe_filename(
                    str(variant_meta.get("file_name") or grokcli_2api_filename(document)),
                    "grokcli-2api-auth-account.json",
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self.send_html(user_page(f"生成 GrokCLI-2API 导入文件失败：{exc}"), HTTPStatus.INTERNAL_SERVER_ERROR, client_headers)
                return
        elif variant == "cockpit":
            if payload_path.suffix.lower() != ".json" or normalize_card_platform(bundle.get("platform")) != "grok":
                self.send_html(user_page("该交付文件不支持 Cockpit 格式。"), HTTPStatus.BAD_REQUEST, client_headers)
                return
            try:
                document = json.loads(data.decode("utf-8-sig"))
                data = json.dumps(
                    cockpit_auth_payload(document), ensure_ascii=False, indent=2
                ).encode("utf-8")
            except (UnicodeDecodeError, ValueError) as exc:
                self.send_html(user_page(f"生成 Cockpit auth.json 失败：{exc}"), HTTPStatus.INTERNAL_SERVER_ERROR, client_headers)
                return
            filename = "auth.json"
        elif variant in {"sub", "sub2api"}:
            if payload_path.suffix.lower() != ".json" or normalize_card_platform(bundle.get("platform")) != "grok":
                self.send_html(user_page("该交付文件不支持 Sub2API 格式。"), HTTPStatus.BAD_REQUEST, client_headers)
                return
            try:
                document = json.loads(data.decode("utf-8-sig"))
                data = json.dumps(
                    sub2api_payload(document), ensure_ascii=False, indent=2
                ).encode("utf-8")
            except (UnicodeDecodeError, ValueError) as exc:
                self.send_html(user_page(f"生成 Sub2API 导入文件失败：{exc}"), HTTPStatus.INTERNAL_SERVER_ERROR, client_headers)
                return
            filename = sub2api_filename(document)
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
            if payload_path.suffix.lower() == ".json"
            else mimetypes.types_map.get(".zip", "application/zip"),
        )
        self.send_header("X-DownloadGate-Version", APP_VERSION)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        for header, value in client_headers.items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    ensure_dirs()
    manifest = load_manifest()
    migrated = migrate_existing_bundle_json(manifest)
    if migrated:
        save_manifest(manifest)
        print(f"Migrated {migrated} bundle(s) to flat CPA JSON")
    host = os.environ.get("DOWNLOAD_GATE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("DOWNLOAD_GATE_PORT", "18787"))
    server = ThreadingHTTPServer((host, port), DownloadGateHandler)
    print(f"DownloadGate v{APP_VERSION} running: http://{host}:{port}")
    print(f"Admin page: http://{host}:{port}{ADMIN_PATH}")
    print(f"Admin password file: {ADMIN_PASSWORD_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()

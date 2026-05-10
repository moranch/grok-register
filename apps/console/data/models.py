"""
SQLModel 数据模型定义。

对应 design.md §4 Data Models。
覆盖：tasks / accounts / register_events / proxies / mailbox_providers /
      settings / exporter_states / sync_jobs
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_str() -> str:
    return _utcnow().isoformat()


# ─── Tasks ───────────────────────────────────────────────────────────────────


class TaskModel(SQLModel, table=False):
    """注册任务表。"""
    __tablename__ = "tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(default="")
    platform: str = Field(default="grok", index=True)
    status: str = Field(default="queued", index=True)  # queued/running/stopping/completed/failed/stopped/partial
    executor_type: str = Field(default="headless")
    target_count: int = Field(default=1)
    completed_count: int = Field(default=0)
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    skipped_count: int = Field(default=0)
    last_error: str = Field(default="")
    config_json: str = Field(default="{}")
    params_json: str = Field(default="{}")  # {engine_id, extra, selected_exporters, ...}
    task_dir: str = Field(default="")
    console_path: str = Field(default="")
    notes: str = Field(default="")
    created_at: str = Field(default_factory=_utcnow_str)
    updated_at: str = Field(default_factory=_utcnow_str)


# ─── Accounts ────────────────────────────────────────────────────────────────


class AccountModel(SQLModel, table=False):
    """账号资产表。"""
    __tablename__ = "accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="grok", index=True)
    email: str = Field(default="", index=True)
    password: str = Field(default="")
    sso: str = Field(default="")  # 主 token（兼容旧字段）
    user_id: str = Field(default="")
    proxy_url: str = Field(default="")
    lifecycle_status: str = Field(default="active", index=True)  # active/trial/expired/suspended
    plan_state: str = Field(default="unknown")  # unknown/free/pro/plus
    validity_status: str = Field(default="unknown", index=True)  # valid/invalid/unknown/remote_missing
    extra_json: str = Field(default="{}")  # 平台特定数据（accessToken/refreshToken/clientId/...）
    exporter_status_json: str = Field(default="{}")  # {exporter_id: {status, last_pushed_at, message}}
    last_error: str = Field(default="")
    last_checked_at: Optional[str] = Field(default=None)
    task_id: Optional[int] = Field(default=None, index=True)
    created_at: str = Field(default_factory=_utcnow_str)
    updated_at: str = Field(default_factory=_utcnow_str)


# ─── Register Events ─────────────────────────────────────────────────────────


class RegisterEventModel(SQLModel, table=False):
    """注册事件表（日志 + 生命周期事件 + Exporter 事件）。"""
    __tablename__ = "register_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: Optional[int] = Field(default=None, index=True)
    account_id: Optional[int] = Field(default=None, index=True)
    platform: str = Field(default="", index=True)
    kind: str = Field(default="", index=True)
    # kind 枚举：success / failure / exporter_push_ok / exporter_push_failed /
    #           trial_warning / lifecycle_timeout / lifecycle_invalid / refresh_ok /
    #           sync_progress / batch_sync_ok / batch_sync_failed / backfill_ok / backfill_failed
    email: str = Field(default="")
    error: str = Field(default="")
    payload_json: str = Field(default="{}")
    mailbox_provider_id: Optional[int] = Field(default=None)
    proxy_url: str = Field(default="")
    created_at: str = Field(default_factory=_utcnow_str)


# ─── Proxies ─────────────────────────────────────────────────────────────────


class ProxyModel(SQLModel, table=False):
    """代理池表。"""
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(default="", index=True)
    label: str = Field(default="")
    enabled: bool = Field(default=True, index=True)
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    consecutive_failures: int = Field(default=0)
    created_at: str = Field(default_factory=_utcnow_str)
    updated_at: str = Field(default_factory=_utcnow_str)


# ─── Mailbox Providers ───────────────────────────────────────────────────────


class MailboxProviderModel(SQLModel, table=False):
    """邮箱 Provider 配置表。"""
    __tablename__ = "mailbox_providers"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(default="")
    provider_type: str = Field(default="tmail", index=True)
    # provider_type 枚举：tmail/duckmail/moemail/laoudo/cloudflare_worker/
    #                     freemail/testmail/tempmail_lol/duckduckgo/custom
    enabled: bool = Field(default=True, index=True)
    config_json: str = Field(default="{}")  # provider 专属配置
    success_count: int = Field(default=0)
    failure_count: int = Field(default=0)
    consecutive_failures: int = Field(default=0)
    created_at: str = Field(default_factory=_utcnow_str)
    updated_at: str = Field(default_factory=_utcnow_str)


# ─── Settings ────────────────────────────────────────────────────────────────


class SettingModel(SQLModel, table=False):
    """全局配置表（key-value 形式）。"""
    __tablename__ = "settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(default="", index=True, sa_column_kwargs={"unique": True})
    value: str = Field(default="")
    updated_at: str = Field(default_factory=_utcnow_str)


# ─── Exporter States ─────────────────────────────────────────────────────────


class ExporterStateModel(SQLModel, table=False):
    """Exporter 推送状态表。"""
    __tablename__ = "exporter_states"

    id: Optional[int] = Field(default=None, primary_key=True)
    exporter_id: str = Field(default="", index=True)
    account_id: int = Field(default=0, index=True)
    status: str = Field(default="pending", index=True)  # pending/pushed/failed
    message: str = Field(default="")
    last_pushed_at: Optional[str] = Field(default=None)
    created_at: str = Field(default_factory=_utcnow_str)


# ─── Sync Jobs ───────────────────────────────────────────────────────────────


class SyncJobModel(SQLModel, table=False):
    """批量状态同步 / auth-file 补传任务表。"""
    __tablename__ = "sync_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(default="", index=True)  # batch_status_sync / backfill_remote_auth
    platform: str = Field(default="", index=True)
    status: str = Field(default="queued", index=True)  # queued/running/completed/failed
    total: int = Field(default=0)
    current: int = Field(default=0)
    ok_count: int = Field(default=0)
    fail_count: int = Field(default=0)
    filter_json: str = Field(default="{}")
    error: str = Field(default="")
    created_at: str = Field(default_factory=_utcnow_str)
    updated_at: str = Field(default_factory=_utcnow_str)

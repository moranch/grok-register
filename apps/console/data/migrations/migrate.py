"""
数据库迁移管理器。

由于项目使用 SQLite + SQLModel，采用简单的版本化迁移方案：
- 每个迁移是一个 SQL 文件或 Python 函数。
- 通过 _migrations 表记录已执行的版本。
- lifespan 启动时自动执行未应用的迁移。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# 迁移定义：(version, description, sql_statements)
MIGRATIONS: List[Tuple[str, str, List[str]]] = [
    (
        "2.0.0",
        "初始化多平台架构表结构",
        [
            # tasks 表新增字段（如果从旧版升级）
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT 'grok',
                status TEXT NOT NULL DEFAULT 'queued',
                executor_type TEXT NOT NULL DEFAULT 'headless',
                target_count INTEGER NOT NULL DEFAULT 1,
                completed_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                config_json TEXT NOT NULL DEFAULT '{}',
                params_json TEXT NOT NULL DEFAULT '{}',
                task_dir TEXT NOT NULL DEFAULT '',
                console_path TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # accounts 表
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT 'grok',
                email TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                sso TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                proxy_url TEXT NOT NULL DEFAULT '',
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                plan_state TEXT NOT NULL DEFAULT 'unknown',
                validity_status TEXT NOT NULL DEFAULT 'unknown',
                extra_json TEXT NOT NULL DEFAULT '{}',
                exporter_status_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                task_id INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # register_events 表
            """
            CREATE TABLE IF NOT EXISTS register_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                account_id INTEGER,
                platform TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                mailbox_provider_id INTEGER,
                proxy_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # proxies 表
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # mailbox_providers 表
            """
            CREATE TABLE IF NOT EXISTS mailbox_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                provider_type TEXT NOT NULL DEFAULT 'tmail',
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}',
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # settings 表
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # exporter_states 表
            """
            CREATE TABLE IF NOT EXISTS exporter_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exporter_id TEXT NOT NULL DEFAULT '',
                account_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                message TEXT NOT NULL DEFAULT '',
                last_pushed_at TEXT,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # sync_jobs 表
            """
            CREATE TABLE IF NOT EXISTS sync_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                total INTEGER NOT NULL DEFAULT 0,
                current INTEGER NOT NULL DEFAULT 0,
                ok_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                filter_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            # 索引
            "CREATE INDEX IF NOT EXISTS idx_tasks_platform ON tasks(platform)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            "CREATE INDEX IF NOT EXISTS idx_accounts_platform ON accounts(platform)",
            "CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email)",
            "CREATE INDEX IF NOT EXISTS idx_accounts_lifecycle ON accounts(lifecycle_status)",
            "CREATE INDEX IF NOT EXISTS idx_accounts_validity ON accounts(validity_status)",
            "CREATE INDEX IF NOT EXISTS idx_events_task ON register_events(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_account ON register_events(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_kind ON register_events(kind)",
            "CREATE INDEX IF NOT EXISTS idx_events_platform ON register_events(platform)",
            "CREATE INDEX IF NOT EXISTS idx_proxies_url ON proxies(url)",
            "CREATE INDEX IF NOT EXISTS idx_proxies_enabled ON proxies(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_mailbox_type ON mailbox_providers(provider_type)",
            "CREATE INDEX IF NOT EXISTS idx_exp_states_exporter ON exporter_states(exporter_id)",
            "CREATE INDEX IF NOT EXISTS idx_exp_states_account ON exporter_states(account_id)",
            "CREATE INDEX IF NOT EXISTS idx_exp_states_status ON exporter_states(status)",
            "CREATE INDEX IF NOT EXISTS idx_sync_jobs_kind ON sync_jobs(kind)",
            "CREATE INDEX IF NOT EXISTS idx_sync_jobs_status ON sync_jobs(status)",
            # _migrations 表自身
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """,
        ],
    ),
]


def run_migrations(db_path: str) -> List[str]:
    """
    执行所有未应用的迁移。

    Args:
        db_path: SQLite 数据库文件路径。

    Returns:
        已应用的版本列表。
    """
    # 确保目录存在
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 确保 _migrations 表存在
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()

    # 获取已应用的版本
    cursor.execute("SELECT version FROM _migrations")
    applied = {row[0] for row in cursor.fetchall()}

    # 执行未应用的迁移
    newly_applied = []
    for version, description, statements in MIGRATIONS:
        if version in applied:
            continue

        logger.info("[Migration] 应用迁移 %s: %s", version, description)
        for sql in statements:
            sql = sql.strip()
            if sql:
                try:
                    cursor.execute(sql)
                except sqlite3.OperationalError as e:
                    # 表/索引已存在等非致命错误
                    if "already exists" in str(e):
                        continue
                    raise

        # 记录已应用
        from datetime import datetime, timezone
        cursor.execute(
            "INSERT INTO _migrations (version, description, applied_at) VALUES (?, ?, ?)",
            (version, description, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        newly_applied.append(version)
        logger.info("[Migration] 迁移 %s 完成", version)

    conn.close()
    return newly_applied

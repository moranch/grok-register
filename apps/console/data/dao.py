"""
数据访问层（DAO）：CRUD + 聚合查询。

对应 Requirement 4 AC1/AC2, Requirement 7 AC5, Requirement 11 AC1-AC3。
使用 SQLModel + sqlite3。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, SQLModel, create_engine, select, col, func

from data.models import (
    AccountModel,
    ExporterStateModel,
    MailboxProviderModel,
    ProxyModel,
    RegisterEventModel,
    SettingModel,
    SyncJobModel,
    TaskModel,
)

logger = logging.getLogger(__name__)

# 数据库路径
DB_PATH = os.getenv("DATABASE_URL", "sqlite:///data/console.db")
engine = create_engine(DB_PATH, echo=False)


def init_db() -> None:
    """初始化数据库（创建所有表）。"""
    SQLModel.metadata.create_all(engine)
    logger.info("[DB] 数据库初始化完成: %s", DB_PATH)


def get_session() -> Session:
    return Session(engine)


# ─── Accounts ────────────────────────────────────────────────────────────────


def list_accounts(
    platform: str = "",
    lifecycle_status: str = "",
    plan_state: str = "",
    validity_status: str = "",
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[AccountModel], int]:
    """分页查询账号列表 + 总数。"""
    with get_session() as s:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if lifecycle_status:
            q = q.where(AccountModel.lifecycle_status == lifecycle_status)
        if plan_state:
            q = q.where(AccountModel.plan_state == plan_state)
        if validity_status:
            q = q.where(AccountModel.validity_status == validity_status)
        if keyword:
            q = q.where(
                col(AccountModel.email).contains(keyword)
                | col(AccountModel.sso).contains(keyword)
            )

        # 总数
        count_q = select(func.count()).select_from(q.subquery())
        total = s.exec(count_q).one()

        # 分页
        q = q.order_by(col(AccountModel.id).desc()).offset(offset).limit(limit)
        items = s.exec(q).all()

    return list(items), total


def get_account_summary() -> Dict[str, Any]:
    """账号汇总统计。"""
    with get_session() as s:
        total = s.exec(select(func.count()).select_from(AccountModel)).one()

        # 按 lifecycle_status 分组
        lifecycle_rows = s.exec(
            select(AccountModel.lifecycle_status, func.count())
            .group_by(AccountModel.lifecycle_status)
        ).all()

        # 按 plan_state 分组
        plan_rows = s.exec(
            select(AccountModel.plan_state, func.count())
            .group_by(AccountModel.plan_state)
        ).all()

        # 按 validity_status 分组
        validity_rows = s.exec(
            select(AccountModel.validity_status, func.count())
            .group_by(AccountModel.validity_status)
        ).all()

        # 按 platform 分组
        platform_rows = s.exec(
            select(AccountModel.platform, func.count())
            .group_by(AccountModel.platform)
        ).all()

    return {
        "total": total,
        "by_lifecycle_status": {r[0]: r[1] for r in lifecycle_rows},
        "by_plan_state": {r[0]: r[1] for r in plan_rows},
        "by_validity_status": {r[0]: r[1] for r in validity_rows},
        "by_platform": {r[0]: r[1] for r in platform_rows},
    }


def save_account(account_data: Dict[str, Any]) -> AccountModel:
    """保存账号。"""
    with get_session() as s:
        model = AccountModel(**{
            k: v for k, v in account_data.items()
            if k in AccountModel.__fields__
        })
        s.add(model)
        s.commit()
        s.refresh(model)
        return model


def update_account(account_id: int, updates: Dict[str, Any]) -> Optional[AccountModel]:
    """更新账号字段。"""
    with get_session() as s:
        account = s.get(AccountModel, account_id)
        if account is None:
            return None
        for k, v in updates.items():
            if hasattr(account, k):
                setattr(account, k, v)
        account.updated_at = datetime.now(timezone.utc).isoformat()
        s.add(account)
        s.commit()
        s.refresh(account)
        return account


def delete_accounts(ids: List[int]) -> int:
    """批量删除账号。"""
    with get_session() as s:
        deleted = 0
        for aid in ids:
            account = s.get(AccountModel, aid)
            if account:
                s.delete(account)
                deleted += 1
        s.commit()
        return deleted


# ─── Register Events ─────────────────────────────────────────────────────────


def write_event(event_data: Dict[str, Any]) -> RegisterEventModel:
    """写入注册事件。"""
    with get_session() as s:
        model = RegisterEventModel(**{
            k: v for k, v in event_data.items()
            if k in RegisterEventModel.__fields__
        })
        if "payload" in event_data and isinstance(event_data["payload"], dict):
            model.payload_json = json.dumps(event_data["payload"], ensure_ascii=False)
        s.add(model)
        s.commit()
        s.refresh(model)
        return model


# ─── Stats ───────────────────────────────────────────────────────────────────


def get_stats_overview(days: int = 7) -> Dict[str, Any]:
    """全局统计概览。"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_session() as s:
        total = s.exec(
            select(func.count()).select_from(RegisterEventModel)
            .where(RegisterEventModel.created_at >= cutoff)
        ).one()

        success = s.exec(
            select(func.count()).select_from(RegisterEventModel)
            .where(RegisterEventModel.created_at >= cutoff)
            .where(RegisterEventModel.kind == "success")
        ).one()

        failure = s.exec(
            select(func.count()).select_from(RegisterEventModel)
            .where(RegisterEventModel.created_at >= cutoff)
            .where(RegisterEventModel.kind == "failure")
        ).one()

        account_count = s.exec(
            select(func.count()).select_from(AccountModel)
        ).one()

    rate = (success / total * 100) if total > 0 else 0.0
    return {
        "total_events": total,
        "success_count": success,
        "failure_count": failure,
        "success_rate": round(rate, 2),
        "account_count": account_count,
        "days": days,
    }


def get_stats_by_platform() -> List[Dict[str, Any]]:
    """按平台统计成功率。"""
    with get_session() as s:
        rows = s.exec(
            select(
                RegisterEventModel.platform,
                RegisterEventModel.kind,
                func.count(),
            )
            .where(RegisterEventModel.kind.in_(["success", "failure"]))
            .group_by(RegisterEventModel.platform, RegisterEventModel.kind)
        ).all()

    # 聚合
    platforms: Dict[str, Dict[str, int]] = {}
    for platform, kind, count in rows:
        if platform not in platforms:
            platforms[platform] = {"success": 0, "failure": 0}
        platforms[platform][kind] = count

    results = []
    for p, stats in platforms.items():
        total = stats["success"] + stats["failure"]
        rate = (stats["success"] / total * 100) if total > 0 else 0.0
        results.append({
            "platform": p,
            "success": stats["success"],
            "failure": stats["failure"],
            "total": total,
            "success_rate": round(rate, 2),
        })
    return results


def get_stats_errors(days: int = 7, top_n: int = 10) -> List[Dict[str, Any]]:
    """错误 Top N 聚合。"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with get_session() as s:
        rows = s.exec(
            select(RegisterEventModel.error, func.count().label("cnt"))
            .where(RegisterEventModel.created_at >= cutoff)
            .where(RegisterEventModel.kind == "failure")
            .where(RegisterEventModel.error != "")
            .group_by(RegisterEventModel.error)
            .order_by(func.count().desc())
            .limit(top_n)
        ).all()

    return [{"error": r[0], "count": r[1]} for r in rows]


# ─── Proxies ─────────────────────────────────────────────────────────────────


def list_proxies() -> List[ProxyModel]:
    """获取所有代理。"""
    with get_session() as s:
        return list(s.exec(select(ProxyModel)).all())


def save_proxy(data: Dict[str, Any]) -> ProxyModel:
    """保存代理。"""
    with get_session() as s:
        model = ProxyModel(**{k: v for k, v in data.items() if k in ProxyModel.__fields__})
        s.add(model)
        s.commit()
        s.refresh(model)
        return model


# ─── Mailbox Providers ───────────────────────────────────────────────────────


def list_mailbox_providers() -> List[MailboxProviderModel]:
    """获取所有邮箱 Provider。"""
    with get_session() as s:
        return list(s.exec(select(MailboxProviderModel)).all())


# ─── Settings ────────────────────────────────────────────────────────────────


def get_all_settings() -> Dict[str, str]:
    """获取全量 settings。"""
    with get_session() as s:
        rows = s.exec(select(SettingModel)).all()
    return {r.key: r.value for r in rows}


def upsert_setting(key: str, value: str) -> None:
    """upsert 一条 setting。"""
    with get_session() as s:
        existing = s.exec(select(SettingModel).where(SettingModel.key == key)).first()
        if existing:
            existing.value = value
            existing.updated_at = datetime.now(timezone.utc).isoformat()
            s.add(existing)
        else:
            s.add(SettingModel(key=key, value=value))
        s.commit()


# ─── Tasks ───────────────────────────────────────────────────────────────────


def list_tasks(platform: str = "", status: str = "", limit: int = 50) -> List[TaskModel]:
    """获取任务列表。"""
    with get_session() as s:
        q = select(TaskModel)
        if platform:
            q = q.where(TaskModel.platform == platform)
        if status:
            q = q.where(TaskModel.status == status)
        q = q.order_by(col(TaskModel.id).desc()).limit(limit)
        return list(s.exec(q).all())


def save_task(data: Dict[str, Any]) -> TaskModel:
    """保存任务。"""
    with get_session() as s:
        model = TaskModel(**{k: v for k, v in data.items() if k in TaskModel.__fields__})
        s.add(model)
        s.commit()
        s.refresh(model)
        return model


def update_task(task_id: int, updates: Dict[str, Any]) -> Optional[TaskModel]:
    """更新任务。"""
    with get_session() as s:
        task = s.get(TaskModel, task_id)
        if task is None:
            return None
        for k, v in updates.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = datetime.now(timezone.utc).isoformat()
        s.add(task)
        s.commit()
        s.refresh(task)
        return task

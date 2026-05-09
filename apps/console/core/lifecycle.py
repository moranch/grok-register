"""
账号生命周期管理 Worker。

对应 Requirement 8 AC1-AC6。

- 6h 有效性检测
- 12h token 续期
- 24h trial 预警
- 单次 _tick 5 分钟熔断
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LifecycleState:
    """Lifecycle Worker 运行时状态。"""
    enabled: bool = True
    running: bool = False
    check_hours: int = 6
    refresh_hours: int = 12
    trial_warn_hours: int = 1
    last_check_at: Optional[float] = None
    last_refresh_at: Optional[float] = None
    last_result: str = ""
    last_error: str = ""
    checked_count: int = 0
    refreshed_count: int = 0
    warned_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "check_hours": self.check_hours,
            "refresh_hours": self.refresh_hours,
            "last_check_at": self.last_check_at,
            "last_refresh_at": self.last_refresh_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "checked_count": self.checked_count,
            "refreshed_count": self.refreshed_count,
            "warned_count": self.warned_count,
        }


class LifecycleWorker:
    """
    常驻协程：定时检测账号有效性、刷新 token、trial 预警。

    使用方式：
        worker = LifecycleWorker(...)
        asyncio.create_task(worker.start())
        # 停止时
        await worker.stop()
    """

    TICK_TIMEOUT = 300  # 单次检测超时 5 分钟（Req 8 AC4）

    def __init__(
        self,
        check_hours: int = 6,
        refresh_hours: int = 12,
        get_accounts: Optional[Callable] = None,
        get_platform: Optional[Callable] = None,
        update_account: Optional[Callable] = None,
        write_event: Optional[Callable] = None,
    ):
        """
        Args:
            check_hours: 有效性检测周期（小时）。
            refresh_hours: token 续期周期（小时）。
            get_accounts: 获取待检测账号列表的回调。
            get_platform: 根据 platform name 获取 BasePlatform 实例的回调。
            update_account: 更新账号字段的回调。
            write_event: 写入 register_events 的回调。
        """
        self.state = LifecycleState(check_hours=check_hours, refresh_hours=refresh_hours)
        self._get_accounts = get_accounts
        self._get_platform = get_platform
        self._update_account = update_account
        self._write_event = write_event
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动 Worker 循环。"""
        self._stop_event.clear()
        self._task = asyncio.current_task()
        logger.info("[Lifecycle] Worker 启动, check_hours=%d", self.state.check_hours)

        while not self._stop_event.is_set():
            if self.state.enabled:
                await self._safe_tick()

            # 等待下一个周期（或被 stop）
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.state.check_hours * 3600,
                )
                break  # stop_event 被 set
            except asyncio.TimeoutError:
                continue  # 超时 = 到了下一个周期

        logger.info("[Lifecycle] Worker 已停止")

    async def stop(self) -> None:
        """停止 Worker。"""
        self._stop_event.set()

    async def trigger_check_now(self) -> Dict[str, Any]:
        """立即触发一次检测（无视周期，Req 8 AC6）。"""
        return await self._safe_tick()

    async def _safe_tick(self) -> Dict[str, Any]:
        """带超时保护的单次检测。"""
        self.state.running = True
        self.state.last_error = ""

        try:
            result = await asyncio.wait_for(
                self._tick(),
                timeout=self.TICK_TIMEOUT,
            )
            self.state.last_result = "ok"
            self.state.last_check_at = time.time()
            return result

        except asyncio.TimeoutError:
            # Req 8 AC4：5 分钟熔断
            self.state.last_error = "lifecycle_timeout"
            self.state.last_result = "timeout"
            logger.error("[Lifecycle] 单次检测超时 (%ds)，已熔断", self.TICK_TIMEOUT)
            return {"error": "lifecycle_timeout"}

        except Exception as exc:
            self.state.last_error = str(exc)
            self.state.last_result = "error"
            logger.exception("[Lifecycle] 检测异常: %s", exc)
            return {"error": str(exc)}

        finally:
            self.state.running = False

    async def _tick(self) -> Dict[str, Any]:
        """单次检测周期的核心逻辑。"""
        if not self._get_accounts or not self._get_platform:
            return {"skipped": "callbacks not configured"}

        accounts = self._get_accounts()
        checked = 0
        refreshed = 0
        warned = 0
        errors = 0

        now = time.time()
        should_refresh = (
            self.state.last_refresh_at is None
            or (now - self.state.last_refresh_at) >= self.state.refresh_hours * 3600
        )

        for account in accounts:
            try:
                platform = self._get_platform(account.get("platform", ""))
                if platform is None:
                    continue

                # 有效性检测
                is_valid = platform.check_validity(account)
                new_status = "valid" if is_valid else "invalid"
                if self._update_account:
                    self._update_account(account["id"], {
                        "validity_status": new_status,
                        "last_checked_at": time.time(),
                    })
                checked += 1

                # Token 续期（Req 8 AC2）
                caps = getattr(platform, "capabilities", None)
                if should_refresh and caps and caps.supports_refresh:
                    result = platform.refresh_token(account)
                    if result:
                        if self._update_account:
                            self._update_account(account["id"], {
                                "extra_json_patch": result,
                            })
                        refreshed += 1

                # Trial 预警（Req 8 AC3）
                extra = account.get("extra_json", {})
                if isinstance(extra, str):
                    import json
                    try:
                        extra = json.loads(extra)
                    except Exception:
                        extra = {}

                trial_end = extra.get("trial_end_time", 0)
                lifecycle_status = account.get("lifecycle_status", "")
                if (
                    lifecycle_status == "trial"
                    and trial_end
                    and (trial_end - now) < 86400  # 24 小时
                    and (trial_end - now) > 0
                ):
                    if self._write_event:
                        self._write_event({
                            "kind": "trial_warning",
                            "account_id": account["id"],
                            "platform": account.get("platform", ""),
                            "payload": {"trial_end_time": trial_end, "remaining_hours": round((trial_end - now) / 3600, 1)},
                        })
                    warned += 1

            except Exception as exc:
                errors += 1
                logger.warning("[Lifecycle] 账号 %s 检测失败: %s", account.get("id"), exc)

        if should_refresh:
            self.state.last_refresh_at = now

        self.state.checked_count += checked
        self.state.refreshed_count += refreshed
        self.state.warned_count += warned

        return {
            "checked": checked,
            "refreshed": refreshed,
            "warned": warned,
            "errors": errors,
        }

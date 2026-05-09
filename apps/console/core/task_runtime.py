"""
注册任务运行时：状态机 + SSE EventBus + stop/skip/熔断。

对应 Requirement 2 AC1-AC9。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── 任务状态枚举 ─────────────────────────────────────────────────────────────


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    PARTIAL = "partial"


# 合法的状态转换（单调递进，Req 2 AC2）
VALID_TRANSITIONS = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.STOPPED},
    TaskStatus.RUNNING: {TaskStatus.STOPPING, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED, TaskStatus.PARTIAL},
    TaskStatus.STOPPING: {TaskStatus.STOPPED, TaskStatus.PARTIAL},
}


# ─── SSE 事件 ─────────────────────────────────────────────────────────────────


@dataclass
class SSEEvent:
    """SSE 事件。"""
    event_type: str  # phase / round / mailbox_created / otp_received / turnstile_solved / success / error / done / sync_progress / exporter_push_ok / exporter_push_failed
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_sse_string(self) -> str:
        payload = json.dumps({**self.data, "type": self.event_type, "ts": self.timestamp}, ensure_ascii=False)
        return f"data: {payload}\n\n"


class EventBus:
    """
    SSE 事件总线：任务内部 emit 事件，前端通过 SSE stream 消费。

    支持多个订阅者（多个浏览器 tab 同时看同一个任务）。
    """

    def __init__(self):
        self._events: List[SSEEvent] = []
        self._subscribers: List[asyncio.Queue] = []
        self._done = False

    def emit(self, event_type: str, **data) -> None:
        """发射一个事件。"""
        event = SSEEvent(event_type=event_type, data=data)
        self._events.append(event)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def mark_done(self) -> None:
        """标记任务结束，通知所有订阅者。"""
        self._done = True
        for q in self._subscribers:
            try:
                q.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass

    async def subscribe(self, since: int = 0) -> AsyncGenerator[SSEEvent, None]:
        """
        订阅事件流。

        Args:
            since: 从第 N 个事件开始回放（用于断线重连）。
        """
        # 先回放历史
        for event in self._events[since:]:
            yield event

        if self._done:
            return

        # 实时订阅
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:  # sentinel = done
                    break
                yield event
        finally:
            self._subscribers.remove(queue)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def is_done(self) -> bool:
        return self._done


# ─── 任务控制信号 ─────────────────────────────────────────────────────────────


class StopTaskRequested(Exception):
    """任务被请求停止。"""
    pass


class SkipCurrentRequested(Exception):
    """当前轮被请求跳过。"""
    pass


class TaskControl:
    """
    任务控制对象：stop / skip-current / 熔断检查。

    每个注册轮在关键 checkpoint 调用 control.checkpoint() 检查是否需要中断。
    """

    def __init__(self, circuit_break_threshold: int = 0, round_timeout: int = 0):
        self._stop_requested = False
        self._skip_requested = False
        self._circuit_break_threshold = circuit_break_threshold
        self._round_timeout = round_timeout
        self._consecutive_failures = 0

    def request_stop(self) -> None:
        """请求停止任务（Req 2 AC4）。"""
        self._stop_requested = True

    def request_skip(self) -> None:
        """请求跳过当前轮（Req 2 AC5）。"""
        self._skip_requested = True

    def clear_skip(self) -> None:
        """清除 skip 标志（进入下一轮时）。"""
        self._skip_requested = False

    def report_failure(self) -> None:
        """上报一次失败（用于熔断计数）。"""
        self._consecutive_failures += 1

    def report_success(self) -> None:
        """上报一次成功（重置熔断计数）。"""
        self._consecutive_failures = 0

    def checkpoint(self) -> None:
        """
        检查点：在注册流程的关键位置调用。

        Raises:
            StopTaskRequested: 任务被请求停止。
            SkipCurrentRequested: 当前轮被请求跳过。
        """
        if self._stop_requested:
            raise StopTaskRequested("任务被手动停止")
        if self._skip_requested:
            raise SkipCurrentRequested("当前轮被手动跳过")

    @property
    def is_stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def should_circuit_break(self) -> bool:
        """是否应该熔断（Req 2 AC7）。"""
        if self._circuit_break_threshold <= 0:
            return False
        return self._consecutive_failures >= self._circuit_break_threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def round_timeout(self) -> int:
        return self._round_timeout


# ─── TaskRuntime ─────────────────────────────────────────────────────────────


@dataclass
class TaskState:
    """任务运行时状态。"""
    task_id: str
    status: TaskStatus = TaskStatus.QUEUED
    platform: str = ""
    engine_id: str = ""
    executor_type: str = "protocol"
    target_count: int = 1
    completed_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskRuntime:
    """
    任务运行时管理器。

    - 管理所有活跃任务的状态。
    - 提供 SSE EventBus。
    - 提供 stop / skip-current 控制。
    - 并发限流（max_concurrent_tasks 信号量）。
    """

    def __init__(self, max_concurrent_tasks: int = 3):
        self._tasks: Dict[str, TaskState] = {}
        self._event_buses: Dict[str, EventBus] = {}
        self._controls: Dict[str, TaskControl] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)
        self._max_concurrent = max_concurrent_tasks

    def create_task(
        self,
        task_id: str,
        platform: str,
        engine_id: str = "default",
        executor_type: str = "protocol",
        target_count: int = 1,
        circuit_break_threshold: int = 0,
        round_timeout: int = 0,
    ) -> TaskState:
        """创建一个新任务。"""
        state = TaskState(
            task_id=task_id,
            platform=platform,
            engine_id=engine_id,
            executor_type=executor_type,
            target_count=target_count,
        )
        self._tasks[task_id] = state
        self._event_buses[task_id] = EventBus()
        self._controls[task_id] = TaskControl(
            circuit_break_threshold=circuit_break_threshold,
            round_timeout=round_timeout,
        )
        return state

    def get_state(self, task_id: str) -> Optional[TaskState]:
        return self._tasks.get(task_id)

    def get_event_bus(self, task_id: str) -> Optional[EventBus]:
        return self._event_buses.get(task_id)

    def get_control(self, task_id: str) -> Optional[TaskControl]:
        return self._controls.get(task_id)

    def transition(self, task_id: str, new_status: TaskStatus) -> bool:
        """
        状态转换（单调递进）。

        Returns:
            True 表示转换成功，False 表示非法转换。
        """
        state = self._tasks.get(task_id)
        if state is None:
            return False

        allowed = VALID_TRANSITIONS.get(state.status, set())
        if new_status not in allowed:
            logger.warning(
                "[TaskRuntime] 非法状态转换: %s -> %s (task=%s)",
                state.status, new_status, task_id,
            )
            return False

        state.status = new_status
        state.updated_at = time.time()
        return True

    def request_stop(self, task_id: str) -> bool:
        """请求停止任务（Req 2 AC4）。"""
        control = self._controls.get(task_id)
        if control is None:
            return False
        control.request_stop()
        self.transition(task_id, TaskStatus.STOPPING)
        return True

    def request_skip(self, task_id: str) -> bool:
        """请求跳过当前轮（Req 2 AC5）。"""
        control = self._controls.get(task_id)
        if control is None:
            return False
        control.request_skip()
        return True

    def finalize_task(
        self,
        task_id: str,
        success_count: int = 0,
        failure_count: int = 0,
        skipped_count: int = 0,
        last_error: str = "",
    ) -> None:
        """任务结束时调用，确定最终状态并通知 EventBus。"""
        state = self._tasks.get(task_id)
        if state is None:
            return

        state.success_count = success_count
        state.failure_count = failure_count
        state.skipped_count = skipped_count
        state.completed_count = success_count + failure_count + skipped_count
        state.last_error = last_error

        control = self._controls.get(task_id)

        # 确定最终状态
        if control and control.is_stop_requested:
            final_status = TaskStatus.STOPPED
        elif control and control.should_circuit_break:
            final_status = TaskStatus.FAILED
            state.last_error = last_error or f"熔断：连续失败 {control.consecutive_failures} 次"
        elif failure_count > 0 and success_count == 0:
            final_status = TaskStatus.FAILED
        elif failure_count > 0 and success_count > 0:
            final_status = TaskStatus.PARTIAL
        else:
            final_status = TaskStatus.COMPLETED

        self.transition(task_id, final_status)

        # 通知 EventBus
        bus = self._event_buses.get(task_id)
        if bus:
            bus.emit("done", status=final_status.value, **{
                "success": success_count,
                "failed": failure_count,
                "skipped": skipped_count,
                "total": state.target_count,
            })
            bus.mark_done()

    def cleanup_finished(self, max_keep: int = 200) -> None:
        """清理已完成的任务（保留最近 max_keep 个）。"""
        finished = [
            (tid, s) for tid, s in self._tasks.items()
            if s.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.STOPPED, TaskStatus.PARTIAL)
        ]
        if len(finished) <= max_keep:
            return

        # 按 updated_at 排序，删除最旧的
        finished.sort(key=lambda x: x[1].updated_at)
        to_remove = finished[: len(finished) - max_keep]
        for tid, _ in to_remove:
            self._tasks.pop(tid, None)
            self._event_buses.pop(tid, None)
            self._controls.pop(tid, None)

    @property
    def active_count(self) -> int:
        """当前活跃（queued + running + stopping）任务数。"""
        return sum(
            1 for s in self._tasks.values()
            if s.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.STOPPING)
        )

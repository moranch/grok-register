"""
代理池：加权选择 + 成功/失败上报 + 自动禁用。

对应 Requirement 7 AC1-AC5。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DISABLE_THRESHOLD = 5  # 连续失败次数达到此值自动禁用


@dataclass
class ProxyEntry:
    """代理条目（对应 proxies 表的一行）。"""
    id: int = 0
    url: str = ""
    enabled: bool = True
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    label: str = ""


def _weighted_score(entry: ProxyEntry) -> float:
    """
    加权公式：(success + 1) / (success + failure + 2)

    Beta 分布期望值，无数据时先验 0.5。
    """
    return (entry.success_count + 1) / (entry.success_count + entry.failure_count + 2)


class ProxyPool:
    """
    代理池管理器。

    - 从外部加载代理列表（通常由 DAO 提供）。
    - 按加权公式随机挑选。
    - 上报成功/失败，连续失败达阈值自动禁用。
    """

    def __init__(
        self,
        proxies: Optional[List[ProxyEntry]] = None,
        disable_threshold: int = DISABLE_THRESHOLD,
        on_disable: Optional[Callable[[ProxyEntry], None]] = None,
    ):
        """
        Args:
            proxies: 初始代理列表。
            disable_threshold: 连续失败多少次自动禁用。
            on_disable: 禁用回调（用于持久化到数据库）。
        """
        self._proxies: Dict[str, ProxyEntry] = {}
        self._disable_threshold = disable_threshold
        self._on_disable = on_disable

        if proxies:
            for p in proxies:
                self._proxies[p.url] = p

    def load(self, proxies: List[ProxyEntry]) -> None:
        """重新加载代理列表。"""
        self._proxies = {p.url: p for p in proxies}

    def get_next(self) -> Optional[str]:
        """
        按加权随机从启用的代理中挑选一条。

        Returns:
            代理 URL，或 None（无可用代理时）。
        """
        enabled = [p for p in self._proxies.values() if p.enabled]
        if not enabled:
            return None

        weights = [_weighted_score(p) for p in enabled]
        total = sum(weights)
        if total <= 0:
            chosen = random.choice(enabled)
            return chosen.url

        chosen = random.choices(enabled, weights=weights, k=1)[0]
        return chosen.url

    def report_success(self, proxy_url: str) -> None:
        """上报代理使用成功。"""
        entry = self._proxies.get(proxy_url)
        if entry is None:
            return
        entry.success_count += 1
        entry.consecutive_failures = 0

    def report_failure(self, proxy_url: str) -> None:
        """
        上报代理使用失败。

        连续失败达到阈值时自动禁用（Req 7 AC3）。
        """
        entry = self._proxies.get(proxy_url)
        if entry is None:
            return
        entry.failure_count += 1
        entry.consecutive_failures += 1

        if entry.consecutive_failures >= self._disable_threshold:
            entry.enabled = False
            logger.warning(
                "[ProxyPool] 代理 '%s' 连续失败 %d 次，已自动禁用",
                proxy_url,
                entry.consecutive_failures,
            )
            if self._on_disable:
                self._on_disable(entry)

    def reset_stats(self, proxy_url: str) -> None:
        """重置指定代理的统计数据。"""
        entry = self._proxies.get(proxy_url)
        if entry is None:
            return
        entry.success_count = 0
        entry.failure_count = 0
        entry.consecutive_failures = 0
        entry.enabled = True

    def get_stats(self) -> List[Dict]:
        """返回所有代理的统计数据（用于 /api/stats/by-proxy）。"""
        results = []
        for entry in self._proxies.values():
            total = entry.success_count + entry.failure_count
            rate = (entry.success_count / total * 100) if total > 0 else 0.0
            results.append({
                "id": entry.id,
                "url": entry.url,
                "enabled": entry.enabled,
                "ok": entry.success_count,
                "fail": entry.failure_count,
                "total": total,
                "success_rate": round(rate, 2),
                "consecutive_failures": entry.consecutive_failures,
            })
        return results

    @property
    def available_count(self) -> int:
        """当前可用代理数量。"""
        return sum(1 for p in self._proxies.values() if p.enabled)

    @property
    def total_count(self) -> int:
        """代理总数。"""
        return len(self._proxies)

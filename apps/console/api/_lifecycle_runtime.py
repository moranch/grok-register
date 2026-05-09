"""
LifecycleWorker runtime：常驻后台线程，定期做"有效性检测"。

此模块从 app.py 中抽出，为 api/lifecycle.py 以及 lifespan 提供共享状态。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ._shared import (
    _request_with_optional_proxy,
    execute_no_return,
    merged_defaults,
    now_iso,
)


# ── 全局状态字典（路由读取 / 后台线程写入） ──────────────────────────

lifecycle_state: dict[str, Any] = {
    "last_check_at": "",
    "last_refresh_at": "",
    "last_result": "",
    "running": False,
}


def lifecycle_check_accounts() -> dict[str, Any]:
    """简单的有效性检测：调用推送接口，确认 token sink 可用。"""
    defaults = merged_defaults()
    api_conf = dict(defaults.get("api") or {})
    endpoint = str(api_conf.get("endpoint", "") or "").strip()
    if not endpoint:
        return {"ok": False, "message": "未配置 token sink（api.endpoint），无法检测"}
    try:
        response = _request_with_optional_proxy(endpoint, timeout=10)
        ok = response.status_code in {200, 401, 403, 405}
        return {
            "ok": ok,
            "message": f"HTTP {response.status_code}",
            "endpoint": endpoint,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc), "endpoint": endpoint}


def _lifecycle_loop() -> None:
    while True:
        try:
            defaults = merged_defaults()
            enabled = bool(defaults.get("lifecycle_enabled", False))
            hours = max(1, int(defaults.get("lifecycle_check_hours", 6) or 6))
            if enabled:
                lifecycle_state["running"] = True
                result = lifecycle_check_accounts()
                lifecycle_state["last_check_at"] = now_iso()
                lifecycle_state["last_result"] = result.get("message", "")
                lifecycle_state["running"] = False
                execute_no_return(
                    "UPDATE accounts SET last_checked_at = ? WHERE status = 'active'",
                    (now_iso(),),
                )
                time.sleep(hours * 3600)
            else:
                time.sleep(60)
        except Exception:
            time.sleep(60)


lifecycle_thread = threading.Thread(target=_lifecycle_loop, daemon=True)


def start_lifecycle_thread() -> None:
    if not lifecycle_thread.is_alive():
        lifecycle_thread.start()

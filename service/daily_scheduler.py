"""服务端日终调度：交易日 16:00 后补齐情绪、行业评分和个股因子。"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import common
import db

_STATE_KEY = "daily_finalize_status"
_ENABLED = os.getenv("DAILY_FINALIZE_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
_POLL_SECONDS = max(15, int(os.getenv("DAILY_FINALIZE_POLL_SECONDS", "30")))
_RETRY_MINUTES = (5, 15, 30, 30, 30, 60)
_TASK_STALE_MINUTES = 10
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(ZoneInfo(common.TZ))


def _save(state: dict[str, Any]) -> None:
    state["updated_at"] = common.now_str()
    db.set_config(_STATE_KEY, state)


def status() -> dict[str, Any]:
    value = db.get_config(_STATE_KEY)
    return value if isinstance(value, dict) else {"status": "waiting", "enabled": _ENABLED}


def _next_retry(attempt: int) -> str:
    minutes = _RETRY_MINUTES[min(max(attempt - 1, 0), len(_RETRY_MINUTES) - 1)]
    return (_now() + timedelta(minutes=minutes)).isoformat()


def _due(state: dict[str, Any]) -> bool:
    value = str(state.get("next_retry_at") or "")
    if not value:
        return True
    try:
        return _now() >= datetime.fromisoformat(value)
    except ValueError:
        return True


def _task_active_recent(task: dict[str, Any]) -> bool:
    """仅把心跳仍在租约内的任务视为活跃；超时任务交给 DB 原子释放并重认领。"""
    if not task.get("active"):
        return False
    try:
        heartbeat = datetime.strptime(str(task.get("heartbeat_at") or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.now() - heartbeat < timedelta(minutes=_TASK_STALE_MINUTES)


def _precompute_state(target: str) -> tuple[dict[str, Any], bool]:
    """按当前行业权重依赖读取可用日，避免旧合同任务被误判为成功。"""
    import precompute

    value = precompute.precompute_status({"limit": 5})
    return value, value.get("latest_usable_date") == target


def _run_once(target: str, state: dict[str, Any]) -> None:
    """执行或推进当天收口；所有步骤幂等，失败不发布伪 final。"""
    import precompute
    import sentiment

    attempt = int(state.get("attempt") or 0) + 1
    components = dict(state.get("components") or {})
    sentiment_result = sentiment.finalize_daily_sentiment(target)
    components["sentiment"] = sentiment_result

    pre_status = precompute.precompute_status({"limit": 5})
    pre_ok = pre_status.get("latest_usable_date") == target
    task = db.get_precompute_job() or {}
    if not pre_ok and not _task_active_recent(task):
        launch = precompute.precompute_daily_factors({"date": target})
        task = launch.get("job") or task
    components["precompute"] = {
        "status": "success" if pre_ok else (task.get("status") or "queued"),
        "job_id": task.get("job_id"), "progress": task.get("progress"),
        "stage": task.get("stage"), "message": task.get("message"),
        "dependency_hash": pre_status.get("dependency_hash"),
        "latest_usable_date": pre_status.get("latest_usable_date"),
    }
    complete = sentiment_result.get("status") == "success" and pre_ok
    scheduled_time = str(common.market_clock().get("final_ready_time") or "16:00")
    _save({
        "enabled": _ENABLED, "target_date": target, "scheduled_time": scheduled_time,
        "status": "success" if complete else "running" if _task_active_recent(task) else "retry_wait",
        "attempt": attempt, "components": components,
        "next_retry_at": None if complete else _next_retry(attempt),
    })


def _tick() -> None:
    clock = common.market_clock()
    if not _ENABLED or not clock.get("is_trading_day") or clock.get("phase") != "final":
        return
    target = str(clock.get("last_data_ready_date") or "")
    if not target or target != common.today_str():
        return
    state = status()
    if state.get("target_date") == target and state.get("status") == "success":
        pre_status, pre_ok = _precompute_state(target)
        recorded_hash = ((state.get("components") or {}).get("precompute") or {}).get("dependency_hash")
        current_hash = pre_status.get("dependency_hash")
        if pre_ok and (not recorded_hash or recorded_hash == current_hash):
            if not recorded_hash:
                components = dict(state.get("components") or {})
                components["precompute"] = {
                    **dict(components.get("precompute") or {}),
                    "dependency_hash": current_hash,
                    "latest_usable_date": pre_status.get("latest_usable_date"),
                }
                _save({**state, "components": components})
            return
        state = {**state, "status": "retry_wait", "next_retry_at": None}

    task = db.get_precompute_job() or {}
    if _task_active_recent(task):
        components = dict(state.get("components") or {})
        components["precompute"] = {
            "status": task.get("status"), "job_id": task.get("job_id"),
            "progress": task.get("progress"), "stage": task.get("stage"),
            "message": task.get("message"),
            "dependency_hash": ((components.get("precompute") or {}).get("dependency_hash")),
        }
        _save({**state, "enabled": _ENABLED, "target_date": target,
               "status": "running", "components": components})
        return

    components = dict(state.get("components") or {})
    sentiment_ok = (components.get("sentiment") or {}).get("status") == "success"
    pre_status, pre_ok = _precompute_state(target)
    if sentiment_ok and pre_ok:
        components["precompute"] = {
            "status": "success", "job_id": task.get("job_id"),
            "progress": task.get("progress"), "stage": task.get("stage"),
            "message": task.get("message"),
            "dependency_hash": pre_status.get("dependency_hash"),
            "latest_usable_date": pre_status.get("latest_usable_date"),
        }
        _save({**state, "enabled": _ENABLED, "target_date": target,
               "status": "success", "components": components,
               "next_retry_at": None, "error": None})
        return
    if state.get("target_date") == target and not _due(state):
        components["precompute"] = {
            "status": task.get("status") or "retry_wait",
            "job_id": task.get("job_id"), "progress": task.get("progress"),
            "stage": task.get("stage"), "message": task.get("message"),
            "dependency_hash": pre_status.get("dependency_hash"),
            "latest_usable_date": pre_status.get("latest_usable_date"),
        }
        _save({**state, "enabled": _ENABLED, "target_date": target,
               "status": "retry_wait", "components": components})
        return
    if state.get("target_date") != target or _due(state):
        _run_once(target, state if state.get("target_date") == target else {})


def _loop() -> None:
    while not _STOP.is_set():
        try:
            _tick()
        except Exception as exc:  # 调度循环不能影响主服务。
            state = status()
            attempt = int(state.get("attempt") or 0) + 1
            _save({**state, "enabled": _ENABLED, "status": "retry_wait", "attempt": attempt,
                   "error": f"{type(exc).__name__}: {exc}"[:500],
                   "next_retry_at": _next_retry(attempt)})
        _STOP.wait(_POLL_SECONDS)


def start() -> None:
    """启动单进程调度线程；DB 预计算租约负责防止重复发布。"""
    global _THREAD
    if not _ENABLED or (_THREAD and _THREAD.is_alive()):
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="daily-finalize", daemon=True)
    _THREAD.start()


def stop() -> None:
    _STOP.set()

"""独立运行审计日志：接口调用、回测运行与选股事件。

日志写入 DATA_DIR/logs，不进入功能注册表，也不影响 data_version。
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import common

SCENES = frozenset({"api", "backtest", "selection"})
SCENE_LABELS = {"api": "接口调用", "backtest": "回测记录", "selection": "选股记录"}
LOG_ROOT = common.DATA_DIR / "logs"
_LOCK = threading.Lock()
_SECRET_KEYS = frozenset({"key", "api_key", "admin_api_key", "user_api_key", "token",
                          "authorization", "password", "secret"})


def key_fingerprint(value: Optional[str]) -> Optional[str]:
    """只保存不可逆短指纹，不保存真实凭据。"""
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else None


def _sanitize(value: Any, depth: int = 0) -> Any:
    """限制日志体积并移除常见敏感字段。"""
    if depth > 7:
        return "<层级过深>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 8000 else value[:8000] + "…<已截断>"
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                output["<其余字段>"] = "已截断"
                break
            name = str(key)
            output[name] = "<已脱敏>" if name.lower() in _SECRET_KEYS else _sanitize(item, depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [_sanitize(item, depth + 1) for item in items[:500]]
        if len(items) > 500:
            output.append(f"<其余 {len(items) - 500} 项已截断>")
        return output
    return _sanitize(str(value), depth + 1)


def _ensure_layout() -> None:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    readme = LOG_ROOT / "README.txt"
    if not readme.exists():
        readme.write_text(
            "运行审计日志（JSONL，一行一条）\n"
            "api/：接口调用状态、耗时、角色和脱敏参数。\n"
            "backtest/：回测运行结果、样本门禁和调参依据。\n"
            "selection/：选股登记、重复、拒绝、补价和删除事件。\n"
            "目录按 YYYY/MM/DD.jsonl 分区，可由管理员日志下载接口导出。\n",
            encoding="utf-8",
        )


def append(scene: str, event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """追加一条日志；审计失败不得影响原业务。"""
    if scene not in SCENES:
        return None
    try:
        now = datetime.now(ZoneInfo(common.TZ))
        payload = _sanitize(event)
        record = {
            "log_id": uuid.uuid4().hex[:20],
            "scene": scene,
            "scene_label": SCENE_LABELS[scene],
            "logged_at": now.isoformat(timespec="milliseconds"),
            "log_date": now.strftime("%Y%m%d"),
            **(payload if isinstance(payload, dict) else {"payload": payload}),
        }
        path = LOG_ROOT / scene / now.strftime("%Y") / now.strftime("%m") / f"{now:%d}.jsonl"
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with _LOCK:
            _ensure_layout()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        return record
    except Exception:
        return None


def _parse_date(value: str, field: str) -> datetime:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效 YYYYMMDD") from exc


def resolve_files(scene: str, scope: str = "date", date: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None) -> list[Path]:
    """按单日、日期区间或全量返回有序日志文件。"""
    if scene not in SCENES:
        raise ValueError(f"scene 必须是 {sorted(SCENES)}")
    mode = str(scope or "date").strip().lower()
    root = LOG_ROOT / scene
    if mode == "all":
        return sorted(root.glob("*/*/*.jsonl")) if root.exists() else []
    if mode == "date":
        target = _parse_date(date or datetime.now(ZoneInfo(common.TZ)).strftime("%Y%m%d"), "date")
        path = root / target.strftime("%Y") / target.strftime("%m") / f"{target:%d}.jsonl"
        return [path] if path.exists() else []
    if mode != "range":
        raise ValueError("scope 必须是 date、range 或 all")
    start = _parse_date(date_from or "", "date_from")
    end = _parse_date(date_to or "", "date_to")
    if start > end:
        raise ValueError("date_from 不能晚于 date_to")
    if (end - start).days > 3660:
        raise ValueError("日期区间不能超过 3660 天")
    files = []
    current = start
    while current <= end:
        path = root / current.strftime("%Y") / current.strftime("%m") / f"{current:%d}.jsonl"
        if path.exists():
            files.append(path)
        current += timedelta(days=1)
    return files


def stream_jsonl(files: list[Path]) -> Iterator[bytes]:
    """逐行流式输出，避免全量日志一次载入内存。"""
    for path in files:
        with path.open("rb") as handle:
            for line in handle:
                if line.strip():
                    yield line

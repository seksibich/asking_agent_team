"""公共模块：tushare 客户端、缓存、交易日守卫、返回包装。

所有脚本与 FastAPI 服务共享本模块，避免重复初始化 tushare 与缓存逻辑。
"""
from __future__ import annotations

import json
import os
import time
import hashlib
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

try:
    import tushare as ts
except ImportError:  # 允许在无 tushare 环境导入（如仅测试缓存）
    ts = None  # type: ignore

# ---- 配置 ----
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/app/cache"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "7"))
TZ = "Asia/Shanghai"

_pro_cache: Optional[Any] = None


def get_pro() -> Any:
    """返回 tushare pro 客户端（单例）。未配置 token 时抛出明确错误。"""
    global _pro_cache
    if _pro_cache is not None:
        return _pro_cache
    if ts is None:
        raise RuntimeError("tushare not installed")
    if not TUSHARE_TOKEN:
        raise RuntimeError("TUSHARE_TOKEN not configured")
    ts.set_token(TUSHARE_TOKEN)
    _pro_cache = ts.pro_api()
    return _pro_cache


def today_str() -> str:
    """返回今日日期 YYYYMMDD（服务器需设为 CST 时区）。"""
    return datetime.now().strftime("%Y%m%d")


def now_str() -> str:
    """返回当前时间戳字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写 JSON：写临时文件再 os.replace，避免并发/崩溃写坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---- 缓存 ----
def _cache_key(name: str, params: dict[str, Any]) -> str:
    raw = name + "_" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    return f"{name}_{digest}"


PERMANENT_DIR = "permanent"  # 不可变历史数据永久缓存子目录（不参与 TTL 清理）


def _cache_path(name: str, params: dict[str, Any], day: str) -> Path:
    return CACHE_DIR / day / f"{_cache_key(name, params)}.json"


def _permanent_path(name: str, params: dict[str, Any]) -> Path:
    return CACHE_DIR / PERMANENT_DIR / f"{_cache_key(name, params)}.json"


def cached_call(
    name: str,
    params: dict[str, Any],
    fetch_fn: Callable[[], pd.DataFrame],
    use_cache: bool = True,
    historical: bool = False,
) -> dict[str, Any]:
    """带缓存的数据获取。

    - historical=True：不可变历史数据（如指定 start/end 的历史日线），
      永久缓存到 CACHE_DIR/permanent，不参与 TTL 清理，跨日复用。
    - use_cache=True 且非 historical：按当日目录缓存（日级数据当日命中）。
    - 实时数据传 use_cache=False。
    返回统一结构：{source, fetched_at, rows: [...]}。
    """
    path = _permanent_path(name, params) if historical else _cache_path(name, params, today_str())
    do_cache = use_cache or historical
    if do_cache and path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    df = fetch_fn()
    rows = df.to_dict(orient="records") if isinstance(df, pd.DataFrame) else df
    payload: dict[str, Any] = {
        "source": name,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows,
    }
    if do_cache:
        atomic_write_json(path, payload)
    return payload


def clean_expired_cache() -> None:
    """清理超过 CACHE_TTL_DAYS 天的当日缓存目录（永久缓存目录跳过）。每日首个任务调用。"""
    if not CACHE_DIR.exists():
        return
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    for sub in CACHE_DIR.iterdir():
        if sub.name == PERMANENT_DIR:
            continue
        if sub.is_dir() and sub.stat().st_mtime < cutoff:
            for p in sorted(sub.rglob("*"), reverse=True):
                p.unlink(missing_ok=True)
            sub.rmdir()


# ---- 交易日守卫 ----
def is_trade_open(day: Optional[str] = None) -> bool:
    """判断指定日期是否为 A 股交易日（默认今日）。"""
    day = day or today_str()
    pro = get_pro()
    df = pro.trade_cal(exchange="SSE", start_date=day, end_date=day)
    if df.empty:
        return False
    return int(df.iloc[0]["is_open"]) == 1


def last_trade_date(day: Optional[str] = None) -> str:
    """返回最近一个交易日（含今日）。

    注意：tushare trade_cal 返回顺序不保证升序，必须显式排序取最大交易日，
    否则会误取区间内最早的交易日。回看 30 天避免月初边界问题。
    """
    day = day or today_str()
    pro = get_pro()
    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=day)
    open_days = sorted(df[df["is_open"] == 1]["cal_date"].astype(str).tolist())
    return open_days[-1] if open_days else day


# ---- 统一错误 ----
class ServiceError(Exception):
    """携带 HTTP 状态码的服务错误。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)

"""公共模块：tushare 客户端、缓存、交易日守卫、返回包装。

所有脚本与 FastAPI 服务共享本模块，避免重复初始化 tushare 与缓存逻辑。
"""
from __future__ import annotations

import json
import os
import time
import hashlib
import threading
from datetime import datetime, date, timedelta, time as clock_time
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - urllib3 随 requests 提供，兜底不重试
    Retry = None  # type: ignore

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
CACHE_PROTOCOL = "v2"
MARKET_FINAL_READY_TIME = os.getenv("MARKET_FINAL_READY_TIME", "16:00")

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


# ---- 共享 HTTP 会话（连接复用 + keep-alive + 连接池 + 有限重试） ----
# 盯盘每 ~60s 拉全市场行情（如新浪分页约 70 次请求、8 并发），若每次新建 TCP+TLS
# 连接会显著增加单轮延迟。共享 Session 复用连接、维持 keep-alive，稳定降低延迟与握手开销。
_session_cache: Optional[requests.Session] = None
_session_lock = threading.Lock()

_HTTP_POOL_SIZE = int(os.getenv("HTTP_POOL_MAXSIZE", "16"))
_HTTP_RETRY_TOTAL = int(os.getenv("HTTP_RETRY_TOTAL", "1"))


def get_session() -> requests.Session:
    """返回全局共享的 requests.Session（单例，线程安全地按需创建）。

    - 连接池：`pool_maxsize` 覆盖盯盘分页并发（默认 16，可用 HTTP_POOL_MAXSIZE 调整）。
    - keep-alive：默认开启，跨请求/跨轮次复用到同一主机的连接。
    - 有限重试：仅对幂等 GET 的连接错误/部分 5xx 重试 1 次，不做激进重试以免拖慢盯盘。
    调用方仍需自行传 timeout；本会话不设默认超时，避免掩盖上层的严格超时约定。
    """
    global _session_cache
    if _session_cache is not None:
        return _session_cache
    with _session_lock:
        if _session_cache is not None:
            return _session_cache
        session = requests.Session()
        retry: Any = None
        if Retry is not None:
            retry = Retry(
                total=_HTTP_RETRY_TOTAL, connect=_HTTP_RETRY_TOTAL,
                read=0, status=0, backoff_factor=0.3,
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
            )
        adapter = HTTPAdapter(
            pool_connections=_HTTP_POOL_SIZE,
            pool_maxsize=_HTTP_POOL_SIZE,
            max_retries=retry,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _session_cache = session
        return _session_cache


def shanghai_now(now: Optional[datetime] = None) -> datetime:
    """返回上海时区时间；无时区入参按上海本地时间解释。"""
    current = now or datetime.now(ZoneInfo(TZ))
    if current.tzinfo is None:
        return current.replace(tzinfo=ZoneInfo(TZ))
    return current.astimezone(ZoneInfo(TZ))


def today_str() -> str:
    """返回上海时区今日日期 YYYYMMDD。"""
    return shanghai_now().strftime("%Y%m%d")


def now_str() -> str:
    """返回上海时区当前时间戳字符串。"""
    return shanghai_now().strftime("%Y-%m-%d %H:%M:%S")


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
    raw = CACHE_PROTOCOL + "_" + name + "_" + json.dumps(
        params, sort_keys=True, ensure_ascii=False
    )
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    return f"{name}_{digest}"


PERMANENT_DIR = "permanent"  # v2 协议内的不可变历史数据目录


def _cache_path(name: str, params: dict[str, Any], day: str) -> Path:
    return CACHE_DIR / CACHE_PROTOCOL / day / f"{_cache_key(name, params)}.json"


def _permanent_path(name: str, params: dict[str, Any]) -> Path:
    return CACHE_DIR / CACHE_PROTOCOL / PERMANENT_DIR / f"{_cache_key(name, params)}.json"


def _normalize_date_value(value: Any) -> Optional[str]:
    """把常见日期值规范为 YYYYMMDD，无法识别时返回空。"""
    text = str(value or "").strip().replace("-", "").replace("/", "")
    if len(text) >= 8 and text[:8].isdigit():
        candidate = text[:8]
        try:
            datetime.strptime(candidate, "%Y%m%d")
            return candidate
        except ValueError:
            return None
    return None


def _rows_max_date(rows: Any) -> Optional[str]:
    """从返回记录中提取最大业务日期，用于确认目标日数据确已覆盖。"""
    if not isinstance(rows, list):
        return None
    date_keys = ("trade_date", "TRADE_DATE", "date", "DATE", "cal_date", "ann_date")
    values = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in date_keys:
            value = _normalize_date_value(row.get(key))
            if value:
                values.append(value)
                break
    return max(values) if values else None


def cached_call(
    name: str,
    params: dict[str, Any],
    fetch_fn: Callable[[], pd.DataFrame],
    use_cache: bool = True,
    historical: bool = False,
    data_status: str = "auto",
    trade_date: Optional[str] = None,
    expected_end: Optional[str] = None,
) -> dict[str, Any]:
    """按缓存协议 v2 获取数据，兼容旧调用签名并暴露日期状态。

    日期型历史数据只有在目标日不晚于安全就绪日、且返回最大日期覆盖目标日时
    才写入永久缓存；无日期参数的静态历史数据仍可永久缓存。任何空结果均不缓存。
    """
    requested_date = _normalize_date_value(trade_date or expected_end)
    last_ready: Optional[str] = None
    if requested_date:
        try:
            last_ready = market_clock()["last_data_ready_date"]
        except Exception:
            last_ready = None

    static_history = historical and requested_date is None
    date_ready = bool(requested_date and last_ready and requested_date <= last_ready)
    permanent_candidate = bool(static_history or (historical and date_ready))
    path = (_permanent_path(name, params) if permanent_candidate
            else _cache_path(name, params, today_str()))
    do_cache = bool(use_cache or historical)
    if do_cache and path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                cached = json.load(file)
            if cached.get("rows"):
                return cached
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    frame = fetch_fn()
    rows = frame.to_dict(orient="records") if isinstance(frame, pd.DataFrame) else frame
    if rows is None:
        rows = []
    effective_date = _rows_max_date(rows)
    covers_target = bool(
        requested_date and effective_date and effective_date >= requested_date
    )
    is_final = bool(static_history or (date_ready and covers_target))
    if data_status == "auto":
        if is_final:
            resolved_status = "final"
        elif requested_date and last_ready and requested_date > last_ready:
            resolved_status = "provisional"
        elif requested_date:
            resolved_status = "incomplete"
        else:
            resolved_status = "live"
    else:
        resolved_status = str(data_status)
        if resolved_status == "final" and not is_final:
            resolved_status = "incomplete" if requested_date else "live"

    payload: dict[str, Any] = {
        "source": name,
        "fetched_at": now_str(),
        "rows": rows,
        "requested_date": requested_date,
        "effective_date": effective_date,
        "data_status": resolved_status,
        "is_final": is_final,
    }
    has_rows = isinstance(rows, (list, dict)) and bool(rows)
    should_write_permanent = bool(permanent_candidate and is_final)
    write_path = (_permanent_path(name, params) if should_write_permanent
                  else _cache_path(name, params, today_str()))
    if do_cache and has_rows:
        atomic_write_json(write_path, payload)
    return payload


def stock_names_map() -> dict:
    """返回 {ts_code: name} 映射（当日缓存，用于给选股结果补名称）。"""
    pro = get_pro()
    payload = cached_call("stock_basic_names", {"d": today_str()},
                          lambda: pro.stock_basic(list_status="L", fields="ts_code,name"))
    out = {}
    for r in payload.get("rows", []):
        out[r.get("ts_code")] = r.get("name", "")
    return out


def clean_expired_cache() -> None:
    """清理 v2 协议中过期的当日缓存；永久缓存和旧协议目录均不触碰。"""
    protocol_dir = CACHE_DIR / CACHE_PROTOCOL
    if not protocol_dir.exists():
        return
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    for sub in protocol_dir.iterdir():
        if sub.name == PERMANENT_DIR:
            continue
        if sub.is_dir() and sub.stat().st_mtime < cutoff:
            for path in sorted(sub.rglob("*"), reverse=True):
                path.unlink(missing_ok=True)
            sub.rmdir()


# ---- 交易日守卫 ----
def is_trade_open(day: Optional[str] = None) -> bool:
    """判断指定日期是否为 A 股交易日（默认上海时区今日）。"""
    day = day or today_str()
    pro = get_pro()
    frame = pro.trade_cal(exchange="SSE", start_date=day, end_date=day)
    if frame is None or frame.empty:
        return False
    return int(frame.iloc[0]["is_open"]) == 1


def last_trade_date(day: Optional[str] = None) -> str:
    """返回最近一个交易日（含指定日期），并显式按日期排序。"""
    day = day or today_str()
    pro = get_pro()
    start = (datetime.strptime(day, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
    frame = pro.trade_cal(exchange="SSE", start_date=start, end_date=day)
    open_days = sorted(
        frame[frame["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist()
    )
    return open_days[-1] if open_days else day


def _final_ready_time() -> clock_time:
    """解析日终安全就绪线；非法配置回退 16:00，且不得早于 15:00。"""
    try:
        parsed = datetime.strptime(MARKET_FINAL_READY_TIME.strip(), "%H:%M").time()
    except (TypeError, ValueError):
        parsed = clock_time(16, 0)
    return max(parsed, clock_time(15, 0))


def market_clock(now: Optional[datetime] = None) -> dict[str, Any]:
    """返回统一上海市场时钟、阶段与三种交易日期口径。

    `last_calendar_trade_date` 表示日历上最近交易日；`last_closed_trade_date`
    表示已过 15:00 收盘线的最近交易日；`last_data_ready_date` 只有越过安全
    就绪线后才包含当天，供日终数据、回测和永久缓存统一使用。
    """
    current = shanghai_now(now)
    today = current.strftime("%Y%m%d")
    start = (current - timedelta(days=60)).strftime("%Y%m%d")
    frame = get_pro().trade_cal(exchange="SSE", start_date=start, end_date=today)
    if frame is None or frame.empty or not {"cal_date", "is_open"}.issubset(frame.columns):
        raise RuntimeError("无法获取上海交易所交易日历")
    open_days = sorted(
        day for day in frame[frame["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist()
        if day <= today
    )
    if not open_days:
        raise RuntimeError("交易日历中没有可用交易日")
    is_trading_day = today in open_days
    last_calendar = open_days[-1]
    previous_days = [day for day in open_days if day < today]
    previous_trade = previous_days[-1] if previous_days else last_calendar
    local_time = current.time().replace(tzinfo=None)

    if not is_trading_day:
        phase = "non_trading_day"
    elif local_time < clock_time(9, 15):
        phase = "preopen"
    elif local_time < clock_time(9, 30):
        phase = "call_auction"
    elif local_time < clock_time(11, 30):
        phase = "morning"
    elif local_time < clock_time(13, 0):
        phase = "lunch"
    elif local_time < clock_time(15, 0):
        phase = "afternoon"
    elif local_time < _final_ready_time():
        phase = "closed_pending"
    else:
        phase = "final"

    is_continuous = phase in {"morning", "afternoon"}
    last_closed = (today if is_trading_day and local_time >= clock_time(15, 0)
                   else previous_trade if is_trading_day else last_calendar)
    last_ready = (today if is_trading_day and phase == "final"
                  else previous_trade if is_trading_day else last_calendar)
    return {
        "market_time": current.strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "is_trading_day": is_trading_day,
        "is_continuous_trading": is_continuous,
        "last_calendar_trade_date": last_calendar,
        "last_closed_trade_date": last_closed,
        "last_data_ready_date": last_ready,
        "final_ready_time": _final_ready_time().strftime("%H:%M"),
    }


def last_calendar_trade_date(now: Optional[datetime] = None) -> str:
    """返回日历口径最近交易日，交易时段内可包含今天。"""
    return str(market_clock(now)["last_calendar_trade_date"])


def last_closed_trade_date(now: Optional[datetime] = None) -> str:
    """返回已越过 15:00 收盘线的最近交易日，不代表日终数据已发布。"""
    return str(market_clock(now)["last_closed_trade_date"])


def last_data_ready_date(now: Optional[datetime] = None) -> str:
    """返回越过安全就绪线的最近交易日，供最终数据与永久缓存使用。"""
    return str(market_clock(now)["last_data_ready_date"])


def last_completed_trade_date(now: Optional[datetime] = None) -> str:
    """兼容旧函数名，返回安全的最近数据就绪交易日。"""
    return last_data_ready_date(now)


# ---- 统一错误 ----
class ServiceError(Exception):
    """携带 HTTP 状态码的服务错误。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)

"""市场情绪温度（0-100）。

综合多项指标：大盘指数动量、板块涨跌比、大盘涨跌家数比、平均股价指数动量、
大盘成交额、涨跌停家数；以及大盘指数与平均股价指数的**振幅方向信号 + 实体长度**
（按百分点位口径，低权重，已涵盖原"大盘K线形态"的阳阴实体/收盘强弱语义）。
各指标"越高越热/越偏多"，按**当天之前 7 个交易日**窗口做 min-max 归一为 0-100 子分，
再按情绪指标权重（factor_config 的 sentiment 模型）加权合成。

其中振幅/实体因子的语义（越高越偏多）：
- 振幅越大=分歧越大；长下影线→适当高分（抄底/支撑），长上影线→适当低分（抛压）。
- 长实体依阳/阴定方向：长阳高分、长阴低分。
- 短实体 + 振幅偏中性→贴近中性（分歧小、抄底力度小）。

完整收盘原始指标按交易日持久化到数据库 daily_sentiment；盘中快照使用 30 秒内存刷新间隔，
并写入独立的当日运行时缓存文件以支持午休/服务重启恢复，但绝不写入 daily_sentiment。
盘中缺失指标按剩余可用权重重新归一。

数据来源：
- 收盘：Tushare daily / dc_index / limit_list_d / index_daily。
- 盘中：东方财富全市场与板块实时列表 + Tushare realtime_quote 沪深300；
  涨跌停、成交额、振幅及 K 线实体等收盘指标暂不参与，收盘后自动补齐。
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
import pandas as pd

import common
import db
import factor_contract
import factor_config
from registry import register

WINDOW_DEFAULT = 7        # 默认：当天之前 7 个交易日
WINDOW_MIN, WINDOW_MAX = 3, 30
WINDOW_CONFIG_KEY = "sentiment_window"
BENCH_INDEX = "000300.SH"
EXTREME_WINDOW = 20      # 极端指数固定使用此前最多 20 个交易日的 robust 窗口
EXTREME_MIN_HISTORY = 8
EXTREME_WEIGHTS = {
    "volatility": 0.30,
    "volume_shock": 0.15,
    "kline_shock": 0.20,
    "breadth_extreme": 0.20,
    "limit_shock": 0.15,
}
VOLATILITY_FIELDS = (
    "market_amp", "return_dispersion", "mean_abs_return", "index_range_abs",
)
KLINE_SHOCK_FIELDS = (
    "market_body_abs", "market_shadow_abs", "index_body_abs", "index_shadow_abs",
)
EXTREME_RAW_FIELDS = set(VOLATILITY_FIELDS + KLINE_SHOCK_FIELDS) | {
    "turnover", "breadth_extreme", "limit_shock",
}

LEVELS = [(80, "高潮"), (60, "回暖"), (40, "分歧"), (20, "退潮"), (0, "冰点")]
EXTREME_LEVELS = [(80, "高强度"), (60, "偏高强度"), (40, "中等强度"), (0, "相对平稳")]

# 反向指标：原始值越大越"冷"，子分取反（100 - minmax），从而拉低温度
INVERSE_INDICATORS = {"limit_down"}

# 盘中温度继续仅使用其既有可核验字段；极端指数会额外消费实时宽度与 OHLC 强度字段。
INTRADAY_FIELDS = {"adv_dec_ratio", "sector_ratio", "index_mom", "avg_price_mom"}
FINAL_ONLY_FIELDS = {
    "limit_up", "limit_down", "turnover",
}
EASTMONEY_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_UT = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_STOCK_SCOPE = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
EASTMONEY_SECTOR_SCOPE = "m:90+t:2"
_INTRADAY_TTL_SECONDS = 30
_INTRADAY_CACHE: dict[str, Any] = {}
_INTRADAY_LOCK = threading.Lock()


def _intraday_disk_path(target: str):
    """返回盘中临时快照文件；文件按交易日隔离，且不属于完整收盘数据库。"""
    return common.CACHE_DIR / "intraday_sentiment" / f"{target}.json"


def _window() -> int:
    """情绪归一窗口（当天之前 N 个交易日），可配置，范围 3-30，默认 7。"""
    try:
        v = db.get_config(WINDOW_CONFIG_KEY)
        n = int(v.get("window")) if isinstance(v, dict) else int(v)
    except Exception:
        return WINDOW_DEFAULT
    return max(WINDOW_MIN, min(WINDOW_MAX, n))


def _trade_dates(pro, end: str, n: int) -> list[str]:
    """返回截至 end（含）的最近 n 个交易日（升序）。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=n * 2 + 20)).strftime("%Y%m%d")
    df = pro.index_daily(ts_code=BENCH_INDEX, start_date=start, end_date=end)
    if df is None or df.empty:
        return []
    dates = sorted(df["trade_date"].astype(str).tolist())
    return dates[-n:]


def _collect(pro, date: str) -> Optional[dict[str, Any]]:
    """采集单个完整交易日指标；关键组件缺失时拒绝发布 final。"""
    try:
        import eod_fallback
        daily = eod_fallback.fetch_daily_slice(pro, date)
    except Exception:
        return None
    providers = sorted(set(daily.get("_provider", pd.Series(dtype=str)).dropna().astype(str)))
    daily_provider = "+".join(providers) or "unknown"
    pct = daily["pct_chg"].astype(float)
    adv = int((pct > 0).sum())
    dec = int((pct < 0).sum())
    adv_dec_ratio = adv / (adv + dec) if (adv + dec) else 0.5
    avg_chg = float(pct.mean())  # 全市场平均涨跌幅（以涨幅锚定，越高越热）
    return_dispersion = float(pct.std(ddof=0))
    mean_abs_return = float(pct.abs().mean())
    breadth_extreme = abs(adv_dec_ratio - 0.5) * 2.0
    turnover = float(daily["amount"].astype(float).sum())  # 千元

    # 全市场个股等权日内强度，均不包含多空方向。
    market_amp = 0.0
    market_body_abs = 0.0
    market_shadow_abs = 0.0
    try:
        ranges = daily[["open", "high", "low", "close", "pre_close"]].astype(float)
        ranges = ranges[
            (ranges["pre_close"] > 0)
            & (ranges[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (ranges["high"] >= ranges[["open", "close"]].max(axis=1))
            & (ranges["low"] <= ranges[["open", "close"]].min(axis=1))
        ]
        if not ranges.empty:
            scale = 100.0 / ranges["pre_close"]
            upper = ranges["high"] - ranges[["open", "close"]].max(axis=1)
            lower = ranges[["open", "close"]].min(axis=1) - ranges["low"]
            market_amp = float(((ranges["high"] - ranges["low"]) * scale).mean())
            market_body_abs = float(((ranges["close"] - ranges["open"]).abs() * scale).mean())
            market_shadow_abs = float(((upper + lower) * scale).mean())
    except Exception:
        pass

    # 平均股价指数：全市场个股 OHLC 等权平均，保留旧温度字段兼容。
    avg_price_body = 0.0
    avg_price_amp = 0.0
    avg_price_range_abs = 0.0
    avg_price_body_abs = 0.0
    avg_price_upper_shadow = 0.0
    avg_price_lower_shadow = 0.0
    try:
        a_open = float(daily["open"].astype(float).mean())
        a_high = float(daily["high"].astype(float).mean())
        a_low = float(daily["low"].astype(float).mean())
        a_close = float(daily["close"].astype(float).mean())
        a_pre = float(daily["pre_close"].astype(float).mean())
        if a_pre > 0:
            avg_price_body, avg_price_amp = _kline_signals(
                a_open, a_high, a_low, a_close, a_pre)
            avg_price_range_abs, avg_price_body_abs, avg_price_upper_shadow, \
                avg_price_lower_shadow = _kline_strengths(
                    a_open, a_high, a_low, a_close, a_pre)
    except Exception:
        pass

    # 板块涨跌比；该组件缺失时拒绝发布完整 final，交给服务端稍后重试。
    try:
        sec = pro.dc_index(trade_date=date)
        if sec is None or sec.empty:
            return None
        col = "pct_change" if "pct_change" in sec.columns else ("pct_chg" if "pct_chg" in sec.columns else None)
        if not col:
            return None
        sp = pd.to_numeric(sec[col], errors="coerce").dropna()
        if sp.empty:
            return None
        su, sd = int((sp > 0).sum()), int((sp < 0).sum())
        sector_ratio = su / (su + sd) if (su + sd) else 0.5
    except Exception:
        return None

    # 涨停 / 跌停家数；接口未就绪时拒绝把 0 当作真实家数。
    try:
        lim = pro.limit_list_d(trade_date=date)
        if lim is None or lim.empty or "limit" not in lim.columns:
            return None
        limit_up = int((lim["limit"] == "U").sum())
        limit_down = int((lim["limit"] == "D").sum())
    except Exception:
        return None
    limit_shock = limit_up + limit_down

    # 大盘指数动量（当日涨跌幅）+ 按【百分点位】口径的大盘振幅方向信号与实体长度
    # （实体+影线已覆盖原 index_kline 的收盘强弱/阳阴实体语义，故不再单列 K 线形态因子）
    index_mom = 0.0
    index_body = 0.0
    index_amp = 0.0
    index_range_abs = 0.0
    index_body_abs = 0.0
    index_shadow_abs = 0.0
    index_upper_shadow = 0.0
    index_lower_shadow = 0.0
    try:
        idx = pro.index_daily(ts_code=BENCH_INDEX, trade_date=date)
        if idx is None or idx.empty:
            return None
        row = idx.iloc[0]
        index_mom = float(row["pct_chg"])
        o, h, l, cl = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        pre = float(row["pre_close"]) if "pre_close" in idx.columns else 0.0
        if pre <= 0:
            return None
        index_body, index_amp = _kline_signals(o, h, l, cl, pre)
        index_range_abs, index_body_abs, index_upper_shadow, index_lower_shadow = \
            _kline_strengths(o, h, l, cl, pre)
        index_shadow_abs = round(index_upper_shadow + index_lower_shadow, 4)
    except Exception:
        return None

    return {
        "adv_dec_ratio": round(adv_dec_ratio, 4),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "sector_ratio": round(sector_ratio, 4),
        "turnover": round(turnover, 2),
        "market_amp": round(market_amp, 4),
        "market_body_abs": round(market_body_abs, 4),
        "market_shadow_abs": round(market_shadow_abs, 4),
        "return_dispersion": round(return_dispersion, 4),
        "mean_abs_return": round(mean_abs_return, 4),
        "breadth_extreme": round(breadth_extreme, 4),
        "limit_shock": limit_shock,
        "index_mom": round(index_mom, 4),
        "avg_price_mom": round(avg_chg, 4),
        "index_body": index_body,
        "index_amp": index_amp,
        "avg_price_body": avg_price_body,
        "avg_price_amp": avg_price_amp,
        "index_range_abs": index_range_abs,
        "index_body_abs": index_body_abs,
        "index_shadow_abs": index_shadow_abs,
        "avg_price_range_abs": avg_price_range_abs,
        "avg_price_body_abs": avg_price_body_abs,
        "index_upper_shadow": index_upper_shadow,
        "index_lower_shadow": index_lower_shadow,
        "avg_price_upper_shadow": avg_price_upper_shadow,
        "avg_price_lower_shadow": avg_price_lower_shadow,
        "adv": adv, "dec": dec,
        "_meta": {
            "is_final": True,
            "data_status": "final",
            "mode": "close",
            "source": f"{daily_provider} daily + tushare dc_index/limit_list_d/index_daily",
            "provider_chain": [daily_provider, "tushare"],
            "target_date": date,
            "collected_at": common.now_str(),
            "finalized_at": common.now_str(),
        },
    }


def _kline_signals(o: float, h: float, l: float, cl: float, pre: float) -> tuple[float, float]:
    """由单根K线 OHLC + 前收，计算【百分点位】口径的两项情绪信号（越高越偏多/越热）。

    - body 实体长度：(close-open)/pre*100，阳线为正、阴线为负，绝对值=实体长度。
      长阳→高分，长阴→低分，短实体→贴近 0（中性，分歧小）。
    - amp  振幅方向信号：(下影线-上影线)/pre*100。
      振幅大且长下影线→大正值（抄底/支撑，偏多，适当高分）；
      振幅大且长上影线→大负值（抛压，偏空，适当低分）；
      振幅小/影线短→贴近 0（分歧小、抄底力度小，中性）。
    """
    body = (cl - o) / pre * 100.0
    upper = (h - max(o, cl)) / pre * 100.0   # 上影线（百分点）
    lower = (min(o, cl) - l) / pre * 100.0   # 下影线（百分点）
    amp = lower - upper
    return round(body, 4), round(amp, 4)


def _kline_strengths(o: float, h: float, l: float, cl: float,
                     pre: float) -> tuple[float, float, float, float]:
    """计算不含多空方向的 K 线强度：总振幅、实体绝对值、上影线和下影线。"""
    if pre <= 0 or min(o, h, l, cl) <= 0 or h < max(o, cl) or l > min(o, cl):
        return 0.0, 0.0, 0.0, 0.0
    scale = 100.0 / pre
    return (
        round((h - l) * scale, 4),
        round(abs(cl - o) * scale, 4),
        round(max(0.0, h - max(o, cl)) * scale, 4),
        round(max(0.0, min(o, cl) - l) * scale, 4),
    )


def _minmax_sub(values: list[float], today: float) -> float:
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 50.0
    return round(100 * (today - lo) / (hi - lo), 1)


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _eastmoney_page(scope: str, page: int) -> tuple[int, list[dict[str, Any]]]:
    """读取东方财富实时列表单页；按静态证券代码排序，避免行情变化造成跨页漂移。"""
    response = requests.get(
        EASTMONEY_LIST_URL,
        params={
            "pn": page, "pz": 100, "po": 0, "np": 1,
            "ut": EASTMONEY_UT, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": scope,
            "fields": "f2,f3,f6,f12,f14,f15,f16,f17,f18",
        },
        timeout=8,
    )
    response.raise_for_status()
    data = response.json().get("data") or {}
    return int(data.get("total") or 0), list(data.get("diff") or [])


def _eastmoney_rows(scope: str, label: str) -> tuple[list[dict[str, Any]], int, list[str]]:
    """并发读取指定实时列表并按代码去重；诊断始终标明全市场或板块来源。"""
    errors: list[str] = []
    try:
        total, first = _eastmoney_page(scope, 1)
    except Exception as exc:
        return [], 0, [f"{label}实时列表首页失败：{type(exc).__name__}: {exc}"[:240]]
    pages = max(1, math.ceil(total / 100))
    page_rows: list[dict[str, Any]] = list(first)

    def fetch(page: int) -> tuple[int, list[dict[str, Any]], Optional[str]]:
        try:
            page_total, items = _eastmoney_page(scope, page)
            return page_total, items, None
        except Exception as exc:
            return 0, [], f"{label}实时列表第{page}页失败：{type(exc).__name__}: {exc}"[:240]

    if pages > 1:
        with ThreadPoolExecutor(max_workers=min(12, pages - 1)) as executor:
            for page_total, items, error in executor.map(fetch, range(2, pages + 1)):
                if page_total and page_total != total:
                    errors.append(f"{label}实时列表分页总数变化：{total}->{page_total}")
                page_rows.extend(items)
                if error:
                    errors.append(error)

    deduped: dict[str, dict[str, Any]] = {}
    for row in page_rows:
        code = str(row.get("f12") or "").strip()
        if code:
            deduped[code] = row
    return list(deduped.values()), total, errors


def _unique_messages(values: list[Any], limit: int = 20) -> list[str]:
    """按原顺序去重诊断文本，避免刷新和双数据源重复堆积同一错误。"""
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        message = str(value or "").strip()
        if message and message not in seen:
            seen.add(message)
            unique.append(message[:240])
    return unique[-limit:]


def _clock_value(clock: Any, *names: str) -> Any:
    """兼容公共市场时钟返回字典或只读对象，避免在本模块重复定义时段。"""
    for name in names:
        if isinstance(clock, dict) and name in clock:
            return clock[name]
        if hasattr(clock, name):
            return getattr(clock, name)
    return None


def _last_data_ready_date(clock: Any = None) -> str:
    """优先读取 common.last_data_ready_date，并兼容旧版公共时钟。"""
    candidates: list[Any] = []
    provider = getattr(common, "last_data_ready_date", None)
    if provider is not None:
        try:
            candidates.append(provider() if callable(provider) else provider)
        except Exception:
            pass
    candidates.append(_clock_value(clock, "last_data_ready_date"))
    legacy = getattr(common, "last_completed_trade_date", None)
    if legacy is not None:
        try:
            candidates.append(legacy() if callable(legacy) else legacy)
        except Exception:
            pass
    for value in candidates:
        ready = str(value or "").strip().replace("-", "")
        if len(ready) == 8 and ready.isdigit():
            try:
                datetime.strptime(ready, "%Y%m%d")
                return ready
            except ValueError:
                continue
    raise RuntimeError("common.last_data_ready_date 与兼容回退均不可用")


def _market_session() -> tuple[Any, str, str]:
    """优先消费 common.market_clock，统一适配竞价、待收盘数据与 final 阶段。"""
    clock: Any = None
    state = ""
    provider = getattr(common, "market_clock", None)
    if provider is not None:
        try:
            clock = provider() if callable(provider) else provider
            state = str(_clock_value(
                clock, "phase", "state", "market_state", "session", "status") or "").strip()
        except Exception:
            clock = None
            state = ""

    aliases = {
        "auction": "call_auction", "callauction": "call_auction",
        "call-auction": "call_auction", "opening_auction": "call_auction",
        "midday": "lunch", "midday_break": "lunch", "break": "lunch",
        "pending": "closed_pending", "postclose": "closed_pending",
        "closed-pending": "closed_pending", "closed": "closed_pending",
        "ready": "final", "completed": "final", "eod": "final",
    }
    normalized = state.lower().replace(" ", "_")
    state = aliases.get(normalized, normalized)

    # 仅在旧版 common 不提供可用时钟状态时使用本地时段兼容，不覆盖公共时钟判断。
    if not state:
        current = datetime.now(ZoneInfo(common.TZ))
        minute = current.hour * 60 + current.minute
        if minute < 9 * 60 + 15:
            state = "preopen"
        elif minute < 9 * 60 + 30:
            state = "call_auction"
        elif minute < 11 * 60 + 30:
            state = "morning"
        elif minute < 13 * 60:
            state = "lunch"
        elif minute < 15 * 60:
            state = "afternoon"
        else:
            state = "closed_pending"

    ready = _last_data_ready_date(clock)
    if ready >= common.today_str() and state == "closed_pending":
        state = "final"
    elif ready < common.today_str() and state == "final":
        state = "closed_pending"
    return clock, state, ready


def _pick(row: dict[str, Any], *names: str) -> Any:
    """从多供应商字段别名中选取首个非空值。"""
    for name in names:
        value = row.get(name)
        if value not in (None, "", "-"):
            return value
    return None


def _row_return(row: dict[str, Any]) -> Optional[float]:
    """读取供应商涨跌幅；缺少显式字段时用现价与昨收可核验计算。"""
    value = _finite(_pick(
        row, "pct_chg", "pct_change", "PCT_CHANGE", "change_pct", "change_percent",
        "change_rate", "涨跌幅", "f3"))
    if value is not None:
        return value
    price = _finite(_pick(row, "price", "close", "PRICE", "现价", "f2"))
    pre_close = _finite(_pick(
        row, "pre_close", "PRE_CLOSE", "CLOSE", "yesterday_close", "昨收", "f18"))
    if price is None or pre_close is None or pre_close <= 0:
        return None
    return (price / pre_close - 1.0) * 100.0


def _normalized_quote_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "").replace("/", "")
    return text[:8] if len(text) >= 8 and text[:8].isdigit() else ""


def _backup_market_rows(target: str) -> tuple[list[dict[str, Any]], int, dict[str, Any], list[str]]:
    """懒加载统一新浪快照，并严格服从统一接口的日期与覆盖质量判定。"""
    try:
        # loader 会把各 skills/scripts 加入 sys.path；函数内导入避免与 market_data 循环导入。
        import market_data
        fetcher = getattr(market_data, "fetch_realtime_market_snapshot", None)
        if not callable(fetcher):
            return [], 0, {}, ["统一新浪备用接口不存在，继续沿用东方财富结果"]
        payload = fetcher(prefer="sina")
    except Exception as exc:
        return [], 0, {}, [f"统一新浪全市场备用失败：{type(exc).__name__}: {exc}"[:240]]
    if not isinstance(payload, dict):
        return [], 0, {}, ["统一新浪备用快照返回格式无效"]

    errors = _unique_messages(list(payload.get("errors") or []))
    provider_name = str(payload.get("provider") or payload.get("source") or "").lower()
    if "sina" not in provider_name:
        return [], 0, payload, _unique_messages([
            *errors, f"统一备用快照来源不是新浪：{provider_name or '未标注'}"])

    rows_value = payload.get("rows") or []
    rows = [row for row in rows_value if isinstance(row, dict)] if isinstance(rows_value, list) else []
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    quote_date = _normalized_quote_date(payload.get("quote_date"))
    row_anchors = {date for date in (
        _normalized_quote_date(_pick(row, "date", "trade_date", "quote_date", "DATE", "TIME"))
        for row in rows) if date}
    observed = ({quote_date} if quote_date else set()) | row_anchors
    if not observed or observed != {target}:
        return [], 0, payload, _unique_messages([
            *errors, f"统一新浪备用快照日期不严格属于今天：{sorted(observed) or '无日期'}"])

    try:
        total = int(coverage.get("stock_count") or len(rows))
    except (TypeError, ValueError):
        total = len(rows)
    validation_passed = coverage.get("validation_passed") is True
    if payload.get("degraded") is True or not validation_passed:
        return [], total, payload, _unique_messages([
            *errors,
            "统一新浪备用快照未通过全市场覆盖校验："
            f"有效股票 {coverage.get('stock_count', len(rows))}，"
            f"综合覆盖 {coverage.get('coverage_ratio', 0)}",
        ])

    valid = [row for row in rows if _row_return(row) is not None]
    valid_ratio = len(valid) / total if total > 0 else 0.0
    minimum_count = max(1000, int(coverage.get("minimum_stock_count") or 0))
    minimum_ratio = max(0.8, float(coverage.get("coverage_threshold") or 0))
    if len(valid) < minimum_count or valid_ratio < minimum_ratio:
        return [], total, payload, _unique_messages([
            *errors, f"统一新浪备用快照有效覆盖不足：{len(valid)}/{total}"])
    return valid, total, payload, errors


def _intraday_snapshot(target: str, force_refresh: bool = False) -> tuple[Optional[dict[str, Any]], str]:
    """生成不落库的盘中情绪快照；强刷绕过 TTL，失败时仍沿用最后成功快照。"""
    if target != common.today_str():
        return None, "仅支持读取今天的盘中快照"
    if not common.is_trade_open(target):
        return None, "今天不是交易日"
    _, session, ready_date = _market_session()
    if session == "preopen":
        return None, "盘前不允许生成盘中快照"
    if ready_date >= target:
        return None, "完整日数据已就绪，应切换 final 模式"

    now_mono = time.monotonic()
    stale_payload: Optional[dict[str, Any]] = None
    stale_age = 0.0

    def mark_provisional(payload: dict[str, Any]) -> dict[str, Any]:
        marked = dict(payload)
        metadata = dict(marked.get("_meta") or {})
        metadata.update({
            "is_final": False,
            "data_status": "provisional",
            "market_session": session,
        })
        marked["_meta"] = metadata
        return marked

    with _INTRADAY_LOCK:
        cached = _INTRADAY_CACHE.get(target)
        if cached:
            stale_payload = dict(cached["payload"])
            stale_age = max(0.0, now_mono - float(cached.get("cached_at", 0)))
            if (not force_refresh and stale_age < _INTRADAY_TTL_SECONDS
                    and session not in {"lunch", "closed_pending"}):
                return mark_provisional(stale_payload), ""

    # 独立运行时文件只恢复当天、明确标记为非 final 的快照，绝不混入历史数据库。
    if not stale_payload:
        disk_path = _intraday_disk_path(target)
        try:
            with disk_path.open("r", encoding="utf-8") as file:
                disk_payload = json.load(file)
            disk_meta = disk_payload.get("_meta") if isinstance(disk_payload, dict) else None
            if (isinstance(disk_meta, dict)
                    and disk_meta.get("is_final") is False
                    and disk_meta.get("target_date") == target):
                stale_payload = disk_payload
                stale_age = max(
                    0.0, datetime.now(ZoneInfo(common.TZ)).timestamp() - disk_path.stat().st_mtime)
                with _INTRADAY_LOCK:
                    _INTRADAY_CACHE[target] = {
                        "cached_at": now_mono - stale_age, "payload": dict(disk_payload),
                    }
                if (not force_refresh and stale_age < _INTRADAY_TTL_SECONDS
                        and session not in {"lunch", "closed_pending"}):
                    return mark_provisional(dict(disk_payload)), ""
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def use_stale(reason: str, refresh_errors: Optional[list[str]] = None
                  ) -> tuple[Optional[dict[str, Any]], str]:
        if not stale_payload:
            return None, reason
        payload = dict(stale_payload)
        metadata = dict(payload.get("_meta") or {})
        metadata.update({
            "is_final": False,
            "data_status": "provisional",
            "market_session": session,
            "stale": True,
            "stale_reason": reason,
            "cache_age_seconds": round(stale_age, 1),
            "waiting_final": session == "closed_pending",
            "errors": _unique_messages([
                *(metadata.get("errors") or []), *(refresh_errors or [])]),
        })
        payload["_meta"] = metadata
        return payload, ""

    # 午休不刷新；closed_pending 优先保留今日盘中快照，明确等待完整日数据发布。
    if session == "lunch":
        if stale_payload:
            return use_stale("午间休市，沿用上午最后盘中快照")
        return None, "午间休市且没有可沿用的今日盘中快照"
    if session == "closed_pending":
        if stale_payload:
            return use_stale("已收盘，沿用今日最后盘中快照并等待 final 数据")
        return None, "已收盘但 final 数据尚未就绪，且没有今日盘中快照可沿用"

    errors: list[str] = []
    source_events: list[str] = []
    component_warnings: list[str] = []
    try:
        import tushare as ts
        index_frame = ts.realtime_quote(ts_code=BENCH_INDEX)
        index_row = (index_frame.iloc[0].to_dict()
                     if index_frame is not None and not index_frame.empty else {})
    except Exception as exc:
        errors.append(f"沪深300实时指数失败：{type(exc).__name__}: {exc}"[:240])
        index_row = {}
    quote_date = _normalized_quote_date(index_row.get("DATE"))

    dc_rows, dc_total, stock_errors = _eastmoney_rows(
        EASTMONEY_STOCK_SCOPE, "东方财富全市场")
    sector_rows, sector_total, sector_errors = _eastmoney_rows(
        EASTMONEY_SECTOR_SCOPE, "东方财富板块")
    errors.extend(stock_errors)
    errors.extend(sector_errors)
    dc_changes = [value for row in dc_rows if (value := _row_return(row)) is not None]
    dc_coverage = len(dc_changes) / dc_total if dc_total else 0.0
    dc_valid = quote_date == target and len(dc_changes) >= 1000 and dc_coverage >= 0.8

    stock_rows = dc_rows
    stock_total = dc_total
    stock_changes = dc_changes
    provider_chain = ["eastmoney"]
    market_source = "东方财富全市场实时列表"
    backup_payload: dict[str, Any] = {}
    if not dc_valid:
        if quote_date != target:
            errors.append(f"东方财富快照缺少今天锚：实时指数日期={quote_date or '无'}")
        if len(dc_changes) < 1000 or dc_coverage < 0.8:
            errors.append(f"东方财富全市场有效覆盖不足：{len(dc_changes)}/{dc_total}")
        backup_rows, backup_total, backup_payload, backup_errors = _backup_market_rows(target)
        errors.extend(backup_errors)
        if backup_rows:
            stock_rows = backup_rows
            stock_total = backup_total
            stock_changes = [value for row in stock_rows
                             if (value := _row_return(row)) is not None]
            reported_source = str(
                backup_payload.get("source") or backup_payload.get("provider") or "sina")
            chain_value = backup_payload.get("provider_chain")
            if isinstance(chain_value, list) and chain_value:
                provider_chain = ["eastmoney_direct", *[str(item) for item in chain_value]]
            else:
                provider_chain = ["eastmoney_direct", "market_data:sina", reported_source]
            market_source = reported_source
            source_events.append("东方财富全市场实时源不可用，已切换新浪备用源")
        else:
            return use_stale("东方财富失败或覆盖不足，且全市场备用源不可用", errors)

    stock_coverage = len(stock_changes) / stock_total if stock_total else 0.0
    if len(stock_changes) < 1000 or stock_coverage < 0.8:
        return use_stale("所有实时源的全市场有效覆盖均不足", errors)

    raw: dict[str, Any] = {}
    adv = sum(value > 0 for value in stock_changes)
    dec = sum(value < 0 for value in stock_changes)
    adv_dec_ratio = adv / (adv + dec) if adv + dec else 0.5
    mean_return = sum(stock_changes) / len(stock_changes)
    return_dispersion = math.sqrt(sum(
        (value - mean_return) ** 2 for value in stock_changes) / len(stock_changes))
    raw.update({
        "adv_dec_ratio": round(adv_dec_ratio, 4),
        "avg_price_mom": round(mean_return, 4),
        "return_dispersion": round(return_dispersion, 4),
        "mean_abs_return": round(
            sum(abs(value) for value in stock_changes) / len(stock_changes), 4),
        "breadth_extreme": round(abs(adv_dec_ratio - 0.5) * 2.0, 4),
        "adv": adv, "dec": dec,
    })

    # OHLC 覆盖也必须达到全市场阈值；不足时宁可缺失组件，不用局部样本外推。
    verified_ohlc: list[tuple[float, float, float, float, float]] = []
    for row in stock_rows:
        o = _finite(_pick(row, "open", "OPEN", "f17"))
        h = _finite(_pick(row, "high", "HIGH", "f15"))
        l = _finite(_pick(row, "low", "LOW", "f16"))
        cl = _finite(_pick(row, "price", "close", "PRICE", "f2"))
        pre = _finite(_pick(
            row, "pre_close", "PRE_CLOSE", "CLOSE", "yesterday_close", "f18"))
        if (None not in (o, h, l, cl, pre) and pre and pre > 0
                and min(o, h, l, cl) > 0 and h >= max(o, cl) and l <= min(o, cl)):
            verified_ohlc.append((o, h, l, cl, pre))
    ohlc_coverage = len(verified_ohlc) / stock_total if stock_total else 0.0
    if len(verified_ohlc) >= 1000 and ohlc_coverage >= 0.8:
        strengths = [_kline_strengths(*values) for values in verified_ohlc]
        raw.update({
            "market_amp": round(sum(value[0] for value in strengths) / len(strengths), 4),
            "market_body_abs": round(sum(value[1] for value in strengths) / len(strengths), 4),
            "market_shadow_abs": round(
                sum(value[2] + value[3] for value in strengths) / len(strengths), 4),
        })
        averages = tuple(sum(values[i] for values in verified_ohlc) / len(verified_ohlc)
                         for i in range(5))
        avg_body, avg_amp = _kline_signals(*averages)
        avg_range, avg_body_abs, avg_upper, avg_lower = _kline_strengths(*averages)
        raw.update({
            "avg_price_body": avg_body,
            "avg_price_amp": avg_amp,
            "avg_price_range_abs": avg_range,
            "avg_price_body_abs": avg_body_abs,
            "avg_price_upper_shadow": avg_upper,
            "avg_price_lower_shadow": avg_lower,
        })
    else:
        errors.append(f"全市场实时 OHLC 有效覆盖不足：{len(verified_ohlc)}/{stock_total}")
        component_warnings.append("全市场实时 OHLC 覆盖不足，K 线强度指标未参与")

    sector_changes = [value for row in sector_rows
                      if (value := _finite(row.get("f3"))) is not None]
    sector_coverage = len(sector_changes) / sector_total if sector_total else 0.0
    if sector_coverage >= 0.8 and sector_changes:
        sector_up = sum(value > 0 for value in sector_changes)
        sector_down = sum(value < 0 for value in sector_changes)
        raw["sector_ratio"] = (round(sector_up / (sector_up + sector_down), 4)
                               if sector_up + sector_down else 0.5)
    else:
        errors.append(f"板块有效实时覆盖不足：{len(sector_changes)}/{sector_total}")
        component_warnings.append("板块实时数据不可用，板块涨跌指标未参与")

    price = _finite(index_row.get("PRICE"))
    pre_close = _finite(index_row.get("PRE_CLOSE"))
    if quote_date == target and price is not None and pre_close not in (None, 0):
        raw["index_mom"] = round((price / pre_close - 1) * 100, 4)
        index_ohlc = tuple(_finite(index_row.get(field)) for field in (
            "OPEN", "HIGH", "LOW", "PRICE", "PRE_CLOSE"))
        if None not in index_ohlc:
            o, h, l, cl, pre = index_ohlc
            if pre and pre > 0 and min(o, h, l, cl) > 0 and h >= max(o, cl) and l <= min(o, cl):
                body, amp = _kline_signals(o, h, l, cl, pre)
                range_abs, body_abs, upper, lower = _kline_strengths(o, h, l, cl, pre)
                raw.update({
                    "index_body": body, "index_amp": amp,
                    "index_range_abs": range_abs, "index_body_abs": body_abs,
                    "index_shadow_abs": round(upper + lower, 4),
                    "index_upper_shadow": upper, "index_lower_shadow": lower,
                })
    else:
        component_warnings.append("沪深300实时行情不可用，指数动量与指数 K 线强度未参与")

    turnover_so_far = sum(
        value for row in stock_rows if (value := _finite(_pick(
            row, "amount", "AMOUNT", "turnover", "成交额", "f6"))) is not None)
    backup_meta = backup_payload.get("meta") if isinstance(backup_payload.get("meta"), dict) else {}
    backup_as_of = (backup_payload.get("as_of") or backup_payload.get("quote_time")
                    or " ".join(part for part in (
                        str(backup_payload.get("anchor_date") or "").strip(),
                        str(backup_payload.get("anchor_time") or "").strip()) if part)
                    or backup_payload.get("fetched_at")
                    or backup_meta.get("as_of") or backup_meta.get("quote_time"))
    raw["_meta"] = {
        "is_final": False,
        "data_status": "provisional",
        "target_date": target,
        "market_session": session,
        "mode": "intraday",
        "source": f"{market_source} + tushare realtime_quote(沪深300)",
        "provider_chain": provider_chain,
        "as_of": (" ".join(part for part in (
            str(index_row.get("DATE") or "").strip(),
            str(index_row.get("TIME") or "").strip()) if part) or str(backup_as_of or "")),
        "stock_coverage": {
            "unique_rows": len(stock_rows), "valid_rows": len(stock_changes),
            "total": stock_total, "ratio": round(stock_coverage, 4),
        },
        "ohlc_coverage": {
            "valid_rows": len(verified_ohlc), "total": stock_total,
            "ratio": round(ohlc_coverage, 4),
        },
        "sector_coverage": {
            "unique_rows": len(sector_rows), "valid_rows": len(sector_changes),
            "total": sector_total, "ratio": round(sector_coverage, 4),
        },
        "turnover_so_far_yuan": round(turnover_so_far, 2),
        "turnover_note": "仅有盘中累计成交额，无可核验全天成交额预测；volume_shock 必须缺失",
        "missing_indicators": sorted(
            field for field in (FINAL_ONLY_FIELDS | EXTREME_RAW_FIELDS) if field not in raw),
        "source_events": _unique_messages(source_events),
        "component_warnings": _unique_messages(component_warnings),
        "stale": False,
        "stale_reason": None,
        "waiting_final": False,
        "errors": _unique_messages(errors),
    }
    required_intraday = {"return_dispersion", "mean_abs_return", "breadth_extreme"}
    if not required_intraday.issubset(raw):
        return use_stale("盘中实时波动与宽度组件覆盖不足", errors)
    with _INTRADAY_LOCK:
        _INTRADAY_CACHE[target] = {"cached_at": now_mono, "payload": dict(raw)}
    common.atomic_write_json(_intraday_disk_path(target), raw)
    return raw, ""


def _ensure_raw(pro, dates: list[str], required_fields: Optional[set[str]] = None) -> dict[str, Any]:
    """只返回并持久化完整收盘数据；历史遗留的非 final 行必须重采，失败则排除。"""
    contract = factor_contract.base_contract("sentiment")
    db.save_factor_contract(contract)
    _, _, ready_date = _market_session()
    final_dates = [date for date in dates if date <= ready_date]
    cache = db.fetch_daily_sentiment(
        final_dates, contract["factor_version"], contract["schema_hash"])
    required = required_fields or set()
    for date in final_dates:
        current = cache.get(date, {})
        current_meta = current.get("_meta") if isinstance(current.get("_meta"), dict) else {}
        is_final = current_meta.get("is_final") is True
        needs_refresh = (not is_final or any(field not in current for field in required))
        if not needs_refresh:
            continue
        # 非完整旧行即使重采失败也不得继续参与历史温度计算。
        cache.pop(date, None)
        raw = _collect(pro, date)
        raw_meta = raw.get("_meta") if raw and isinstance(raw.get("_meta"), dict) else {}
        if raw and raw_meta.get("is_final") is True:
            db.upsert_daily_sentiment(
                date, raw, contract["factor_version"], contract["schema_hash"])
            cache[date] = raw
    return cache


def _context(pro, requested_end: str, needed_days: int,
             allow_intraday: bool = True,
             force_intraday_refresh: bool = False) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """统一构建情绪日期上下文，严格区分盘中临时快照与已完成日持久数据。"""
    requested = str(requested_end).replace("-", "")
    _, market_state, ready_date = _market_session()
    historical_end = min(requested, ready_date)
    dates = _trade_dates(pro, historical_end, needed_days)
    cache = _ensure_raw(pro, dates)
    meta: dict[str, Any] = {
        "requested_date": requested,
        "completed_date": ready_date,
        "last_data_ready_date": ready_date,
        "market_state": market_state,
        "data_mode": "final",
        "data_status": "final",
        "is_final": True,
        "fallback_reason": None,
    }
    if allow_intraday and requested == common.today_str() and requested > ready_date:
        intraday, unavailable_reason = _intraday_snapshot(
            requested, force_refresh=force_intraday_refresh)
        if intraday:
            dates = [*dates, requested]
            cache = {**cache, requested: intraday}
            intraday_meta = intraday.get("_meta") or {}
            meta.update({
                "data_mode": "intraday", "data_status": "provisional", "is_final": False,
                "intraday_as_of": intraday_meta.get("as_of"),
                "intraday_source": intraday_meta.get("source"),
                "provider_chain": intraday_meta.get("provider_chain") or [],
                "intraday_source_events": intraday_meta.get("source_events") or [],
                "intraday_component_warnings": intraday_meta.get("component_warnings") or [],
                "intraday_quality": {
                    "stock": intraday_meta.get("stock_coverage") or {},
                    "ohlc": intraday_meta.get("ohlc_coverage") or {},
                    "sector": intraday_meta.get("sector_coverage") or {},
                    "missing_indicators": intraday_meta.get("missing_indicators") or [],
                },
                "intraday_errors": intraday_meta.get("errors") or [],
                "intraday_stale": bool(intraday_meta.get("stale")),
                "intraday_stale_reason": intraday_meta.get("stale_reason"),
                "waiting_final": bool(intraday_meta.get("waiting_final")),
                "turnover_so_far_yuan": intraday_meta.get("turnover_so_far_yuan"),
                "turnover_note": intraday_meta.get("turnover_note"),
            })
        else:
            meta.update({
                "data_mode": "fallback", "is_final": True,
                "waiting_final": market_state == "closed_pending",
                "fallback_reason": (
                    f"{unavailable_reason}；所有实时源均未形成可核验的今日快照，"
                    f"仅保留 last_data_ready_date={ready_date} 的完整数据"),
            })
    elif requested > ready_date:
        meta.update({
            "data_mode": "fallback", "is_final": True,
            "waiting_final": market_state == "closed_pending",
            "fallback_reason": (
                f"请求日完整数据尚未就绪；last_data_ready_date={ready_date}，"
                "未使用不可核验数据补齐"),
        })
    return dates, cache, meta


def _temperature_for(cache: dict[str, Any], target: str, all_dates: list[str],
                     weights: dict[str, float], window_size: int = WINDOW_DEFAULT) -> Optional[dict[str, Any]]:
    """用 target 及其之前 window_size 个交易日窗口，按当前可用正权重归一计算温度。"""
    idx = all_dates.index(target) if target in all_dates else -1
    if idx < 0:
        return None
    window = [d for d in all_dates[max(0, idx - window_size):idx + 1] if d in cache]
    if target not in cache or len(window) < 2:
        return None
    today = cache[target]
    indicators: dict[str, Any] = {}
    configured_weight = 0.0
    weighted_subscores: list[tuple[str, float, float]] = []
    total_positive_weight = sum(max(0.0, float(weight)) for weight in weights.values())
    for ind, weight_value in weights.items():
        weight = float(weight_value)
        if weight <= 0 or ind not in today:
            continue
        series = [float(cache[d][ind]) for d in window if ind in cache[d]]
        if not series:
            continue
        sub = _minmax_sub(series, float(today[ind]))
        if ind in INVERSE_INDICATORS:
            sub = round(100.0 - sub, 1)
        mean = sum(series) / len(series)
        indicators[ind] = {"raw_today": round(float(today[ind]), 4), "sub_score": sub,
                           "window_min": round(min(series), 4),
                           "window_mean": round(mean, 4),
                           "window_max": round(max(series), 4),
                           "vs_mean": round(float(today[ind]) - mean, 4)}
        configured_weight += weight
        weighted_subscores.append((ind, weight, sub))
    if configured_weight <= 0 or total_positive_weight <= 0:
        return None
    exact_applied_weights = {
        ind: weight / configured_weight for ind, weight, _ in weighted_subscores
    }
    applied_weights = {
        ind: round(weight, 6) for ind, weight in exact_applied_weights.items()
    }
    temperature = round(sum(
        exact_applied_weights[ind] * sub for ind, _, sub in weighted_subscores), 1)
    level = next(name for th, name in LEVELS if temperature >= th)
    missing = [ind for ind, weight in weights.items()
               if float(weight) > 0 and ind not in indicators]
    return {"date": target, "temperature": temperature, "level": level,
            "indicators": indicators, "window_dates": window,
            "configured_weights": weights, "applied_weights": applied_weights,
            "weight_coverage": round(configured_weight / total_positive_weight, 4),
            "missing_indicators": missing,
            "breadth": {"adv": today.get("adv"), "dec": today.get("dec")}}


def finalize_daily_sentiment(date: Optional[str] = None) -> dict[str, Any]:
    """同步补齐指定交易日完整情绪原始指标；关键源缺失时失败并等待调度重试。"""
    clock = common.market_clock()
    target = str(date or clock["last_closed_trade_date"]).replace("-", "")
    if target > str(clock["last_data_ready_date"]):
        return {"status": "waiting", "date": target, "error": "尚未达到日终安全就绪线"}
    if not common.is_trade_open(target):
        return {"status": "skipped", "date": target, "error": "目标日期不是交易日"}
    contract = factor_contract.base_contract("sentiment")
    db.save_factor_contract(contract)
    required = set(EXTREME_RAW_FIELDS) | set(FINAL_ONLY_FIELDS) | set(INTRADAY_FIELDS) | {"adv", "dec"}
    required.update(
        name for name, weight in factor_config.effective_weights("sentiment").items()
        if float(weight) > 0)
    existing = db.fetch_daily_sentiment(
        [target], contract["factor_version"], contract["schema_hash"]).get(target)
    existing_meta = existing.get("_meta", {}) if isinstance(existing, dict) else {}
    if (existing_meta.get("is_final") is True
            and required.issubset(existing or {})):
        return {"status": "success", "date": target, "cached": True,
                "finalized_at": existing_meta.get("finalized_at") or existing_meta.get("collected_at")}
    raw = _collect(common.get_pro(), target)
    if not raw:
        return {"status": "failed", "date": target,
                "error": "Tushare/AkShare 日线或 Tushare 板块、涨跌停、指数数据尚未完整"}
    missing = sorted(required - set(raw))
    if missing:
        return {"status": "failed", "date": target,
                "error": f"完整情绪指标缺失：{','.join(missing)}"}
    db.upsert_daily_sentiment(
        target, raw, contract["factor_version"], contract["schema_hash"])
    metadata = raw.get("_meta") or {}
    return {"status": "success", "date": target, "cached": False,
            "source": metadata.get("source"), "finalized_at": metadata.get("finalized_at")}


@register("sentiment_temperature", "sentiment",
          "市场情绪温度0-100：盘中使用实时涨跌广度、板块广度、沪深300涨幅和全市场平均涨幅；"
          "涨跌停、成交额及K线形态等收盘指标盘后补齐并持久化，缺失权重按可用指标重新归一。",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "force_refresh", "type": "bool", "required": False, "default": False,
                   "desc": "仅当日连续交易时段有效；绕过盘中快照 TTL 强制刷新"}],
          returns="temperature / indicators / applied_weights / data_mode / is_final / requested_date")
def sentiment_temperature(p: dict) -> dict:
    pro = common.get_pro()
    end = str(p.get("date") or common.last_trade_date()).replace("-", "")
    win = _window()
    dates, cache, context = _context(
        pro, end, win + 1, allow_intraday=True,
        force_intraday_refresh=bool(p.get("force_refresh")))
    if not dates:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                "requested_date": end, "error": "无法获取交易日窗口"}
    weights = factor_config.effective_weights("sentiment")
    available = [date for date in dates if date in cache]
    effective_end = end if end in cache else (available[-1] if available else end)
    result = _temperature_for(cache, effective_end, dates, weights, win)
    if result is None and context.get("data_mode") == "intraday":
        # 当前权重若全部落在盘后指标，不能把单一或零权重实时指标伪装成当日温度。
        final_dates = [date for date in dates if date != end]
        final_available = [date for date in final_dates if date in cache]
        effective_end = final_available[-1] if final_available else end
        result = _temperature_for(cache, effective_end, final_dates, weights, win)
        context.update({
            "data_mode": "fallback", "is_final": True,
            "fallback_reason": "盘中可用指标与当前权重不足，已回退最近完整收盘日",
        })
    if result is None:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                **context, "error": "数据不足（窗口内有效交易日不足）"}
    if effective_end != end and not context.get("fallback_reason"):
        context.update({"data_mode": "fallback", "is_final": True,
                        "fallback_reason": "目标日数据尚不可用，已回退最近有效交易日"})
    selected_meta = cache.get(effective_end, {}).get("_meta") or {}
    context["is_final"] = selected_meta.get("is_final") is True
    mode_note = ("盘中临时温度，仅使用可核验实时指标；收盘指标尚未参与"
                 if context.get("data_mode") == "intraday" else "完整收盘口径")
    return {
        "source": "sentiment_temperature", "fetched_at": common.now_str(),
        "weights": weights, "factor_contract": factor_config.model_contract("sentiment"),
        "window_size": win, "effective_date": effective_end, **context,
        "note": f"0-100，越高越热；{mode_note}；按可用指标权重重新归一", **result,
    }


def _median(values: list[float]) -> float:
    """返回有限序列中位数。"""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _robust_percentile(history: list[float], current: float) -> Optional[float]:
    """用最多 20 个历史值的经验分位映射到 0-100；并列值取区间中点。"""
    values = [value for value in (_finite(item) for item in history) if value is not None]
    current_value = _finite(current)
    if current_value is None or len(values) < EXTREME_MIN_HISTORY:
        return None
    below = sum(value < current_value for value in values)
    equal = sum(value == current_value for value in values)
    return round(100.0 * (below + 0.5 * equal) / len(values), 1)


def _metric_percentile(cache: dict[str, Any], target_value: Any,
                       history_dates: list[str], field: str) -> Optional[dict[str, Any]]:
    """计算单个强度指标的经验分位及可审计历史摘要。"""
    history = [value for date in history_dates
               if (value := _finite(cache.get(date, {}).get(field))) is not None]
    current = _finite(target_value)
    score = _robust_percentile(history, current) if current is not None else None
    if score is None:
        return None
    return {
        "raw_today": round(current, 4),
        "score": score,
        "history_count": len(history),
        "history_median": round(_median(history), 4),
    }


def _volume_shock_raw(cache: dict[str, Any], target: str,
                      all_dates: list[str]) -> Optional[tuple[float, float]]:
    """计算相对此前最多 20 日成交额滚动中位数的无方向对数偏离。"""
    if target not in all_dates or cache.get(target, {}).get("_meta", {}).get("is_final") is not True:
        return None
    idx = all_dates.index(target)
    history = [value for date in all_dates[max(0, idx - EXTREME_WINDOW):idx]
               if (value := _finite(cache.get(date, {}).get("turnover"))) is not None and value > 0]
    current = _finite(cache.get(target, {}).get("turnover"))
    if current is None or current <= 0 or len(history) < EXTREME_MIN_HISTORY:
        return None
    baseline = _median(history)
    return abs(math.log(current / baseline)), baseline


def _extreme_for(cache: dict[str, Any], target: str,
                 all_dates: list[str]) -> Optional[dict[str, Any]]:
    """计算市场波动强度；所有指标均只表达异常幅度，不表达上涨或下跌方向。"""
    if target not in all_dates or target not in cache:
        return None
    idx = all_dates.index(target)
    history_dates = [date for date in all_dates[max(0, idx - EXTREME_WINDOW):idx]
                     if date in cache]
    if len(history_dates) < EXTREME_MIN_HISTORY:
        return None
    today = cache[target]
    components: dict[str, Any] = {}

    volatility_metrics = {
        field: detail for field in VOLATILITY_FIELDS
        if (detail := _metric_percentile(cache, today.get(field), history_dates, field))
    }
    if volatility_metrics:
        components["volatility"] = {
            "score": round(sum(item["score"] for item in volatility_metrics.values())
                           / len(volatility_metrics), 1),
            "metrics": volatility_metrics,
        }

    # 成交量冲击对放量和缩量一视同仁；盘中累计成交额绝不参与。
    current_volume = _volume_shock_raw(cache, target, all_dates)
    if current_volume:
        historical_shocks: list[float] = []
        for date in history_dates:
            value = _volume_shock_raw(cache, date, all_dates)
            if value:
                historical_shocks.append(value[0])
        score = _robust_percentile(historical_shocks[-EXTREME_WINDOW:], current_volume[0])
        if score is not None:
            components["volume_shock"] = {
                "score": score,
                "raw_today": round(current_volume[0], 6),
                "turnover_today": round(float(today["turnover"]), 2),
                "rolling_median": round(current_volume[1], 2),
                "history_count": min(len(historical_shocks), EXTREME_WINDOW),
            }

    kline_metrics = {
        field: detail for field in KLINE_SHOCK_FIELDS
        if (detail := _metric_percentile(cache, today.get(field), history_dates, field))
    }
    if kline_metrics:
        components["kline_shock"] = {
            "score": round(sum(item["score"] for item in kline_metrics.values())
                           / len(kline_metrics), 1),
            "metrics": kline_metrics,
        }

    breadth = _metric_percentile(
        cache, today.get("breadth_extreme"), history_dates, "breadth_extreme")
    if breadth:
        components["breadth_extreme"] = {"score": breadth["score"], "metric": breadth}
    limit = _metric_percentile(cache, today.get("limit_shock"), history_dates, "limit_shock")
    if limit:
        components["limit_shock"] = {"score": limit["score"], "metric": limit}

    available_weight = sum(EXTREME_WEIGHTS[name] for name in components)
    if available_weight <= 0:
        return None
    exact_applied = {name: EXTREME_WEIGHTS[name] / available_weight for name in components}
    applied_weights = {name: round(weight, 6) for name, weight in exact_applied.items()}
    extreme_index = round(sum(
        components[name]["score"] * exact_applied[name] for name in components), 1)
    level = next(name for threshold, name in EXTREME_LEVELS if extreme_index >= threshold)
    direction_note = "高值只表示市场更极端，不代表看多或看空。"
    if extreme_index >= 80:
        selection_bias = direction_note + "优先控制总仓位与单票暴露，缩短复核周期，严设止损"
    elif extreme_index >= 60:
        selection_bias = direction_note + "适度降低仓位和集中度，避免因振幅放大被动追涨杀跌"
    else:
        selection_bias = direction_note + "维持常规仓位纪律，仍需按个股风险独立设限"
    point_is_final = today.get("_meta", {}).get("is_final") is True
    return {
        "date": target,
        "extreme_index": extreme_index,
        "level": level,
        "components": components,
        "configured_weights": dict(EXTREME_WEIGHTS),
        "applied_weights": applied_weights,
        "component_coverage": round(available_weight / sum(EXTREME_WEIGHTS.values()), 4),
        "missing_components": [name for name in EXTREME_WEIGHTS if name not in components],
        "model_mode": "final" if point_is_final else "provisional",
        "window_dates": history_dates,
        "selection_bias": selection_bias,
    }


@register("sentiment_extreme_index", "sentiment",
          "市场波动强度指数 0-100：固定 20 日 robust 经验分位，综合波动率、成交量冲击、"
          "K线实体与影线、市场宽度极端度、涨跌停冲击；不表达多空方向。"
          "盘中仅使用可核验组件并按配置权重重新归一。",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "days", "type": "int", "required": False, "default": 15,
                   "desc": "返回走势的交易日数，3-30；不影响固定20日 robust 窗口"}],
          returns="extreme_index / components / configured_weights / applied_weights / component_coverage / missing_components / model_mode / recent")
def sentiment_extreme_index(p: dict) -> dict:
    pro = common.get_pro()
    end = str(p.get("date") or common.last_trade_date()).replace("-", "")
    days = max(3, min(30, int(p.get("days", 15))))
    dates, cache, context = _context(
        pro, end, days + EXTREME_WINDOW * 2, allow_intraday=True)
    if not dates:
        return {"source": "sentiment_extreme_index", "fetched_at": common.now_str(),
                **context, "error": "无法获取交易日窗口"}

    # 仅完整历史日允许重采并入库；当日 provisional 快照保留在内存/文件中，不进入数据库。
    final_dates = [date for date in dates
                   if cache.get(date, {}).get("_meta", {}).get("is_final") is True]
    cache.update(_ensure_raw(pro, final_dates, EXTREME_RAW_FIELDS))

    latest = _extreme_for(cache, end, dates)
    effective_end = end
    if latest is None:
        for candidate in reversed(dates):
            latest = _extreme_for(cache, candidate, dates)
            if latest:
                effective_end = candidate
                break
    if latest is None:
        return {"source": "sentiment_extreme_index", "fetched_at": common.now_str(),
                **context, "error": "数据不足（20日 robust 窗口最低需要8个可用历史日）"}
    if effective_end != end:
        context.update({
            "data_mode": "fallback", "data_status": "final", "is_final": True,
            "fallback_reason": context.get("fallback_reason")
            or "目标日没有足够可核验组件，已回退最近可计算的完整交易日",
        })
    else:
        context.update({
            "data_mode": latest["model_mode"],
            "data_status": latest["model_mode"],
            "is_final": latest["model_mode"] == "final",
        })

    recent: list[dict[str, Any]] = []
    for trade_date in dates:
        item = _extreme_for(cache, trade_date, dates)
        if item:
            recent.append({
                "date": trade_date,
                "extreme_index": item["extreme_index"],
                "level": item["level"],
                "model_mode": item["model_mode"],
                "is_final": item["model_mode"] == "final",
                "component_coverage": item["component_coverage"],
                "missing_components": item["missing_components"],
            })
    return {
        "source": "sentiment_extreme_index", "fetched_at": common.now_str(),
        "window_size": EXTREME_WINDOW,
        "minimum_history": EXTREME_MIN_HISTORY,
        "effective_date": effective_end,
        "recent": recent[-days:], **context,
        "note": "固定20日 robust 历史分位的多维波动强度；高值只表示市场更极端，不代表看多或看空。盘中仅用可核验组件并按配置权重重新归一，无全天成交额预测时 volume_shock 与不完整涨跌停冲击缺失。",
        **latest,
    }


@register("market_timing", "sentiment",
          "择时判断：盘中序列可包含当日临时温度，历史点仅使用完整收盘数据；识别连续冰点/高热并给出仓位倾向",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "days", "type": "int", "required": False, "default": 5, "desc": "回看交易日数"}],
          returns="recent / date / requested_date / data_mode / cold_streak / hot_streak / stance")
def market_timing(p: dict) -> dict:
    pro = common.get_pro()
    end = str(p.get("date") or common.last_trade_date()).replace("-", "")
    k = max(3, min(30, int(p.get("days", 5))))
    win = _window()
    dates, cache, context = _context(pro, end, k + win, allow_intraday=True)
    if not dates:
        return {"source": "market_timing", "fetched_at": common.now_str(),
                **context, "error": "无法获取交易日窗口"}
    weights = factor_config.effective_weights("sentiment")

    recent = []
    for trade_date in dates[-k:]:
        result = _temperature_for(cache, trade_date, dates, weights, win)
        if result:
            point_meta = cache.get(trade_date, {}).get("_meta") or {}
            point_is_final = point_meta.get("is_final") is True
            recent.append({
                "date": trade_date, "temperature": result["temperature"],
                "level": result["level"],
                "data_mode": "final" if point_is_final else "intraday",
                "is_final": point_is_final,
            })
    if not recent:
        return {"source": "market_timing", "fetched_at": common.now_str(),
                **context, "error": "温度序列不足"}
    effective_end = recent[-1]["date"]
    if effective_end != end:
        context.update({
            "data_mode": "fallback", "is_final": True,
            "fallback_reason": context.get("fallback_reason")
            or "目标日温度不可用，已回退最近完整收盘日",
        })
    else:
        context.update({
            "data_mode": recent[-1]["data_mode"],
            "is_final": recent[-1]["is_final"],
        })

    # 尾部连续冰点/高热计数
    cold_streak = 0
    for x in reversed(recent):
        if x["temperature"] < 20:
            cold_streak += 1
        else:
            break
    hot_streak = 0
    for x in reversed(recent):
        if x["temperature"] >= 80:
            hot_streak += 1
        else:
            break

    latest = recent[-1]["temperature"]
    if cold_streak >= 2:
        stance = f"连续 {cold_streak} 日冰点：超跌/抄底窗口，**提高出手买入权重**（分批试仓，重逻辑/涨价支撑标的）"
        buy_weight_hint = round(min(1.5, 1.0 + 0.15 * cold_streak), 2)
    elif hot_streak >= 2:
        stance = f"连续 {hot_streak} 日高热：**警惕退潮**，降低仓位/兑现，不追高，可部分空仓"
        buy_weight_hint = round(max(0.5, 1.0 - 0.15 * hot_streak), 2)
    elif latest >= 80:
        stance = "单日高热：谨慎追高，关注退潮信号"
        buy_weight_hint = 0.8
    elif latest < 20:
        stance = "单日冰点：情绪低迷，可小仓试错超跌反弹"
        buy_weight_hint = 1.15
    else:
        stance = "情绪中性：按常规四维打分出手，仓位适中"
        buy_weight_hint = 1.0

    return {
        "source": "market_timing", "fetched_at": common.now_str(),
        "date": recent[-1]["date"], **context,
        "recent": recent,
        "cold_streak": cold_streak,
        "hot_streak": hot_streak,
        "latest_temperature": latest,
        "factor_contract": factor_config.model_contract("sentiment"),
        "stance": stance,
        "buy_weight_hint": buy_weight_hint,
        "note": "盘中末点为临时温度，收盘后补齐完整指标；冰点连续→提高买入权重，高热连续→警惕退潮",
    }


@register("get_sentiment_config", "sentiment",
          "获取情绪配置：归一窗口天数（当天之前 N 个交易日）及允许范围，含当前窗口的留痕版本号",
          params=[], returns="window / range / version_id / actor")
def get_sentiment_config(p: dict) -> dict:
    meta = db.get_config(WINDOW_CONFIG_KEY)
    version_id = meta.get("version_id") if isinstance(meta, dict) else None
    actor = meta.get("actor") if isinstance(meta, dict) else None
    return {"source": "sentiment_config", "fetched_at": common.now_str(),
            "window": _window(), "range": [WINDOW_MIN, WINDOW_MAX], "default": WINDOW_DEFAULT,
            "version_id": version_id, "actor": actor}


@register("set_sentiment_config", "sentiment",
          "设置情绪归一窗口天数（3-30，落库持久化）。影响 sentiment_temperature / market_timing。"
          "每次修改留痕为类 commit 的 version_id（署名 actor），可用 get_config_history(config_key=sentiment_window) 定位",
          params=[{"name": "window", "type": "int", "required": True, "desc": "3-30 之间的交易日数"},
                  {"name": "actor", "type": "string", "required": False, "default": "agent",
                   "desc": "修改者身份署名"},
                  {"name": "reason", "type": "string", "required": False, "default": "",
                   "desc": "修改原因（回测/背离说明等）"}],
          returns="applied / window / version_id")
def set_sentiment_config(p: dict) -> dict:
    w = int(p["window"])
    if not (WINDOW_MIN <= w <= WINDOW_MAX):
        return {"applied": False, "error": f"window 须在 {WINDOW_MIN}-{WINDOW_MAX} 之间", "given": w}
    actor = p.get("actor") or "agent"
    reason = p.get("reason") or ""
    ver = db.record_config_version(WINDOW_CONFIG_KEY, {"window": w}, actor, reason)
    db.set_config(WINDOW_CONFIG_KEY, {"window": w, "version_id": ver["version_id"],
                                      "actor": actor, "reason": reason})
    return {"applied": True, "window": w, "version_id": ver["version_id"],
            "parent_version": ver.get("parent_version"), "actor": actor,
            "note": "已保存并留痕；后续情绪温度/择时按新窗口归一（可能需重采窗口内数据）"}


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(sentiment_temperature({}), ensure_ascii=False, indent=2))

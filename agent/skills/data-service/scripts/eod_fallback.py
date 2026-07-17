"""日终全市场行情适配：优先 Tushare，仅在当日收盘后用 AkShare 严格兜底。"""
from __future__ import annotations

from datetime import datetime, time as clock_time
from typing import Any

import pandas as pd

import common

_REQUIRED = {
    "ts_code", "trade_date", "open", "high", "low", "close",
    "pre_close", "pct_chg", "vol", "amount",
}


def _validate(frame: pd.DataFrame, target: str, provider: str) -> pd.DataFrame:
    """校验目标日期、字段、代码唯一性和现代市场最低覆盖，不接受旧日或局部样本。"""
    if frame is None or frame.empty:
        raise RuntimeError(f"{provider} 未返回日终行情")
    missing = sorted(_REQUIRED - set(frame.columns))
    if missing:
        raise RuntimeError(f"{provider} 日终行情缺少字段：{','.join(missing)}")
    normalized = frame.copy()
    normalized["trade_date"] = normalized["trade_date"].astype(str).str.replace("-", "", regex=False)
    normalized = normalized[normalized["trade_date"] == target].copy()
    normalized["ts_code"] = normalized["ts_code"].astype(str).str.upper().str.strip()
    normalized = normalized.drop_duplicates("ts_code")
    minimum = 4500 if target >= "20240101" else 1000
    if len(normalized) < minimum:
        raise RuntimeError(f"{provider} 目标日覆盖不足：{len(normalized)} < {minimum}")
    for column in _REQUIRED - {"ts_code", "trade_date"}:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    valid = normalized.dropna(subset=["open", "high", "low", "close", "pre_close", "pct_chg"])
    if len(valid) / len(normalized) < 0.9:
        raise RuntimeError(f"{provider} 有效 OHLC 覆盖不足：{len(valid)}/{len(normalized)}")
    normalized["_provider"] = provider
    return normalized


def _code(value: Any) -> str:
    text = str(value or "").strip().zfill(6)
    suffix = "SH" if text.startswith("6") else ("BJ" if text.startswith(("4", "8", "9")) else "SZ")
    return f"{text}.{suffix}"


def _akshare_today(target: str) -> pd.DataFrame:
    """仅在今天收盘后抓取 AkShare 全市场快照，并用 Tushare 指数日期锚防止陈旧数据。"""
    now = common.shanghai_now()
    if target != common.today_str() or now.time().replace(tzinfo=None) < clock_time(15, 0):
        raise RuntimeError("AkShare 快照兜底只允许用于今天收盘后")
    try:
        import tushare as ts
        anchor = ts.realtime_quote(ts_code="000300.SH")
        anchor_date = str(anchor.iloc[0].get("DATE") or "").replace("-", "")
    except Exception as exc:
        raise RuntimeError(f"Tushare 日期锚不可用，拒绝 AkShare 快照：{exc}") from exc
    if anchor_date != target:
        raise RuntimeError(f"AkShare 兜底日期锚不属于目标日：{anchor_date or '空'}")
    try:
        import akshare as ak
        raw = ak.stock_zh_a_spot_em()
    except Exception as exc:
        raise RuntimeError(f"AkShare 全市场快照失败：{type(exc).__name__}: {exc}") from exc
    columns = {"代码", "今开", "最高", "最低", "最新价", "昨收", "涨跌幅", "成交量", "成交额"}
    if raw is None or raw.empty or not columns.issubset(raw.columns):
        missing = sorted(columns - set(raw.columns if raw is not None else []))
        raise RuntimeError(f"AkShare 全市场快照字段不完整：{','.join(missing)}")
    frame = pd.DataFrame({
        "ts_code": raw["代码"].map(_code), "trade_date": target,
        "open": raw["今开"], "high": raw["最高"], "low": raw["最低"],
        "close": raw["最新价"], "pre_close": raw["昨收"], "pct_chg": raw["涨跌幅"],
        "vol": raw["成交量"],
        # AkShare 东财快照成交额为元；统一换算为 Tushare daily 的千元口径。
        "amount": pd.to_numeric(raw["成交额"], errors="coerce") / 1000.0,
    })
    return _validate(frame, target, "akshare")


def fetch_daily_slice(pro: Any, target: str) -> pd.DataFrame:
    """优先获取 Tushare 日线；失败、空结果或覆盖不合格时尝试等价 AkShare 当日兜底。"""
    errors: list[str] = []
    try:
        tushare_frame = pro.daily(trade_date=target)
        return _validate(tushare_frame, target, "tushare")
    except Exception as exc:
        errors.append(f"Tushare：{type(exc).__name__}: {exc}")
    try:
        return _akshare_today(target)
    except Exception as exc:
        errors.append(f"AkShare：{type(exc).__name__}: {exc}")
    raise RuntimeError("；".join(errors)[:1000])

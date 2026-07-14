"""市场情绪温度（0-100）。

综合六项指标：大盘指数动量、板块涨跌比、大盘涨跌家数比、平均股价指数动量、
大盘成交额、涨跌停家数。各指标"越高越热"，按**当天之前 7 个交易日**窗口做
min-max 归一为 0-100 子分，再按情绪指标权重（factor_config 的 sentiment 模型）加权合成。

原始指标按交易日缓存到 DATA_DIR/sentiment_cache.json（滚动保留 30 个交易日），
避免重复全市场取数。

数据来源（每交易日）：
- daily(trade_date=全市场)：涨跌家数比、平均股价、成交额（一次调用取全市场）
- dc_index：板块涨跌比
- limit_list_d：涨跌停家数
- index_daily(000300.SH)：大盘指数动量
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import db
import factor_config
from registry import register

WINDOW = 7                # 当天之前 7 个交易日
BENCH_INDEX = "000300.SH"

LEVELS = [(80, "高潮"), (60, "回暖"), (40, "分歧"), (20, "退潮"), (0, "冰点")]


def _trade_dates(pro, end: str, n: int) -> list[str]:
    """返回截至 end（含）的最近 n 个交易日（升序）。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=n * 2 + 20)).strftime("%Y%m%d")
    df = pro.index_daily(ts_code=BENCH_INDEX, start_date=start, end_date=end)
    if df is None or df.empty:
        return []
    dates = sorted(df["trade_date"].astype(str).tolist())
    return dates[-n:]


def _collect(pro, date: str) -> Optional[dict[str, float]]:
    """采集单个交易日的六项原始指标。"""
    try:
        daily = pro.daily(trade_date=date)
    except Exception:
        daily = None
    if daily is None or daily.empty:
        return None
    pct = daily["pct_chg"].astype(float)
    adv = int((pct > 0).sum())
    dec = int((pct < 0).sum())
    adv_dec_ratio = adv / (adv + dec) if (adv + dec) else 0.5
    avg_chg = float(pct.mean())  # 全市场平均涨跌幅（以涨幅锚定，越高越热）
    turnover = float(daily["amount"].astype(float).sum())  # 千元

    # 板块涨跌比
    sector_ratio = 0.5
    try:
        sec = pro.dc_index(trade_date=date)
        col = "pct_change" if "pct_change" in sec.columns else ("pct_chg" if "pct_chg" in sec.columns else None)
        if col:
            sp = sec[col].astype(float)
            su, sd = int((sp > 0).sum()), int((sp < 0).sum())
            sector_ratio = su / (su + sd) if (su + sd) else 0.5
    except Exception:
        pass

    # 涨跌停家数（涨停占比）
    limit_updown = 0.5
    try:
        lim = pro.limit_list_d(trade_date=date)
        if lim is not None and not lim.empty and "limit" in lim.columns:
            u = int((lim["limit"] == "U").sum())
            d = int((lim["limit"] == "D").sum())
            limit_updown = u / (u + d) if (u + d) else 1.0
    except Exception:
        pass

    # 大盘指数动量（当日涨跌幅）+ 当天K线形态（收盘强弱 + 阳阴实体）
    index_mom = 0.0
    index_kline = 0.5
    try:
        idx = pro.index_daily(ts_code=BENCH_INDEX, trade_date=date)
        if idx is not None and not idx.empty:
            row = idx.iloc[0]
            index_mom = float(row["pct_chg"])
            o, h, l, cl = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            rng = h - l
            if rng > 0:
                pos = max(0.0, min(1.0, (cl - l) / rng))   # 收盘在日内区间位置（越高越强）
                body = (cl - o) / rng                        # 实体方向与占比，∈[-1,1]
                index_kline = round(0.5 * pos + 0.5 * (body + 1) / 2, 4)  # 0~1，越高越强/越阳
    except Exception:
        pass

    return {
        "adv_dec_ratio": round(adv_dec_ratio, 4),
        "limit_updown": round(limit_updown, 4),
        "index_kline": index_kline,
        "sector_ratio": round(sector_ratio, 4),
        "turnover": round(turnover, 2),
        "index_mom": round(index_mom, 4),
        "avg_price_mom": round(avg_chg, 4),  # 全市场平均涨跌幅（涨幅锚定）
        "adv": adv, "dec": dec,
    }


def _minmax_sub(values: list[float], today: float) -> float:
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 50.0
    return round(100 * (today - lo) / (hi - lo), 1)


def _ensure_raw(pro, dates: list[str]) -> dict[str, Any]:
    """确保 dates 的原始指标已落库，缺失则采集并 upsert。返回 {date: indicators}。"""
    cache = db.fetch_daily_sentiment(dates)
    for d in dates:
        if d not in cache:
            raw = _collect(pro, d)
            if raw:
                db.upsert_daily_sentiment(d, raw)
                cache[d] = raw
    return cache


def _temperature_for(cache: dict[str, Any], target: str, all_dates: list[str],
                     weights: dict[str, float]) -> Optional[dict[str, Any]]:
    """用 target 及其之前 7 个交易日窗口，计算 target 当日的情绪温度。"""
    idx = all_dates.index(target) if target in all_dates else -1
    if idx < 0:
        return None
    window = [d for d in all_dates[max(0, idx - WINDOW):idx + 1] if d in cache]
    if target not in cache or len(window) < 2:
        return None
    today = cache[target]
    indicators: dict[str, Any] = {}
    temperature = 0.0
    for ind in weights:
        series = [float(cache[d][ind]) for d in window if ind in cache[d]]
        if not series:
            continue
        sub = _minmax_sub(series, float(today[ind]))
        mean = sum(series) / len(series)
        indicators[ind] = {"raw_today": round(float(today[ind]), 4), "sub_score": sub,
                           "window_min": round(min(series), 4),
                           "window_mean": round(mean, 4),
                           "window_max": round(max(series), 4),
                           "vs_mean": round(float(today[ind]) - mean, 4)}
        temperature += weights[ind] * sub
    temperature = round(temperature, 1)
    level = next(name for th, name in LEVELS if temperature >= th)
    return {"date": target, "temperature": temperature, "level": level,
            "indicators": indicators, "window_dates": window,
            "breadth": {"adv": today.get("adv"), "dec": today.get("dec")}}


@register("sentiment_temperature", "sentiment",
          "市场情绪温度 0-100：综合大盘指数/板块涨跌比/涨跌家数比/平均股价/成交额/涨跌停，"
          "按当天之前7个交易日窗口归一加权。指标权重见 get_factor_config(model=sentiment)",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"}],
          returns="temperature(0-100) / level / indicators(raw+sub) / weights")
def sentiment_temperature(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    dates = _trade_dates(pro, end, WINDOW + 1)
    if not dates:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                "error": "无法获取交易日窗口"}
    cache = _ensure_raw(pro, dates)
    weights = factor_config.effective_weights("sentiment")
    # 当天 EOD 行情可能未出：回退到窗口内最近一个有数据的交易日
    avail = [d for d in dates if d in cache]
    eff_end = end if end in cache else (avail[-1] if avail else end)
    r = _temperature_for(cache, eff_end, dates, weights)
    if r is None:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                "error": "数据不足（窗口内有效交易日不足）"}
    return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
            "weights": weights, "note": "0-100，越高越热；子分按当天之前7个交易日窗口 min-max 归一", **r}


@register("market_timing", "sentiment",
          "择时判断：计算最近若干交易日情绪温度序列，识别连续冰点/高热，给出出手买入权重提示与仓位倾向",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "days", "type": "int", "required": False, "default": 5, "desc": "回看交易日数"}],
          returns="recent(温度序列) / cold_streak / hot_streak / stance / buy_weight_hint")
def market_timing(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    k = int(p.get("days", 5))
    dates = _trade_dates(pro, end, k + WINDOW)
    if not dates:
        return {"source": "market_timing", "fetched_at": common.now_str(), "error": "无法获取交易日窗口"}
    cache = _ensure_raw(pro, dates)
    weights = factor_config.effective_weights("sentiment")

    recent = []
    for d in dates[-k:]:
        r = _temperature_for(cache, d, dates, weights)
        if r:
            recent.append({"date": d, "temperature": r["temperature"], "level": r["level"]})
    if not recent:
        return {"source": "market_timing", "fetched_at": common.now_str(), "error": "温度序列不足"}

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
        "source": "market_timing",
        "fetched_at": common.now_str(),
        "date": end,
        "recent": recent,
        "cold_streak": cold_streak,
        "hot_streak": hot_streak,
        "latest_temperature": latest,
        "stance": stance,
        "buy_weight_hint": buy_weight_hint,
        "note": "择时结论用于调节选股出手权重与仓位；冰点连续→提高买入权重，高热连续→警惕退潮",
    }


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(sentiment_temperature({}), ensure_ascii=False, indent=2))

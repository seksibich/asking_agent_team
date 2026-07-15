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
import factor_contract
import factor_config
from registry import register

WINDOW_DEFAULT = 7        # 默认：当天之前 7 个交易日
WINDOW_MIN, WINDOW_MAX = 3, 30
WINDOW_CONFIG_KEY = "sentiment_window"
BENCH_INDEX = "000300.SH"
EXTREME_WINDOW = 7       # 固定：含当日在内的最近 7 个交易日，不支持配置
EXTREME_REQUIRED_FIELDS = {"market_amp", "turnover"}

LEVELS = [(80, "高潮"), (60, "回暖"), (40, "分歧"), (20, "退潮"), (0, "冰点")]
EXTREME_LEVELS = [(80, "高极端"), (60, "偏极端"), (40, "中度波动"), (0, "相对平稳")]

# 反向指标：原始值越大越"冷"，子分取反（100 - minmax），从而拉低温度
INVERSE_INDICATORS = {"limit_down"}


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

    # 全市场平均日内振幅：(high-low)/pre_close*100，越大表示市场波动越极端。
    market_amp = 0.0
    try:
        ranges = daily[["high", "low", "pre_close"]].astype(float)
        ranges = ranges[ranges["pre_close"] > 0]
        if not ranges.empty:
            market_amp = float(((ranges["high"] - ranges["low"]) / ranges["pre_close"] * 100.0).mean())
    except Exception:
        pass

    # 平均股价指数：全市场个股 OHLC 等权平均，构造"平均一只票"的K线，
    # 实体/振幅均按【百分点位】口径（相对平均前收），而非绝对价格数值
    avg_price_body = 0.0   # 实体：阳线正、阴线负，绝对值=实体长度（百分点）
    avg_price_amp = 0.0    # 振幅方向信号：下影线净多于上影线为正（越高越偏多）
    try:
        a_open = float(daily["open"].astype(float).mean())
        a_high = float(daily["high"].astype(float).mean())
        a_low = float(daily["low"].astype(float).mean())
        a_close = float(daily["close"].astype(float).mean())
        a_pre = float(daily["pre_close"].astype(float).mean())
        if a_pre > 0:
            avg_price_body, avg_price_amp = _kline_signals(a_open, a_high, a_low, a_close, a_pre)
    except Exception:
        pass

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

    # 涨停 / 跌停家数（独立计分：涨停正向、跌停反向）
    limit_up, limit_down = 0, 0
    try:
        lim = pro.limit_list_d(trade_date=date)
        if lim is not None and not lim.empty and "limit" in lim.columns:
            limit_up = int((lim["limit"] == "U").sum())
            limit_down = int((lim["limit"] == "D").sum())
    except Exception:
        pass

    # 大盘指数动量（当日涨跌幅）+ 按【百分点位】口径的大盘振幅方向信号与实体长度
    # （实体+影线已覆盖原 index_kline 的收盘强弱/阳阴实体语义，故不再单列 K 线形态因子）
    index_mom = 0.0
    index_body = 0.0
    index_amp = 0.0
    try:
        idx = pro.index_daily(ts_code=BENCH_INDEX, trade_date=date)
        if idx is not None and not idx.empty:
            row = idx.iloc[0]
            index_mom = float(row["pct_chg"])
            o, h, l, cl = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            pre = float(row["pre_close"]) if "pre_close" in idx.columns else 0.0
            if pre > 0:
                index_body, index_amp = _kline_signals(o, h, l, cl, pre)
    except Exception:
        pass

    return {
        "adv_dec_ratio": round(adv_dec_ratio, 4),
        "limit_up": limit_up,
        "limit_down": limit_down,
        "sector_ratio": round(sector_ratio, 4),
        "turnover": round(turnover, 2),
        "market_amp": round(market_amp, 4),       # 全市场平均日内振幅（百分点）
        "index_mom": round(index_mom, 4),
        "avg_price_mom": round(avg_chg, 4),  # 全市场平均涨跌幅（涨幅锚定）
        "index_body": index_body,            # 大盘指数实体（百分点，阳正阴负）
        "index_amp": index_amp,              # 大盘指数振幅方向信号（下影净偏多，百分点）
        "avg_price_body": avg_price_body,    # 平均股价指数实体（百分点，阳正阴负）
        "avg_price_amp": avg_price_amp,      # 平均股价指数振幅方向信号（下影净偏多，百分点）
        "adv": adv, "dec": dec,
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


def _minmax_sub(values: list[float], today: float) -> float:
    lo, hi = min(values), max(values)
    if hi <= lo:
        return 50.0
    return round(100 * (today - lo) / (hi - lo), 1)


def _ensure_raw(pro, dates: list[str], required_fields: Optional[set[str]] = None) -> dict[str, Any]:
    """只复用当前情绪公式契约的原始指标；旧版或缺字段记录重新采集。"""
    contract = factor_contract.base_contract("sentiment")
    db.save_factor_contract(contract)
    cache = db.fetch_daily_sentiment(
        dates, contract["factor_version"], contract["schema_hash"])
    required = required_fields or set()
    for date in dates:
        current = cache.get(date, {})
        if date not in cache or any(field not in current for field in required):
            raw = _collect(pro, date)
            if raw:
                db.upsert_daily_sentiment(
                    date, raw, contract["factor_version"], contract["schema_hash"])
                cache[date] = raw
    return cache


def _temperature_for(cache: dict[str, Any], target: str, all_dates: list[str],
                     weights: dict[str, float], window_size: int = WINDOW_DEFAULT) -> Optional[dict[str, Any]]:
    """用 target 及其之前 window_size 个交易日窗口，计算 target 当日的情绪温度。"""
    idx = all_dates.index(target) if target in all_dates else -1
    if idx < 0:
        return None
    window = [d for d in all_dates[max(0, idx - window_size):idx + 1] if d in cache]
    if target not in cache or len(window) < 2:
        return None
    today = cache[target]
    indicators: dict[str, Any] = {}
    temperature = 0.0
    for ind in weights:
        if ind not in today:
            continue   # 旧缓存缺该指标（如新增因子）：跳过，待窗口内数据滚动补齐
        series = [float(cache[d][ind]) for d in window if ind in cache[d]]
        if not series:
            continue
        sub = _minmax_sub(series, float(today[ind]))
        if ind in INVERSE_INDICATORS:
            sub = round(100.0 - sub, 1)   # 跌停越多 → 子分越低 → 拉低温度
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
          "并叠加大盘与平均股价指数的振幅方向信号+实体长度(百分点位,低权重)，"
          "按当天之前7个交易日窗口归一加权。指标权重见 get_factor_config(model=sentiment)",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"}],
          returns="temperature(0-100) / level / indicators(raw+sub) / weights")
def sentiment_temperature(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    win = _window()
    dates = _trade_dates(pro, end, win + 1)
    if not dates:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                "error": "无法获取交易日窗口"}
    cache = _ensure_raw(pro, dates)
    weights = factor_config.effective_weights("sentiment")
    # 当天 EOD 行情可能未出：回退到窗口内最近一个有数据的交易日
    avail = [d for d in dates if d in cache]
    eff_end = end if end in cache else (avail[-1] if avail else end)
    r = _temperature_for(cache, eff_end, dates, weights, win)
    if r is None:
        return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
                "error": "数据不足（窗口内有效交易日不足）"}
    return {"source": "sentiment_temperature", "fetched_at": common.now_str(),
            "weights": weights, "factor_contract": factor_config.model_contract("sentiment"),
            "window_size": win,
            "note": f"0-100，越高越热；子分按当天之前 {win} 个交易日窗口 min-max 归一", **r}


def _extreme_for(cache: dict[str, Any], target: str,
                 all_dates: list[str]) -> Optional[dict[str, Any]]:
    """按含当日在内的固定 7 个交易日计算市场情绪极端指数。"""
    idx = all_dates.index(target) if target in all_dates else -1
    if idx < EXTREME_WINDOW - 1:
        return None
    window_dates = all_dates[idx - EXTREME_WINDOW + 1:idx + 1]
    if any(d not in cache or any(field not in cache[d] for field in EXTREME_REQUIRED_FIELDS)
           for d in window_dates):
        return None

    amplitudes = [float(cache[d]["market_amp"]) for d in window_dates]
    turnovers = [float(cache[d]["turnover"]) for d in window_dates]
    amplitude_score = _minmax_sub(amplitudes, amplitudes[-1])
    volume_shrink_score = round(100.0 - _minmax_sub(turnovers, turnovers[-1]), 1)
    extreme_index = round((amplitude_score + volume_shrink_score) / 2.0, 1)
    level = next(name for threshold, name in EXTREME_LEVELS if extreme_index >= threshold)

    if extreme_index >= 80:
        selection_bias = "行情高度极端：强倾向分析连板股与断板反包股，仍须过滤高位、流动性和逻辑风险"
    elif extreme_index >= 60:
        selection_bias = "行情偏极端：适度提高连板股与断板反包股的候选分析优先级"
    else:
        selection_bias = "行情极端度有限：不额外提高连板与断板反包候选优先级"

    return {
        "date": target,
        "extreme_index": extreme_index,
        "level": level,
        "components": {
            "amplitude": {
                "raw_today": round(amplitudes[-1], 4),
                "normalized_7d": amplitude_score,
                "window_min": round(min(amplitudes), 4),
                "window_max": round(max(amplitudes), 4),
            },
            "volume_shrink": {
                "raw_today": round(turnovers[-1], 2),
                "normalized_7d": volume_shrink_score,
                "window_min": round(min(turnovers), 2),
                "window_max": round(max(turnovers), 2),
            },
        },
        "window_dates": window_dates,
        "selection_bias": selection_bias,
    }


@register("sentiment_extreme_index", "sentiment",
          "市场情绪极端指数 0-100：全市场平均日内振幅越大、成交额越缩小则越极端；"
          "两项固定各占50%，按含当日在内最近7个交易日归一，不支持配置。"
          "指数越高，越倾向分析连板股与断板反包股",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "days", "type": "int", "required": False, "default": 15,
                   "desc": "返回走势的交易日数，3-30；不影响固定7日归一窗口"}],
          returns="extreme_index / level / components / recent / selection_bias / window_dates")
def sentiment_extreme_index(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    days = max(3, min(30, int(p.get("days", 15))))
    dates = _trade_dates(pro, end, days + EXTREME_WINDOW - 1)
    if not dates:
        return {"source": "sentiment_extreme_index", "fetched_at": common.now_str(),
                "error": "无法获取交易日窗口"}
    cache = _ensure_raw(pro, dates, EXTREME_REQUIRED_FIELDS)
    available = [d for d in dates
                 if d in cache and all(field in cache[d] for field in EXTREME_REQUIRED_FIELDS)]
    effective_end = end if end in available else (available[-1] if available else end)
    latest = _extreme_for(cache, effective_end, dates)
    if latest is None:
        return {"source": "sentiment_extreme_index", "fetched_at": common.now_str(),
                "error": "数据不足（需要完整7个交易日的振幅与成交额）"}

    recent: list[dict[str, Any]] = []
    for trade_date in dates:
        item = _extreme_for(cache, trade_date, dates)
        if item:
            recent.append({"date": trade_date, "extreme_index": item["extreme_index"],
                           "level": item["level"]})
    return {
        "source": "sentiment_extreme_index",
        "fetched_at": common.now_str(),
        "window_size": EXTREME_WINDOW,
        "weights": {"amplitude": 0.5, "volume_shrink": 0.5},
        "recent": recent[-days:],
        "note": "固定7日归一（含当日）：全市场平均振幅与缩量程度各占50%，不支持配置",
        **latest,
    }


@register("market_timing", "sentiment",
          "择时判断：计算最近若干交易日情绪温度序列，识别连续冰点/高热，给出出手买入权重提示与仓位倾向",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "days", "type": "int", "required": False, "default": 5, "desc": "回看交易日数"}],
          returns="recent(温度序列) / cold_streak / hot_streak / stance / buy_weight_hint")
def market_timing(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    k = int(p.get("days", 5))
    win = _window()
    dates = _trade_dates(pro, end, k + win)
    if not dates:
        return {"source": "market_timing", "fetched_at": common.now_str(), "error": "无法获取交易日窗口"}
    cache = _ensure_raw(pro, dates)
    weights = factor_config.effective_weights("sentiment")

    recent = []
    for d in dates[-k:]:
        r = _temperature_for(cache, d, dates, weights, win)
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
        "factor_contract": factor_config.model_contract("sentiment"),
        "stance": stance,
        "buy_weight_hint": buy_weight_hint,
        "note": "择时结论用于调节选股出手权重与仓位；冰点连续→提高买入权重，高热连续→警惕退潮",
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

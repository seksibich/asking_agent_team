"""量化多因子选股（趋势 + 情绪，回测有效因子）。

因子引擎见 scripts/factors.py。个股层面采用：
12-1 动量 + 1个月反转 + 低波动 + 低换手 + 趋势确认 + 量能确认。
（个股短期为反转，故用 reversal_1m；中期趋势用 mom_12_1。）

底层数据来自 tushare daily / daily_basic。因子横截面标准化后加权合成排名。
量化候选须由 Agent 叠加 涨价>逻辑>预期>情绪 四维交叉验证后方可入选。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import db
import factors
import factor_config
from registry import register


def _table_from_db(end: str, code_filter: Optional[set]) -> Optional[pd.DataFrame]:
    """优先从预计算的 daily_factors 读当日全市场因子，构建因子表。"""
    try:
        rows = db.fetch_daily_factors(end)
    except Exception:
        return None
    if not rows:
        return None
    recs = []
    for r in rows:
        code = r["code"]
        if code_filter is not None and code not in code_filter:
            continue
        fac = dict(r["factors"])
        fac["code"] = code
        recs.append(fac)
    return pd.DataFrame(recs) if recs else None


def _attach_quotes(rows: list[dict]) -> None:
    """给候选补最新行情：最新价(最近收盘)、当日涨幅、近3日/近5日涨幅。

    用 daily(historical=True) 永久缓存，同日重复选股命中缓存、不重复打 tushare。
    """
    if not rows:
        return
    end = common.last_trade_date()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d")
    for r in rows:
        code = r.get("code")
        try:
            payload = common.cached_call(
                "daily", {"c": code, "s": start, "e": end},
                lambda c=code: common.get_pro().daily(ts_code=c, start_date=start, end_date=end),
                historical=True)
            df = pd.DataFrame(payload.get("rows", []))
            if df.empty:
                continue
            df = df.sort_values("trade_date")
            cl = df["close"].astype(float).tolist()
            r["last"] = round(cl[-1], 2)
            if "pct_chg" in df.columns:
                r["chg"] = round(float(df.iloc[-1]["pct_chg"]), 2)
            if len(cl) >= 4:
                r["ret3"] = round((cl[-1] / cl[-4] - 1) * 100, 2)
            if len(cl) >= 6:
                r["ret5"] = round((cl[-1] / cl[-6] - 1) * 100, 2)
        except Exception:
            continue


def _attach_quotes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为候选补最新行情：最新价 last / 当日涨幅 chg / 近5日涨幅 ret5。

    用日线（永久缓存，end=最近交易日不可变、跨次复用），不逐次打实时接口。
    """
    if not rows:
        return rows
    end = common.last_trade_date()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d")
    pro = common.get_pro()
    for r in rows:
        code = r.get("code")
        try:
            payload = common.cached_call(
                "daily", {"c": code, "s": start, "e": end},
                lambda c=code: pro.daily(ts_code=c, start_date=start, end_date=end),
                historical=True)
            df = pd.DataFrame(payload.get("rows", []))
            if df.empty:
                continue
            df = df.sort_values("trade_date")
            cl = df["close"].astype(float).tolist()
            r["last"] = round(cl[-1], 2)
            if "pct_chg" in df.columns:
                r["chg"] = round(float(df.iloc[-1]["pct_chg"]), 2)
            if len(cl) >= 6:
                r["ret5"] = round((cl[-1] / cl[-6] - 1) * 100, 2)
        except Exception:
            continue
    return rows


def _build_factor_table(pro, codes: list[str], end: str) -> pd.DataFrame:
    """为候选股构建因子原始值表。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    turnover_map: dict[str, Any] = {}
    basic_map: dict[str, dict[str, Any]] = {}
    try:
        basics = pro.daily_basic(trade_date=end,
                                 fields="ts_code,turnover_rate,pe_ttm,pe,pb,circ_mv,total_mv")
        for r in basics.to_dict(orient="records"):
            turnover_map[r["ts_code"]] = r.get("turnover_rate")
            basic_map[r["ts_code"]] = r
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    for code in codes:
        try:
            df = pro.daily(ts_code=code, start_date=start, end_date=end)
        except Exception:
            continue
        fac = factors.compute_stock_factors(df, turnover_map.get(code), basic_map.get(code))
        if fac is None:
            continue
        fac["code"] = code
        rows.append(fac)
    return pd.DataFrame(rows)


def run(industries: Optional[list[str]] = None,
        weights: Optional[dict[str, float]] = None,
        top_n: int = 30) -> dict[str, Any]:
    """量化选股主入口。industries 指定则限定范围，否则全市场（剔除 ST）。

    优先读预计算 daily_factors（全市场路径可完全不依赖 tushare）；未命中回退实时逐只。
    """
    w = factor_config.effective_weights("stock")
    if weights:
        w.update(weights)

    # 解析交易日：优先 tushare 最近交易日；不可用则用 daily_factors 最新日期
    try:
        end = common.last_trade_date()
    except Exception:
        end = db.latest_factor_date()
    if not end:
        return {"source": "screen/quant", "fetched_at": common.now_str(),
                "candidates": [], "note": "无法确定交易日且无预计算数据"}

    # 行业过滤需要成分股映射（用 tushare stock_basic，已缓存）
    code_filter = None
    if industries:
        from screen_trend import _industry_members
        code_filter = set(_industry_members(common.get_pro(), industries))

    # 优先读预计算因子（daily_factors），命中则本地排序、不打 tushare
    data_source = "precomputed"
    tbl = _table_from_db(end, code_filter)
    if tbl is None or tbl.empty:
        data_source = "realtime"
        pro = common.get_pro()
        if industries:
            codes = list(code_filter) if code_filter else []
        else:
            try:
                basic = pro.stock_basic(list_status="L", fields="ts_code,name,market")
                codes = [r["ts_code"] for r in basic.to_dict(orient="records")
                         if "ST" not in str(r["name"]) and "退" not in str(r["name"])]
            except Exception:
                codes = []
        codes = codes[:800]  # 实时兜底：控制 tushare 频率
        tbl = _build_factor_table(pro, codes, end)
    if tbl is None or tbl.empty:
        return {"source": "screen/quant", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "candidates": [], "note": "无有效候选或数据不足（可先跑 precompute_daily_factors）"}

    tbl = factors.composite_score(tbl, w)
    tbl = tbl.sort_values("score", ascending=False)

    # 仅展示权重不为 0 的因子列（0 权重的候选因子不参与打分也不展示）
    active = [f for f in w if float(w.get(f, 0) or 0) != 0.0]
    cols = ["code", "price"] + active + ["score"]
    cols = [c for c in cols if c in tbl.columns]
    out = tbl[cols].head(top_n).round(4).to_dict(orient="records")
    try:
        nm = common.stock_names_map()
        for r in out:
            r["name"] = nm.get(r.get("code"), "")
    except Exception:
        pass
    _attach_quotes(out)
    return {
        "source": "screen/quant",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": end,
        "data_source": data_source,
        "weights": w,
        "factor_note": "默认启用：mom_12_1(趋势)+reversal_1m(短期反转)+low_ivol/low_turnover(情绪)+trend_ma/high_52w(趋势)+vol_confirm(量能)；"
                       "候选因子(默认权重0，可在权重配置启用)：mom_6_1/max_lottery/downside_vol/amihud_illiq/small_size/value_bm/earnings_yield。"
                       "仅权重≠0 的因子参与打分并在结果中展示",
        "candidates": out,
        "note": "量化候选，须由 Agent 叠加 涨价>逻辑>预期>情绪 四维交叉验证复核",
    }


@register("screen_quant", "screening",
          "量化多因子选股（默认：12-1动量+1月反转+低波动+低换手+趋势+量能；另有 7 个默认0权重候选因子"
          "mom_6_1/max_lottery/downside_vol/amihud_illiq/small_size/value_bm/earnings_yield 可在权重配置启用，"
          "仅权重≠0 的因子参与打分与展示）。候选须四维复核",
          params=[{"name": "industries", "type": "array", "required": False,
                   "desc": "限定行业/主线/概念名（单词条多源模糊匹配：stock_basic行业+申万L1/L2/L3+同花顺/东财概念，"
                           "支持'机器人''PCB铜箔''电子布'等细分主题；传多词条取交集层层收窄，"
                           "如['通信','PCB','铜箔']=同时属于三者），省略则全市场"},
                  {"name": "weights", "type": "object", "required": False, "desc": "自定义因子权重"},
                  {"name": "top_n", "type": "int", "required": False, "default": 30}],
          returns="candidates（含各因子值与合成 score）")
def screen_quant(p: dict) -> dict:
    return run(p.get("industries"), p.get("weights"), p.get("top_n", 30))


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    inds = args[0].split(",") if args else None
    print(json.dumps(run(inds), ensure_ascii=False, indent=2))

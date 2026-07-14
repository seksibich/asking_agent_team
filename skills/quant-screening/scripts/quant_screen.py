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


def _build_factor_table(pro, codes: list[str], end: str) -> pd.DataFrame:
    """为候选股构建因子原始值表。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    try:
        basics = pro.daily_basic(trade_date=end, fields="ts_code,turnover_rate")
        turnover_map = {r["ts_code"]: r.get("turnover_rate") for r in basics.to_dict(orient="records")}
    except Exception:
        turnover_map = {}

    rows: list[dict[str, Any]] = []
    for code in codes:
        try:
            df = pro.daily(ts_code=code, start_date=start, end_date=end)
        except Exception:
            continue
        fac = factors.compute_stock_factors(df, turnover_map.get(code))
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

    cols = ["code", "price", "mom_12_1", "reversal_1m", "trend_ma", "high_52w",
            "low_ivol", "low_turnover", "vol_confirm", "score"]
    cols = [c for c in cols if c in tbl.columns]
    out = tbl[cols].head(top_n).round(4).to_dict(orient="records")
    try:
        nm = common.stock_names_map()
        for r in out:
            r["name"] = nm.get(r.get("code"), "")
    except Exception:
        pass
    return {
        "source": "screen/quant",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": end,
        "data_source": data_source,
        "weights": w,
        "factor_note": "个股：mom_12_1(趋势)+reversal_1m(短期反转)+low_ivol/low_turnover(情绪)+trend_ma/high_52w(趋势)",
        "candidates": out,
        "note": "量化候选，须由 Agent 叠加 涨价>逻辑>预期>情绪 四维交叉验证复核",
    }


@register("screen_quant", "screening",
          "量化多因子选股（个股：12-1动量+1月反转+低波动+低换手+趋势）。候选须四维复核",
          params=[{"name": "industries", "type": "array", "required": False,
                   "desc": "限定行业/主线名，省略则全市场"},
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

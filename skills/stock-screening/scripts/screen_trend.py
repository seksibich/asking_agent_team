"""趋势 + 行业逻辑选股。

在指定行业/主线内筛选处于趋势中的个股（均线多头、量价配合、非高位），
并用 factors.py 因子库的趋势侧权重对候选排序，与量化选股口径一致。
排除 ST。候选须 Agent 按四维（涨价>逻辑>预期>情绪）复核。
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


def _industry_members(pro, industries: list[str]) -> list[str]:
    """获取行业成分股代码（按行业名模糊匹配，剔除 ST）。"""
    codes: list[str] = []
    try:
        basic = pro.stock_basic(exchange="", list_status="L",
                                fields="ts_code,name,industry")
    except Exception:
        return codes
    for ind in industries:
        sel = basic[basic["industry"].astype(str).str.contains(ind, na=False)]
        sel = sel[~sel["name"].astype(str).str.contains("ST|退", na=False)]
        codes.extend(sel["ts_code"].tolist())
    return list(dict.fromkeys(codes))


def _passes_trend_filter(fac: dict[str, Any]) -> bool:
    """趋势硬过滤：多头排列（trend_ma>=2 表示 price>ma20>ma60）。"""
    return fac.get("trend_ma", 0) >= 2.0


def run(industries: Optional[list[str]] = None, top_n: int = 30) -> dict[str, Any]:
    """趋势选股主入口。"""
    pro = common.get_pro()
    end = common.last_trade_date()
    if not industries:
        return {
            "source": "screen/trend",
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "未指定行业/主线。建议先用 screen_sector 或 price_hike_scan 定主线再选股",
            "candidates": [],
        }
    ind_codes = _industry_members(pro, industries)
    rows: list[dict[str, Any]] = []
    data_source = "precomputed"

    # 优先读预计算因子（daily_factors）
    db_hit = False
    try:
        db_hit = db.has_daily_factors(end)
    except Exception:
        db_hit = False
    if db_hit:
        fac_map = {r["code"]: r["factors"] for r in db.fetch_daily_factors(end)}
        for code in ind_codes:
            fac = fac_map.get(code)
            if fac and _passes_trend_filter(fac):
                fac = dict(fac)
                fac["code"] = code
                rows.append(fac)

    # 回退实时逐只
    if not rows:
        data_source = "realtime"
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
        try:
            basics = pro.daily_basic(trade_date=end, fields="ts_code,turnover_rate")
            turnover_map = {r["ts_code"]: r.get("turnover_rate") for r in basics.to_dict(orient="records")}
        except Exception:
            turnover_map = {}
        for code in ind_codes[: max(top_n * 5, 120)]:
            try:
                df = pro.daily(ts_code=code, start_date=start, end_date=end)
            except Exception:
                continue
            fac = factors.compute_stock_factors(df, turnover_map.get(code))
            if fac is None or not _passes_trend_filter(fac):
                continue
            fac["code"] = code
            rows.append(fac)

    if not rows:
        return {"source": "screen/trend", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "industries": industries, "candidates": [], "note": "无满足趋势过滤的候选"}

    tbl = factors.composite_score(pd.DataFrame(rows), factor_config.effective_weights("trend"))
    tbl = tbl.sort_values("score", ascending=False)
    cols = ["code", "price", "mom_12_1", "trend_ma", "high_52w", "vol_confirm", "score"]
    cols = [c for c in cols if c in tbl.columns]
    return {
        "source": "screen/trend",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industries": industries,
        "trade_date": end,
        "data_source": data_source,
        "candidates": tbl[cols].head(top_n).round(4).to_dict(orient="records"),
        "note": "趋势初筛候选，需 Agent 按 涨价>逻辑>预期>情绪 四维复核",
    }


@register("screen_trend", "screening",
          "趋势+行业逻辑选股（行业内多头排列过滤 + 趋势因子排序）",
          params=[{"name": "industries", "type": "array", "required": True,
                   "desc": "行业/主线名列表（先定主线再选股）"},
                  {"name": "top_n", "type": "int", "required": False, "default": 30}],
          returns="candidates（趋势因子排序）")
def screen_trend(p: dict) -> dict:
    return run(p.get("industries"), p.get("top_n", 30))


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    inds = args[0].split(",") if args else None
    print(json.dumps(run(inds), ensure_ascii=False, indent=2))

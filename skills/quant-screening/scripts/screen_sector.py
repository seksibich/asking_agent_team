"""板块选择 / 板块轮动量化（趋势有效因子）。

依据回测经验：A股行业/板块层面动量为正（轮动具延续性），故用
板块 12-1 / 20 日 / 5 日动量 + 量能确认 + 低波动 合成打分排名。

数据源优先申万一级行业指数（sw_daily），无权限时回退到 tushare 概念/板块指数。
可选：对排名靠前的板块，进一步在成分股内做个股量化选股（trend/quant）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import factors
import factor_config
from registry import register


def _sw_l1_industries(pro) -> list[dict[str, str]]:
    """获取申万一级行业指数清单。"""
    try:
        df = pro.index_classify(level="L1", src="SW2021")
    except Exception:
        try:
            df = pro.index_classify(level="L1", src="SW")
        except Exception:
            return []
    if df is None or df.empty:
        return []
    code_col = "index_code" if "index_code" in df.columns else "ts_code"
    name_col = "industry_name" if "industry_name" in df.columns else "name"
    return [{"code": r[code_col], "name": r[name_col]} for r in df.to_dict(orient="records")]


def _sector_history(pro, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """获取板块指数日线历史（申万）。"""
    try:
        df = pro.sw_daily(ts_code=code, start_date=start, end_date=end)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
        return df
    except Exception:
        return None


def run(weights: Optional[dict[str, float]] = None, top_n: int = 10,
        with_stocks: bool = False, stocks_per_sector: int = 5) -> dict[str, Any]:
    """板块轮动选择主入口。"""
    pro = common.get_pro()
    end = common.last_trade_date()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    w = factor_config.effective_weights("sector")
    if weights:
        w.update(weights)

    industries = _sw_l1_industries(pro)
    if not industries:
        return {"source": "screen/sector", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sectors": [], "note": "无法获取行业指数（检查 index_classify/sw_daily 权限）"}

    rows: list[dict[str, Any]] = []
    for ind in industries:
        hist = _sector_history(pro, ind["code"], start, end)
        fac = factors.compute_sector_factors(hist) if hist is not None else None
        if fac is None:
            continue
        fac["code"] = ind["code"]
        fac["name"] = ind["name"]
        rows.append(fac)

    if not rows:
        return {"source": "screen/sector", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sectors": [], "note": "行业历史数据不足"}

    tbl = factors.composite_score(pd.DataFrame(rows), w)
    tbl = tbl.sort_values("score", ascending=False)
    cols = ["code", "name", "sec_mom_12_1", "sec_mom_20d", "sec_mom_5d",
            "sec_vol_confirm", "sec_low_vol", "score"]
    cols = [c for c in cols if c in tbl.columns]
    top_sectors = tbl[cols].head(top_n).round(4).to_dict(orient="records")

    result: dict[str, Any] = {
        "source": "screen/sector",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": end,
        "weights": w,
        "factor_note": "板块层面动量为正：sec_mom_12_1/20d/5d + 量能确认 + 低波动",
        "sectors": top_sectors,
        "note": "板块轮动候选，须由 Agent 叠加涨价/景气逻辑交叉验证",
    }

    # 可选：在强势板块内选个股
    if with_stocks:
        import quant_screen
        picks = {}
        for sec in top_sectors[:3]:
            sub = quant_screen.run([sec["name"]], top_n=stocks_per_sector)
            picks[sec["name"]] = sub.get("candidates", [])
        result["stock_picks_by_sector"] = picks

    return result


@register("screen_sector", "screening",
          "选板块/板块轮动量化（板块动量为正：12-1/20日/5日动量+量能+低波动）",
          params=[{"name": "weights", "type": "object", "required": False},
                  {"name": "top_n", "type": "int", "required": False, "default": 10},
                  {"name": "with_stocks", "type": "bool", "required": False, "default": False,
                   "desc": "true 则在前3板块内选个股"},
                  {"name": "stocks_per_sector", "type": "int", "required": False, "default": 5}],
          returns="sectors 排名（+可选 stock_picks_by_sector）")
def screen_sector(p: dict) -> dict:
    return run(p.get("weights"), p.get("top_n", 10),
               p.get("with_stocks", False), p.get("stocks_per_sector", 5))


if __name__ == "__main__":
    import json
    print(json.dumps(run(with_stocks=False), ensure_ascii=False, indent=2))

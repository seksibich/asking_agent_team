"""全市场个股因子预计算。

把选股取数从"逐只 daily"改为"按交易日切片"：拉最近 lookback 个交易日的全市场
daily 切片（永久缓存、跨日复用），透视成每只个股的日线序列，对每只跑
compute_stock_factors，把某交易日 D 的全市场因子写入 daily_factors 表。

- 增量（默认）：为最近交易日 D 计算并落库（日常盘后跑一次）。
- 全量 full=True：为窗口内多个交易日补算（首次部署/断档补数）。

选股（screen_quant/screen_trend）优先读 daily_factors，未命中回退实时逐只。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import db
import factors
from registry import register

BENCH_INDEX = "000300.SH"
LOOKBACK_DEFAULT = 260


def _trade_dates(pro, end: str, n: int) -> list[str]:
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=n * 2 + 30)).strftime("%Y%m%d")
    df = pro.index_daily(ts_code=BENCH_INDEX, start_date=start, end_date=end)
    if df is None or df.empty:
        return []
    return sorted(df["trade_date"].astype(str).tolist())[-n:]


def _daily_slice(pro, date: str) -> pd.DataFrame:
    """全市场某日 daily 切片（永久缓存复用）。"""
    payload = common.cached_call("daily_slice", {"trade_date": date},
                                 lambda: pro.daily(trade_date=date), historical=True)
    return pd.DataFrame(payload.get("rows", []))


def _turnover_map(pro, date: str) -> dict[str, float]:
    try:
        payload = common.cached_call("daily_basic_slice", {"trade_date": date},
                                     lambda: pro.daily_basic(trade_date=date,
                                                             fields="ts_code,turnover_rate"),
                                     historical=True)
        return {r["ts_code"]: r.get("turnover_rate") for r in payload.get("rows", [])}
    except Exception:
        return {}


def _basic_map(pro, date: str) -> dict[str, dict[str, Any]]:
    """全市场某日估值/市值切片（供规模/价值/盈利收益率等候选因子，永久缓存复用）。"""
    try:
        payload = common.cached_call(
            "daily_basic_val_slice", {"trade_date": date},
            lambda: pro.daily_basic(trade_date=date,
                                    fields="ts_code,pe_ttm,pe,pb,circ_mv,total_mv"),
            historical=True)
        return {r["ts_code"]: r for r in payload.get("rows", [])}
    except Exception:
        return {}


def _compute_for_date(pro, target: str, lookback: int) -> int:
    """为交易日 target 计算全市场因子并落库，返回写入股票数。"""
    dates = _trade_dates(pro, target, lookback)
    if not dates or dates[-1] != target:
        # target 不在交易日序列（非交易日）则跳过
        if not dates:
            return 0
    frames = []
    for d in dates:
        sl = _daily_slice(pro, d)
        if not sl.empty:
            keep = [c for c in ("ts_code", "trade_date", "close", "vol", "amount") if c in sl.columns]
            frames.append(sl[keep])
    if not frames:
        return 0
    allbars = pd.concat(frames, ignore_index=True)
    turnover = _turnover_map(pro, target)
    basic = _basic_map(pro, target)

    bar_cols = [c for c in ("trade_date", "close", "vol", "amount") if c in allbars.columns]
    items: list[dict[str, Any]] = []
    for code, g in allbars.groupby("ts_code"):
        fac = factors.compute_stock_factors(g[bar_cols], turnover.get(code), basic.get(code))
        if fac is not None:
            items.append({"code": code, "factors": fac})
    if items:
        db.bulk_upsert_daily_factors(target, items)
    return len(items)


@register("precompute_daily_factors", "screening",
          "全市场个股因子预计算并落库 daily_factors（选股读库提速、省 tushare）。"
          "默认为最近交易日增量；full=true 为窗口内多日补算",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "lookback", "type": "int", "required": False, "default": LOOKBACK_DEFAULT,
                   "desc": "因子计算回看交易日数（≥252 保证 12-1 动量）"},
                  {"name": "full", "type": "bool", "required": False, "default": False,
                   "desc": "true=为最近 lookback 个交易日逐日补算（首次/断档）"}],
          returns="dates_computed / stocks 每日写入数")
def precompute_daily_factors(p: dict) -> dict:
    pro = common.get_pro()
    end = p.get("date") or common.last_trade_date()
    lookback = int(p.get("lookback", LOOKBACK_DEFAULT))

    results: dict[str, int] = {}
    if p.get("full"):
        targets = _trade_dates(pro, end, lookback)
        # 只为有足够历史（>=60日）的靠后交易日补算，避免前段窗口不足
        for i, d in enumerate(targets):
            if i < 60:
                continue
            results[d] = _compute_for_date(pro, d, lookback)
    else:
        results[end] = _compute_for_date(pro, end, lookback)

    return {
        "source": "precompute_daily_factors",
        "fetched_at": common.now_str(),
        "lookback": lookback,
        "dates_computed": list(results.keys()),
        "stocks_per_date": results,
        "note": "已写入 daily_factors；选股 screen_quant/screen_trend 将优先读库",
    }


@register("precompute_status", "screening",
          "预计算覆盖状态：daily_factors 最近交易日的覆盖股票数、最新日期、总记录数",
          params=[{"name": "limit", "type": "int", "required": False, "default": 30,
                   "desc": "返回最近多少个交易日"}],
          returns="latest_date / coverage[{trade_date,count}]")
def precompute_status(p: dict) -> dict:
    limit = int(p.get("limit", 30))
    coverage = db.factor_date_counts(limit)
    return {
        "source": "precompute_status",
        "fetched_at": common.now_str(),
        "latest_date": db.latest_factor_date(),
        "coverage": coverage,
        "note": "daily_factors 覆盖情况；如最新交易日缺失，请跑 precompute_daily_factors",
    }


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(precompute_daily_factors({}), ensure_ascii=False, indent=2))

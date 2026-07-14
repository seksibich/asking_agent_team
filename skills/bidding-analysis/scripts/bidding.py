"""竞价分析数据（09:25 集合竞价结束后）。

聚合集合竞价数据并结合昨日收盘/成交额，为竞价分析 skill 提供：
- 指定标的（昨日选股 + 用户关注 + 持仓 + 高热度）的竞价表现
- 全市场竞价后成交额最高的 Top N（默认 20）
- 异常高开、竞价爆量 标记

字段：高开幅度(gap_pct)、竞价成交额(auction_amount)、竞价量(auction_vol)、
昨收(pre_close)、昨日成交额(prev_amount)、竞价额/昨额(amt_ratio)。

超预期能力/抄底可能等定性判断由 bidding-analysis skill 结合情绪(market_timing/
sentiment_temperature)与舆情完成，本脚本只备数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
from registry import register

GAP_ABNORMAL = 3.0        # 异常高开阈值（%）
BURST_RATIO = 0.03        # 竞价爆量阈值：竞价额/昨日全天成交额


def _prev_trade_date(pro, today: str) -> Optional[str]:
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=20)).strftime("%Y%m%d")
    try:
        df = pro.index_daily(ts_code="000001.SH", start_date=start, end_date=today)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    dates = sorted(df["trade_date"].astype(str).tolist())
    dates = [d for d in dates if d < today] or dates
    return dates[-1] if dates else None


def _pick(row: dict, keys: list[str]) -> Optional[float]:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def _analyze(pro, today: str) -> tuple[dict[str, dict], str]:
    """返回 {code: 竞价指标} 与 备注。"""
    prev_td = _prev_trade_date(pro, today)
    prev_map: dict[str, dict] = {}
    if prev_td:
        try:
            pv = pro.daily(trade_date=prev_td)
            for r in pv.to_dict(orient="records"):
                prev_map[r["ts_code"]] = {"pre_close": float(r.get("close") or 0),
                                          "prev_amount": float(r.get("amount") or 0)}
        except Exception:
            pass
    try:
        auc = pro.stk_auction_o(trade_date=today)
    except Exception as e:
        return {}, f"竞价数据获取失败（stk_auction_o）：{e}"
    if auc is None or auc.empty:
        return {}, "竞价数据为空（可能非交易日或未到 09:25）"

    out: dict[str, dict] = {}
    for r in auc.to_dict(orient="records"):
        code = r.get("ts_code")
        if not code:
            continue
        price = _pick(r, ["open", "close", "price", "cur_price"])
        amount = _pick(r, ["amount", "auc_amount"])
        vol = _pick(r, ["vol", "volume", "auc_vol"])
        pm = prev_map.get(code, {})
        pc = pm.get("pre_close") or _pick(r, ["pre_close"])
        pa = pm.get("prev_amount")
        gap = round((price - pc) / pc * 100, 2) if (price and pc) else None
        amt_ratio = round(amount / pa, 4) if (amount and pa) else None
        out[code] = {
            "code": code,
            "name": r.get("name") or r.get("ts_name") or "",
            "auction_price": price,
            "pre_close": pc,
            "gap_pct": gap,
            "auction_amount": amount,
            "auction_vol": vol,
            "prev_amount": pa,
            "amt_ratio": amt_ratio,
            "abnormal_gap": bool(gap is not None and gap >= GAP_ABNORMAL),
            "burst_volume": bool(amt_ratio is not None and amt_ratio >= BURST_RATIO),
        }
    return out, "ok"


@register("bidding_analysis", "sentiment",
          "竞价分析数据（09:25）：指定标的竞价表现 + 全市场竞价成交额 TopN + 异常高开/竞价爆量标记。"
          "结合昨收与昨日成交额。定性判断（超预期/抄底）由 skill 结合情绪与舆情完成",
          params=[{"name": "codes", "type": "array", "required": False, "default": [],
                   "desc": "关注标的（昨日选股+关注+持仓+高热度）"},
                  {"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认今日"},
                  {"name": "top_n", "type": "int", "required": False, "default": 20,
                   "desc": "全市场竞价成交额 TopN"}],
          returns="targets / market_top / abnormal_gap / burst_volume")
def bidding_analysis(p: dict) -> dict:
    pro = common.get_pro()
    today = p.get("date") or common.today_str()
    codes = p.get("codes", []) or []
    top_n = int(p.get("top_n", 20))

    data, note = _analyze(pro, today)
    if not data:
        return {"source": "bidding_analysis", "fetched_at": common.now_str(),
                "date": today, "targets": [], "market_top": [], "note": note}

    targets = [data[c] for c in codes if c in data]
    ranked = sorted([v for v in data.values() if v.get("auction_amount")],
                    key=lambda x: x["auction_amount"], reverse=True)
    market_top = ranked[:top_n]

    abnormal = [v for v in data.values() if v["abnormal_gap"]]
    burst = [v for v in data.values() if v["burst_volume"]]

    return {
        "source": "bidding_analysis",
        "fetched_at": common.now_str(),
        "date": today,
        "targets": targets,
        "market_top": market_top,
        "abnormal_gap_count": len(abnormal),
        "burst_volume_count": len(burst),
        "abnormal_gap_top": sorted(abnormal, key=lambda x: (x["gap_pct"] or 0), reverse=True)[:top_n],
        "burst_volume_top": sorted(burst, key=lambda x: (x["amt_ratio"] or 0), reverse=True)[:top_n],
        "note": ("竞价数据备查；超预期表现能力与抄底可能由 skill 结合 market_timing/"
                 "sentiment_temperature 与舆情/行业催化综合判断"),
    }


if __name__ == "__main__":
    print(json.dumps(bidding_analysis({}), ensure_ascii=False, indent=2))

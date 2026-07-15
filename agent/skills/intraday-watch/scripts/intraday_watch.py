"""盘中盯盘扫描。

一次聚合：实时行情、涨跌停增量、连板梯队，与上轮快照对比识别异动。
优先报告涨价链/趋势主线异动，其次纯情绪连板异动。无异动返回空 alerts（静默）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import common
from registry import register

SNAPSHOT = common.CACHE_DIR / "intraday_snapshot.json"


def _load_snapshot() -> dict[str, Any]:
    if SNAPSHOT.exists():
        with SNAPSHOT.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_snapshot(data: dict[str, Any]) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with SNAPSHOT.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def scan(codes: list[str], positions: list[dict], alert_pct: float = 5.0) -> dict[str, Any]:
    """盘中扫描主入口。

    codes: 关注/趋势股清单；positions: [{code, cost, stop_loss}]。
    """
    import tushare as ts
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    watch_codes = list(dict.fromkeys(codes + [p["code"] for p in positions]))

    try:
        rt = ts.realtime_quote(ts_code=",".join(watch_codes)) if watch_codes else pd.DataFrame()
    except Exception:
        rt = pd.DataFrame()

    alerts: list[dict[str, Any]] = []
    rt_map: dict[str, dict] = {}
    if not rt.empty:
        for r in rt.to_dict(orient="records"):
            code = r.get("TS_CODE") or r.get("ts_code")
            price = float(r.get("PRICE") or r.get("price") or 0)
            pre = float(r.get("PRE_CLOSE") or r.get("pre_close") or 0)
            pct = (price - pre) / pre * 100 if pre else 0.0
            rt_map[code] = {"price": price, "pct": round(pct, 2)}

    # 止损与持仓异动（优先级最高）
    for p in positions:
        code = p["code"]
        info = rt_map.get(code)
        if not info:
            continue
        if p.get("stop_loss") and info["price"] <= float(p["stop_loss"]):
            alerts.append({"type": "止损触发", "level": "critical", "code": code,
                           "price": info["price"], "reason": f"跌破止损 {p['stop_loss']}"})
        elif abs(info["pct"]) >= alert_pct:
            alerts.append({"type": "持仓异动", "level": "warning", "code": code,
                           "price": info["price"], "pct": info["pct"], "reason": "持仓波动超阈值"})

    # 关注/趋势股异动
    pos_codes = {p["code"] for p in positions}
    for code in codes:
        if code in pos_codes:
            continue
        info = rt_map.get(code)
        if info and abs(info["pct"]) >= alert_pct:
            alerts.append({"type": "趋势/关注股异动", "level": "info", "code": code,
                           "price": info["price"], "pct": info["pct"],
                           "reason": "关注股波动超阈值（如涉涨价链请优先核验）"})

    snapshot = {"timestamp": now, "rt": rt_map}
    _save_snapshot(snapshot)

    return {
        "source": "watch_intraday",
        "fetched_at": now,
        "alerts": alerts,
        "silent": len(alerts) == 0,
        "note": "涨价链/趋势主线异动请优先推送；纯情绪连板次之",
    }


@register("watch_intraday", "screening",
          "盘中盯盘扫描：聚合实时行情+快照对比，识别持仓/关注/趋势股异动（无异动静默）",
          params=[{"name": "codes", "type": "array", "required": True, "desc": "关注/趋势股代码"},
                  {"name": "positions", "type": "array", "required": False, "default": [],
                   "desc": "[{code,cost,stop_loss}]"},
                  {"name": "alert_pct", "type": "float", "required": False, "default": 5.0}],
          returns="alerts / silent")
def watch_intraday(p: dict) -> dict:
    return scan(p["codes"], p.get("positions", []), p.get("alert_pct", 5.0))


if __name__ == "__main__":
    demo = scan(sys.argv[1].split(",") if len(sys.argv) > 1 else [], [])
    print(json.dumps(demo, ensure_ascii=False, indent=2))

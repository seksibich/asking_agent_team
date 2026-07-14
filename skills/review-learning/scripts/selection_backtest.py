"""选股回测工具（DB 持久化 + 成熟样本固化）。

- log_selection：登记标的到 DB（selections 表），按 (日期,代码,category) **幂等去重**。
  category=auto(自动选股,用于调参) / watch(用户关注) / holding(用户持仓)。
  用户临时指定方向的选股不登记。
- selection_backtest：统计选出后 1/3/7/30 交易日涨幅、胜率、相对沪深300超额，
  分 category、auto 再分 driver/分数分桶，产出调参建议。
  **成熟样本（已满该持有期）的前向收益写入 selection_forward_returns 缓存并固化，
  下次不再重复回算**，只增量计算未成熟样本，大幅减少 tushare 调用。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

import common
import db
from registry import register

HORIZONS = [1, 3, 7, 30]
BENCHMARK = "000300.SH"
VALID_CATEGORIES = {"auto", "watch", "holding"}


def _to_date(s: str):
    s = str(s).replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


# ---------------- 登记 ----------------
@register("log_selection", "review",
          "登记一只标的用于盯盘观察与回测（DB 持久化，按 日期+代码+category 幂等去重）。"
          "category=auto(自动选股,调参)/watch(关注)/holding(持仓)。用户临时指定方向的选股不登记",
          params=[{"name": "code", "type": "string", "required": True, "desc": "tushare 代码"},
                  {"name": "name", "type": "string", "required": False, "default": ""},
                  {"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认今日"},
                  {"name": "score", "type": "float", "required": False, "default": 0.0},
                  {"name": "driver", "type": "string", "required": False, "default": "未标注",
                   "desc": "主导驱动：涨价/逻辑/预期/情绪"},
                  {"name": "reason", "type": "string", "required": False, "default": ""},
                  {"name": "category", "type": "string", "required": False, "default": "auto",
                   "desc": "auto|watch|holding"},
                  {"name": "extra", "type": "object", "required": False}],
          returns="登记结果")
def log_selection(p: dict) -> dict:
    cat = p.get("category", "auto")
    if cat not in VALID_CATEGORIES:
        return {"logged": False, "reason": f"category 须为 {sorted(VALID_CATEGORIES)}；用户临时指定方向的选股不登记"}
    rec = {
        "sel_date": _to_date(p.get("date") or common.today_str()),
        "code": p["code"],
        "name": p.get("name", ""),
        "score": float(p.get("score", 0.0) or 0.0),
        "driver": p.get("driver", "未标注"),
        "reason": p.get("reason", ""),
        "category": cat,
        "extra": p.get("extra"),
        "logged_at": datetime.now(),
    }
    db.upsert_selection(rec)
    return {"logged": True, "record": {**rec, "sel_date": str(rec["sel_date"]), "logged_at": common.now_str()}}


# ---------------- 前向收益 ----------------
def _forward_returns(pro, code: str, sel_day: str) -> tuple[dict[int, float], dict[int, float]]:
    """返回 (个股前向收益 %, 基准前向收益 %)，仅含已实现（已满）的持有期。"""
    end = (datetime.strptime(sel_day, "%Y%m%d") + timedelta(days=70)).strftime("%Y%m%d")
    try:
        import tushare as ts
        df = ts.pro_bar(ts_code=code, adj="qfq", start_date=sel_day, end_date=end)
    except Exception:
        try:
            df = pro.daily(ts_code=code, start_date=sel_day, end_date=end)
        except Exception:
            df = None
    stock: dict[int, float] = {}
    if df is not None and not df.empty:
        df = df.sort_values("trade_date").reset_index(drop=True)
        entry = float(df.iloc[0]["close"]) if len(df) else 0.0
        if entry:
            for h in HORIZONS:
                if len(df) > h:
                    stock[h] = round((float(df.iloc[h]["close"]) - entry) / entry * 100, 2)
    bench: dict[int, float] = {}
    try:
        bd = pro.index_daily(ts_code=BENCHMARK, start_date=sel_day, end_date=end)
        if bd is not None and not bd.empty:
            bd = bd.sort_values("trade_date").reset_index(drop=True)
            be = float(bd.iloc[0]["close"])
            for h in HORIZONS:
                if len(bd) > h and be:
                    bench[h] = round((float(bd.iloc[h]["close"]) - be) / be * 100, 2)
    except Exception:
        pass
    return stock, bench


def _tuning_hints(by_driver: dict[str, dict[int, list[float]]]) -> list[str]:
    hints: list[str] = []
    h = 30
    avg = {d: sum(v[h]) / len(v[h]) for d, v in by_driver.items() if v.get(h)}
    if not avg:
        return ["样本不足，暂无法给出调参建议（需更多已满 30 交易日的自动选股样本）"]
    best = max(avg, key=avg.get)
    worst = min(avg, key=avg.get)
    hints.append(f"30日超额最优驱动：{best}（+{avg[best]:.2f}pct），可维持/提高其在选股中的权重")
    if avg[worst] < 0:
        hints.append(f"30日超额为负驱动：{worst}（{avg[worst]:.2f}pct），建议降低权重或提高入选门槛（尤其情绪类）")
    return hints


@register("selection_backtest", "review",
          "自动选股回测：选出后 1/3/7/30 交易日收益/胜率/相对沪深300超额，分 category 与 driver/分数桶，"
          "给出调参建议。成熟样本前向收益固化到 DB，增量计算，省 tushare 调用",
          params=[{"name": "save_snapshot", "type": "bool", "required": False, "default": False,
                   "desc": "是否把本次聚合结果留存到 backtest_snapshots"}],
          returns="by_category / auto_by_driver / tuning_hints / details")
def selection_backtest(p: dict) -> dict:
    pro = common.get_pro()
    sels = db.fetch_selections()

    cat_returns: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    cat_excess: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_driver: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_bucket: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    details: list[dict[str, Any]] = []
    computed_calls = 0

    for s in sels:
        sid = s["id"]
        sel_day = str(s["sel_date"]).replace("-", "")
        cat = s.get("category", "auto")
        driver = s.get("driver", "未标注")
        score = float(s.get("score", 0.0) or 0.0)
        bucket = "high(>=0.75)" if score >= 0.75 else ("mid(0.55-0.75)" if score >= 0.55 else "low(<0.55)")

        cached = db.get_cached_returns(sid)  # {h:{ret_pct,excess_pct,matured}}
        need = [h for h in HORIZONS if not (h in cached and cached[h].get("matured"))]
        if need:
            stock, bench = _forward_returns(pro, s["code"], sel_day)
            computed_calls += 1
            for h in need:
                if h in stock:  # 已实现 → 固化
                    exc = round(stock[h] - bench[h], 2) if h in bench else None
                    db.save_return(sid, h, stock[h], exc, matured=True)
                    cached[h] = {"ret_pct": stock[h], "excess_pct": exc, "matured": 1}
                # 未实现的持有期本次不缓存，下次再算

        rec_ret = {}
        for h in HORIZONS:
            c = cached.get(h)
            if not c or not c.get("matured"):
                continue
            ret = float(c["ret_pct"]) if c["ret_pct"] is not None else None
            exc = float(c["excess_pct"]) if c["excess_pct"] is not None else None
            if ret is None:
                continue
            cat_returns[cat][h].append(ret)
            rec_ret[h] = ret
            if exc is not None:
                cat_excess[cat][h].append(exc)
            if cat == "auto":
                auto_by_driver[driver][h].append(exc if exc is not None else ret)
                auto_by_bucket[bucket][h].append(ret)
        details.append({"date": str(s["sel_date"]), "code": s["code"], "name": s.get("name", ""),
                        "category": cat, "driver": driver, "score": score, "returns_pct": rec_ret})

    def _summ(d: dict[int, list[float]]) -> dict[str, Any]:
        out = {}
        for h in HORIZONS:
            vals = d.get(h, [])
            if vals:
                out[f"{h}d"] = {"n": len(vals), "avg_pct": round(sum(vals) / len(vals), 2),
                                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)}
        return out

    result = {
        "source": "selection_backtest",
        "fetched_at": common.now_str(),
        "total_selections": len(sels),
        "recomputed_samples": computed_calls,
        "by_category_return": {c: _summ(hz) for c, hz in cat_returns.items()},
        "by_category_excess": {c: _summ(hz) for c, hz in cat_excess.items()},
        "auto_by_driver_excess": {d: _summ(hz) for d, hz in auto_by_driver.items()},
        "auto_by_bucket_return": {b: _summ(hz) for b, hz in auto_by_bucket.items()},
        "tuning_hints": _tuning_hints(auto_by_driver),
        "details": details[-100:],
        "note": ("成熟样本前向收益已固化到 DB，仅增量计算；仅 auto 用于调参，watch/holding 仅观察"),
    }
    if p.get("save_snapshot"):
        db.save_snapshot("selection", {k: v for k, v in result.items() if k != "details"})
    return result


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(selection_backtest({}), ensure_ascii=False, indent=2))

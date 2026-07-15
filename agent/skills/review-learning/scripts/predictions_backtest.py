"""预判登记与回测（DB 持久化）。

- log_prediction：登记方向性预判到 DB（predictions 表），按 (日期,标的,方向) 幂等去重。
- predictions_backtest：对比预判方向与实际涨跌，计算总准确率与分驱动准确率。
  优先读 DB；DB 为空时回退读旧的 DATA_DIR/predictions.jsonl（向后兼容）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import common
import db
from registry import register


def _to_date(s: str):
    s = str(s).replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


@register("log_prediction", "review",
          "登记一条方向性预判到 DB（按 日期+标的+方向 幂等去重），供回测计算准确率",
          params=[{"name": "target", "type": "string", "required": True, "desc": "tushare 代码"},
                  {"name": "direction", "type": "string", "required": True, "desc": "up|down"},
                  {"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认今日"},
                  {"name": "driver", "type": "string", "required": False, "default": "未标注"},
                  {"name": "reason", "type": "string", "required": False, "default": ""},
                  {"name": "extra", "type": "object", "required": False}],
          returns="登记结果")
def log_prediction(p: dict) -> dict:
    if p["direction"] not in ("up", "down"):
        return {"logged": False, "reason": "direction 须为 up 或 down"}
    rec = {
        "pred_date": _to_date(p.get("date") or common.today_str()),
        "target": p["target"],
        "direction": p["direction"],
        "driver": p.get("driver", "未标注"),
        "reason": p.get("reason", ""),
        "extra": p.get("extra"),
    }
    db.upsert_prediction(rec)
    return {"logged": True, "record": {**rec, "pred_date": str(rec["pred_date"])}}


def _legacy_jsonl(day_dash: str) -> list[dict[str, Any]]:
    path = Path(common.DATA_DIR) / "predictions.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("date") == day_dash and o.get("direction") in ("up", "down"):
                out.append({"target": o.get("target"), "direction": o["direction"],
                            "driver": o.get("driver", "未标注")})
    return out


def _actual_direction(pro, target: str, trade_date: str) -> Optional[str]:
    if not target or "." not in target:
        return None
    try:
        d = pro.daily(ts_code=target, trade_date=trade_date)
    except Exception:
        return None
    if d is None or d.empty:
        return None
    pct = float(d.iloc[0]["pct_chg"])
    return "up" if pct > 0 else ("down" if pct < 0 else "neutral")


@register("predictions_backtest", "review",
          "预判回测：对比预判方向与实际涨跌，计算总准确率与分驱动准确率（DB 优先，兼容旧 jsonl）",
          params=[{"name": "day", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"}],
          returns="accuracy_pct / accuracy_by_driver / details")
def predictions_backtest(p: dict) -> dict:
    pro = common.get_pro()
    day = (p.get("day") or common.last_trade_date())
    day = str(day).replace("-", "")
    day_dash = f"{day[:4]}-{day[4:6]}-{day[6:]}"

    items = db.fetch_predictions(_to_date(day))
    items = [{"target": r["target"], "direction": r["direction"], "driver": r.get("driver", "未标注")}
             for r in items if r.get("direction") in ("up", "down")]
    if not items:
        items = _legacy_jsonl(day_dash)

    total, correct = 0, 0
    by_driver: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    details = []
    for it in items:
        actual = _actual_direction(pro, it["target"], day)
        if actual is None:
            continue
        total += 1
        hit = int(actual == it["direction"])
        correct += hit
        drv = it.get("driver", "未标注")
        by_driver[drv][0] += hit
        by_driver[drv][1] += 1
        details.append({"target": it["target"], "predicted": it["direction"],
                        "actual": actual, "hit": bool(hit), "driver": drv})

    driver_acc = {d: round(c / t * 100, 1) if t else None for d, (c, t) in by_driver.items()}
    return {
        "source": "predictions_backtest",
        "fetched_at": common.now_str(),
        "trade_date": day,
        "total": total,
        "correct": correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else None,
        "accuracy_by_driver": driver_acc,
        "details": details,
    }


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(predictions_backtest({}), ensure_ascii=False, indent=2))

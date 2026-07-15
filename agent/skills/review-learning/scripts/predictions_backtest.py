"""预判登记与回测（不可变 DB 持久化 + 下一交易日成熟口径）。

- log_prediction：登记时固化预测时刻和下一目标交易日；同日同标的不允许反向覆盖。
- predictions_backtest：仅在目标交易日成熟后核验，行情失败和 legacy 样本均显式审计。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import common
import db
from registry import register

PREDICTION_CALC_VERSION = "prediction-direction-v2"


def _to_date(value: Any):
    text = str(value).replace("-", "")
    return datetime.strptime(text, "%Y%m%d").date()


def _date_text(value: Any) -> str:
    return value.strftime("%Y%m%d") if hasattr(value, "strftime") else str(value).replace("-", "")[:8]


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo(common.TZ))


def _open_dates(pro, start: str, end: str) -> list[str]:
    calendar = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    if calendar is None or calendar.empty:
        return []
    return sorted(calendar[calendar["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist())


def _next_trade_date(pro, pred_date: str, requested: Any = None) -> str:
    start = (datetime.strptime(pred_date, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    end = (datetime.strptime(pred_date, "%Y%m%d") + timedelta(days=40)).strftime("%Y%m%d")
    dates = _open_dates(pro, start, end)
    if not dates:
        raise RuntimeError("无法从 SSE trade_cal 获取下一交易日")
    if requested is None:
        return dates[0]
    target = _date_text(requested)
    if target not in dates:
        raise ValueError("target_trade_date 必须是预判日期之后的 SSE 交易日")
    return target


def _previous_trade_date(pro, anchor: str) -> str:
    start = (datetime.strptime(anchor, "%Y%m%d") - timedelta(days=40)).strftime("%Y%m%d")
    dates = [day for day in _open_dates(pro, start, anchor) if day < anchor]
    if not dates:
        raise RuntimeError("无法从 SSE trade_cal 获取上一交易日")
    return dates[-1]


def _serialize_record(record: dict[str, Any]) -> dict[str, Any]:
    output = dict(record)
    for key in ("pred_date", "target_trade_date", "predicted_at", "created_at"):
        if output.get(key) is not None:
            output[key] = str(output[key])
    return output


@register("log_prediction", "review",
          "登记不可变的下一交易日方向预判，固化预测时刻和目标交易日；同日同标的反向冲突会被拒绝",
          params=[{"name": "target", "type": "string", "required": True, "desc": "tushare 代码"},
                  {"name": "direction", "type": "string", "required": True, "desc": "up|down"},
                  {"name": "date", "type": "string", "required": False, "desc": "预判日期 YYYYMMDD，默认上海当前日期"},
                  {"name": "target_trade_date", "type": "string", "required": False,
                   "desc": "目标 SSE 交易日；默认由 trade_cal 取预判日的下一交易日"},
                  {"name": "driver", "type": "string", "required": False, "default": "未标注"},
                  {"name": "reason", "type": "string", "required": False, "default": ""},
                  {"name": "extra", "type": "object", "required": False}],
          returns="登记结果（含 predicted_at/target_trade_date/calc_version）")
def log_prediction(p: dict) -> dict:
    if p["direction"] not in ("up", "down"):
        return {"logged": False, "reason": "direction 须为 up 或 down"}
    target = str(p["target"]).strip().upper()
    if "." not in target:
        return {"logged": False, "reason": "target 须为完整 tushare 代码"}
    now = _shanghai_now()
    pred_date = _date_text(p.get("date") or now.strftime("%Y%m%d"))
    try:
        _to_date(pred_date)
        target_trade_date = _next_trade_date(
            common.get_pro(), pred_date, p.get("target_trade_date"))
    except Exception as exc:
        return {"logged": False, "reason": f"目标交易日校验失败：{type(exc).__name__}: {exc}"[:400]}
    rec = {
        "pred_date": _to_date(pred_date),
        "target_trade_date": _to_date(target_trade_date),
        "target": target,
        "direction": p["direction"],
        "driver": p.get("driver", "未标注"),
        "reason": p.get("reason", ""),
        "extra": {**dict(p.get("extra") or {}), "horizon_trade_days": 1},
        "predicted_at": now.replace(tzinfo=None),
        "calc_version": PREDICTION_CALC_VERSION,
    }
    write = db.upsert_prediction(rec)
    record = _serialize_record(write["record"])
    if write.get("conflict"):
        return {"logged": False, "conflict": True,
                "reason": "同一预判日期与标的已有不可变的相反方向记录，禁止覆盖",
                "record": record}
    return {"logged": True, "inserted": write.get("inserted", True),
            "immutable": True, "record": record}


def _legacy_jsonl(day_dash: str) -> list[dict[str, Any]]:
    path = Path(common.DATA_DIR) / "predictions.jsonl"
    if not path.exists():
        return []
    output = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if item.get("date") == day_dash and item.get("direction") in ("up", "down"):
                output.append({"id": None, "pred_date": day_dash,
                               "target": item.get("target"), "direction": item["direction"],
                               "driver": item.get("driver", "未标注"),
                               "legacy": True})
    return output


def _actual_direction(pro, target: str, trade_date: str) -> dict[str, Any]:
    if not target or "." not in target:
        return {"status": "failed", "error": "标的不是完整 tushare 代码"}
    try:
        frame = pro.daily(ts_code=target, trade_date=trade_date)
    except Exception as exc:
        return {"status": "failed", "error": f"行情请求失败：{type(exc).__name__}: {exc}"[:300]}
    if frame is None or frame.empty:
        return {"status": "failed", "error": "目标交易日行情为空（可能停牌或数据未就绪）"}
    try:
        pct = float(frame.iloc[0]["pct_chg"])
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "failed", "error": f"行情 pct_chg 无效：{type(exc).__name__}: {exc}"[:300]}
    actual = "up" if pct > 0 else ("down" if pct < 0 else "neutral")
    return {"status": "success", "actual": actual, "pct_chg": pct}


@register("predictions_backtest", "review",
          "预判回测：仅核验已到目标交易日的不可变预判，显式统计未成熟、legacy 与行情失败并默认保存快照",
          params=[{"name": "day", "type": "string", "required": False,
                   "desc": "预判日期 YYYYMMDD；默认最近完成交易日的上一交易日"},
                  {"name": "save_snapshot", "type": "bool", "required": False, "default": True}],
          returns="accuracy_pct / accuracy_by_driver / audit_counts / sample_hash / snapshot_id / details")
def predictions_backtest(p: dict) -> dict:
    pro = common.get_pro()
    latest_completed = _date_text(common.last_completed_trade_date())
    try:
        day = _date_text(p.get("day")) if p.get("day") else _previous_trade_date(pro, latest_completed)
        _to_date(day)
    except Exception as exc:
        return {"source": "predictions_backtest", "fetched_at": common.now_str(),
                "calc_version": PREDICTION_CALC_VERSION,
                "error": f"回测日期确定失败：{type(exc).__name__}: {exc}"[:400]}
    day_dash = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    items = db.fetch_predictions(_to_date(day))
    source = "db"
    if not items:
        items = _legacy_jsonl(day_dash)
        source = "legacy_jsonl" if items else "db"

    correct = 0
    evaluated = 0
    counts = {"registered": len(items), "evaluated": 0, "correct": 0,
              "not_matured": 0, "failed": 0, "legacy_unverifiable": 0}
    by_driver: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    details: list[dict[str, Any]] = []
    sample_identity: list[dict[str, Any]] = []
    for item in items:
        detail = {"id": item.get("id"), "prediction_date": day,
                  "target": item.get("target"), "predicted": item.get("direction"),
                  "driver": item.get("driver", "未标注")}
        target_date = item.get("target_trade_date")
        if item.get("legacy") or not target_date:
            counts["legacy_unverifiable"] += 1
            detail.update({"status": "legacy_unverifiable", "error": "旧记录没有目标交易日，禁止按同日涨跌回填"})
            details.append(detail)
            continue
        target_text = _date_text(target_date)
        detail["target_trade_date"] = target_text
        sample_identity.append({"id": item.get("id"), "target_trade_date": target_text,
                                "direction": item.get("direction")})
        if target_text > latest_completed:
            counts["not_matured"] += 1
            detail.update({"status": "not_matured", "error": "目标交易日尚未完成"})
            details.append(detail)
            continue
        actual = _actual_direction(pro, str(item.get("target") or ""), target_text)
        if actual["status"] != "success":
            counts["failed"] += 1
            detail.update(actual)
            details.append(detail)
            continue
        hit = int(actual["actual"] == item.get("direction"))
        evaluated += 1
        correct += hit
        counts["evaluated"] += 1
        counts["correct"] += hit
        driver = item.get("driver", "未标注")
        by_driver[driver][0] += hit
        by_driver[driver][1] += 1
        detail.update(actual)
        detail.update({"hit": bool(hit)})
        details.append(detail)

    driver_acc = {driver: round(hit / total * 100, 1) if total else None
                  for driver, (hit, total) in by_driver.items()}
    sample_hash = hashlib.sha256(json.dumps(
        sample_identity, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    result = {
        "source": "predictions_backtest", "record_source": source,
        "fetched_at": common.now_str(), "prediction_date": day,
        "latest_completed_trade_date": latest_completed,
        "calc_version": PREDICTION_CALC_VERSION,
        "total": evaluated, "correct": correct,
        "accuracy_pct": round(correct / evaluated * 100, 1) if evaluated else None,
        "accuracy_by_driver": driver_acc,
        "audit_counts": counts,
        "sample_hash": sample_hash,
        "details": details,
        "note": "准确率分母只含目标交易日已成熟且行情核验成功的样本；未成熟、legacy 与失败样本均保留审计，禁止静默缩小样本。",
    }
    if p.get("save_snapshot", True):
        result["snapshot_id"] = db.save_snapshot(
            "predictions", {key: value for key, value in result.items() if key != "details"}
            | {"sample_identity": sample_identity, "audit_details": details})
    else:
        result["snapshot_id"] = None
    return result

if __name__ == "__main__":
    db.init_db()
    print(json.dumps(predictions_backtest({}), ensure_ascii=False, indent=2))

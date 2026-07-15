"""选股回测工具（DB 持久化 + 成熟样本固化）。

- log_selection：登记标的到 DB（selections 表），按 (日期,代码,category) **幂等去重**。
  category=auto(自动选股,用于调参) / manual(用户触发正式选股) /
  watch(用户关注) / holding(用户持仓)。所有正式选股保存选股价、热点/事件、短线地位和量化因子快照。
- selection_dashboard：按日期/热点/类别查询选股，并补最近交易日行情与选股后涨跌。
- selection_backtest：统计选出后 1/3/7/30 交易日涨幅、胜率、相对沪深300超额，
  分 category、auto 再分 driver/分数分桶，产出调参建议。
  **成熟样本（已满该持有期）的前向收益写入 selection_forward_returns 缓存并固化，
  下次不再重复回算**，只增量计算未成熟样本，大幅减少 tushare 调用。
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

import common
import db
import factor_contract
import factor_config
from registry import register

HORIZONS = [1, 3, 7, 30]
BENCHMARK = "000300.SH"
RETURN_CALC_VERSION = "forward-returns-v2"
MIN_OPTIMIZATION_SAMPLES = 50
MIN_OOS_SAMPLES = 10
VALID_CATEGORIES = {"auto", "manual", "watch", "holding"}


def _to_date(s: str):
    s = str(s).replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


# ---------------- 登记 ----------------
def _capture_selection_price(code: str, selected_date: str) -> dict[str, Any]:
    """仅抓选股交易日当天收盘价；无数据时留空，禁止回退到未来日期。"""
    try:
        pro = common.get_pro()
        payload = common.cached_call(
            "selection_entry_quote", {"code": code, "trade_date": selected_date},
            lambda: pro.daily(ts_code=code, trade_date=selected_date),
            historical=selected_date < common.today_str())
        rows = payload.get("rows", [])
        exact = next((row for row in rows if str(row.get("trade_date")) == selected_date), None)
        if exact and exact.get("close") is not None:
            return {"selected_price": float(exact["close"]),
                    "price_trade_date": selected_date, "price_source": "tushare daily close"}
    except Exception as exc:
        return {"selected_price": None, "price_trade_date": selected_date,
                "price_error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"selected_price": None, "price_trade_date": selected_date,
            "price_error": "选股交易日当天没有可核验收盘价"}


def _capture_factor_snapshot(code: str, selected_date: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """只读取 selected_date 当日或之前、质量合格且与当前公式契约一致的因子。"""
    contract = factor_contract.base_contract("stock")
    sector_contract = factor_config.model_contract("sector")
    dependencies = factor_contract.stock_data_dependencies(sector_contract)
    dependency_hash = factor_contract.fingerprint(dependencies)
    try:
        record = db.fetch_latest_usable_factor(
            code, selected_date, contract["factor_version"], contract["schema_hash"],
            dependency_hash=dependency_hash)
    except Exception as exc:
        return {}, f"因子快照读取失败：{type(exc).__name__}: {exc}"[:300], {}
    if not record:
        return {}, "选股日及之前没有与当前因子契约一致的合格快照", {}
    snapshot = dict(record.get("factors") or {})
    valid, invalid_fields = factor_contract.validate_payload("stock", snapshot)
    if not valid:
        return {}, f"因子快照缺少契约成分：{','.join(invalid_fields)}", {}
    metadata = {
        "factor_trade_date": record["trade_date"],
        "factor_version": record["factor_version"],
        "schema_hash": record["schema_hash"],
        "dependency_hash": record.get("dependency_hash"),
        "dependencies": record.get("dependencies") or dependencies,
        "precompute_run_id": record.get("run_id"),
    }
    return snapshot, "daily_factors_as_of", metadata


@register("log_selection", "review",
          "持久化正式选股/关注/持仓（日期+代码+category 幂等）。保存选股价、热点事件、短线地位、完整理由和量化因子快照；"
          "auto 用于调参，manual/watch/holding 仅隔离回测",
          params=[{"name": "code", "type": "string", "required": True, "desc": "tushare 代码"},
                  {"name": "name", "type": "string", "required": False, "default": ""},
                  {"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认今日"},
                  {"name": "score", "type": "float", "required": False, "default": 0.0,
                   "desc": "兼容字段；新调用请使用 score_percentile"},
                  {"name": "score_raw", "type": "float", "required": False,
                   "desc": "筛选横截面标准化原始分"},
                  {"name": "score_percentile", "type": "float", "required": False,
                   "desc": "筛选横截面0~1分位，回测分桶统一使用"},
                  {"name": "screening_run_id", "type": "string", "required": False,
                   "desc": "screen_quant/screen_trend 返回的运行ID；auto/manual正式选股必填"},
                  {"name": "driver", "type": "string", "required": False, "default": "未标注",
                   "desc": "主导驱动：涨价/逻辑/预期/情绪"},
                  {"name": "reason", "type": "string", "required": True,
                   "desc": "完整理由链：热点/事件、炒作路线地位、受益证据、量化信号、风险证伪"},
                  {"name": "category", "type": "string", "required": False, "default": "auto",
                   "desc": "auto|manual|watch|holding；用户触发正式选股使用 manual"},
                  {"name": "selected_price", "type": "float", "required": False,
                   "desc": "选股时价格；省略则服务端抓最近收盘价"},
                  {"name": "hotspot", "type": "string", "required": False, "default": "",
                   "desc": "所属市场热点/主线"},
                  {"name": "event", "type": "string", "required": False, "default": "",
                   "desc": "当时核心事件或催化"},
                  {"name": "market_role", "type": "string", "required": False, "default": "",
                   "desc": "核心/分支/补涨/非主线"},
                  {"name": "factors", "type": "object", "required": False,
                   "desc": "选股时全部量化因子、行业分和综合分快照"},
                  {"name": "extra", "type": "object", "required": False}],
          returns="登记结果（含选股价格快照）")
def log_selection(p: dict) -> dict:
    cat = p.get("category", "auto")
    if cat not in VALID_CATEGORIES:
        return {"logged": False, "reason": f"category 须为 {sorted(VALID_CATEGORIES)}"}
    code = str(p["code"]).strip().upper()
    selected_date = str(p.get("date") or common.today_str()).replace("-", "")
    try:
        _to_date(selected_date)
    except ValueError:
        return {"logged": False, "reason": "date 必须是有效 YYYYMMDD"}
    extra = dict(p.get("extra") or {})
    run_id = str(p.get("screening_run_id") or extra.get("screening_run_id") or "")
    screening_run = db.get_screening_run(run_id) if run_id else None
    run_candidate = db.get_screening_candidate(run_id, code) if run_id else None
    if cat in {"auto", "manual"}:
        if not screening_run or not run_candidate:
            return {"logged": False, "reason": "正式选股必须引用包含该股票及分数快照的有效筛选运行"}
        if str(screening_run.get("trade_date")) != selected_date:
            return {"logged": False, "reason": "筛选运行交易日必须与选股日期一致"}
        run_contract = screening_run.get("contract") or {}
        source_contract = run_contract.get("source_factor_contract") or run_contract
        current_stock = factor_contract.base_contract("stock")
        current_dependency_hash = factor_contract.fingerprint(
            factor_contract.stock_data_dependencies(factor_config.model_contract("sector")))
        if (source_contract.get("schema_hash") != current_stock["schema_hash"]
                or run_contract.get("dependency_hash") != current_dependency_hash):
            return {"logged": False, "reason": "筛选运行使用的因子结构或上游权重依赖已过期，必须重新筛选"}

    percentile_value = (run_candidate.get("score_percentile") if run_candidate
                        else p.get("score_percentile", extra.get("score_percentile")))
    if cat in {"auto", "manual"} and percentile_value is None:
        return {"logged": False, "reason": "正式选股必须保存 score_percentile（0~1）"}
    try:
        score_percentile = float(percentile_value) if percentile_value is not None else None
        if score_percentile is not None and not 0 <= score_percentile <= 1:
            raise ValueError
    except (TypeError, ValueError):
        return {"logged": False, "reason": "score_percentile 必须在0~1之间"}

    if cat == "auto" or p.get("selected_price") is None:
        price_snapshot = _capture_selection_price(code, selected_date)
    else:
        price_snapshot = {"selected_price": float(p["selected_price"]),
                          "price_trade_date": selected_date,
                          "price_source": extra.get("price_source", "调用方提供")}

    factor_snapshot, factor_source, factor_metadata = _capture_factor_snapshot(code, selected_date)
    if not factor_snapshot and cat in {"auto", "manual"}:
        return {"logged": False, "reason": factor_source}
    if not factor_snapshot:
        supplied = p.get("factors") or extra.get("factors") or {}
        valid, _ = factor_contract.validate_payload("stock", supplied) if supplied else (False, [])
        if valid:
            factor_snapshot, factor_source = dict(supplied), "调用方完整契约快照"

    extra.update(price_snapshot)
    extra.update({
        "hotspot": p.get("hotspot") or extra.get("hotspot", ""),
        "event": p.get("event") or extra.get("event", ""),
        "market_role": p.get("market_role") or extra.get("market_role", ""),
        "factors": factor_snapshot,
        "factor_source": factor_source,
        "factor_contract": factor_contract.base_contract("stock"),
        "factor_metadata": factor_metadata,
        "screening_run_id": run_id or None,
        "screening_function": screening_run.get("function_name") if screening_run else None,
        "screening_contract": screening_run.get("contract") if screening_run else None,
        "score_raw": (run_candidate.get("score_raw") if run_candidate
                      else p.get("score_raw", extra.get("score_raw"))),
        "screening_rank": run_candidate.get("rank") if run_candidate else None,
        "score_percentile": score_percentile,
        "trigger": "scheduled" if cat == "auto" else "user",
    })
    if not factor_snapshot:
        extra["factor_error"] = factor_source
    stored_score = score_percentile if score_percentile is not None else float(p.get("score", 0) or 0)
    rec = {
        "sel_date": _to_date(selected_date), "code": code, "name": p.get("name", ""),
        "score": stored_score, "driver": p.get("driver", "未标注"),
        "reason": p.get("reason", ""), "category": cat, "extra": extra,
        "logged_at": datetime.now(),
    }
    write = db.upsert_selection(rec)
    record = dict(write.get("record") or rec)
    for key in ("sel_date", "logged_at"):
        if record.get(key) is not None:
            record[key] = str(record[key])
    return {"logged": True, "inserted": write.get("inserted", True),
            "immutable": cat in {"auto", "manual"}, "record": record}


# ---------------- 前向收益 ----------------
def _forward_returns_v2(pro, code: str, sel_day: str) -> dict[int, dict[str, Any]]:
    """按上交所统一交易日精确对齐个股和基准；个股只接受 qfq，失败不降级原始价。"""
    calendar_end = (datetime.strptime(sel_day, "%Y%m%d") + timedelta(days=180)).strftime("%Y%m%d")
    try:
        cal = pro.trade_cal(exchange="SSE", start_date=sel_day, end_date=calendar_end)
        dates = sorted(cal[cal["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist())
    except Exception as exc:
        return {h: {"status": "failed", "error": f"交易日历失败：{type(exc).__name__}: {exc}"[:300]}
                for h in HORIZONS}
    if not dates or dates[0] != sel_day:
        return {h: {"status": "failed", "error": "选股日期不是交易日"} for h in HORIZONS}
    last_available = common.last_completed_trade_date()
    mature_dates = {h: dates[h] for h in HORIZONS if len(dates) > h and dates[h] <= last_available}
    results = {h: {"status": "not_matured", "entry_trade_date": sel_day,
                   "exit_trade_date": dates[h] if len(dates) > h else None}
               for h in HORIZONS}
    if not mature_dates:
        return results

    end = max(mature_dates.values())
    try:
        import tushare as ts
        stock_df = ts.pro_bar(ts_code=code, adj="qfq", start_date=sel_day, end_date=end)
    except Exception as exc:
        return {h: ({**results[h], "status": "failed",
                     "error": f"前复权行情失败：{type(exc).__name__}: {exc}"[:300]}
                    if h in mature_dates else results[h]) for h in HORIZONS}
    if stock_df is None or stock_df.empty:
        return {h: ({**results[h], "status": "failed", "error": "前复权行情为空"}
                    if h in mature_dates else results[h]) for h in HORIZONS}
    stock_prices = {str(row["trade_date"]): float(row["close"])
                    for row in stock_df.to_dict(orient="records") if row.get("close") is not None}
    try:
        bench_df = pro.index_daily(ts_code=BENCHMARK, start_date=sel_day, end_date=end)
        bench_prices = {str(row["trade_date"]): float(row["close"])
                        for row in bench_df.to_dict(orient="records") if row.get("close") is not None}
    except Exception:
        bench_prices = {}

    entry = stock_prices.get(sel_day)
    bench_entry = bench_prices.get(sel_day)
    for horizon, exit_day in mature_dates.items():
        exit_price = stock_prices.get(exit_day)
        if entry is None or exit_price is None:
            results[horizon] = {
                "status": "failed", "entry_trade_date": sel_day,
                "exit_trade_date": exit_day,
                "error": "个股在统一入场或退出交易日无前复权价格（可能停牌）",
            }
            continue
        ret = (exit_price / entry - 1) * 100
        bench_exit = bench_prices.get(exit_day)
        benchmark_ret = ((bench_exit / bench_entry - 1) * 100
                         if bench_entry and bench_exit is not None else None)
        results[horizon] = {
            "status": "success", "ret_pct": round(ret, 6),
            "excess_pct": round(ret - benchmark_ret, 6) if benchmark_ret is not None else None,
            "entry_trade_date": sel_day, "entry_price": entry,
            "exit_trade_date": exit_day, "exit_price": exit_price,
            "benchmark_entry_price": bench_entry, "benchmark_exit_price": bench_exit,
            "error": None,
        }
    return results


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
          "按统一交易日和前复权口径回测1/3/7/30日收益；自动保存可审计快照。"
          "只有筛选来源、因子契约、样本量和时序样本外表现均合格时才开放调参。",
          params=[{"name": "save_snapshot", "type": "bool", "required": False, "default": True,
                   "desc": "默认保存本次聚合、样本哈希和优化门禁"}],
          returns="收益统计、optimization_gate、snapshot_id、tuning_hints 与明细")
def selection_backtest(p: dict) -> dict:
    pro = common.get_pro()
    sels = db.fetch_selections()
    current_contract = factor_contract.base_contract("stock")

    cat_returns: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    cat_excess: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_driver: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_bucket: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    details: list[dict[str, Any]] = []
    controlled_30d: list[dict[str, Any]] = []
    computed_calls = 0

    for selection in sels:
        sid = selection["id"]
        sel_day = str(selection["sel_date"]).replace("-", "")
        category = selection.get("category", "auto")
        driver = selection.get("driver", "未标注")
        extra = selection.get("extra") or {}
        percentile = extra.get("score_percentile")
        try:
            percentile = float(percentile) if percentile is not None else None
        except (TypeError, ValueError):
            percentile = None
        bucket = ("high(>=0.75)" if percentile is not None and percentile >= 0.75
                  else "mid(0.55-0.75)" if percentile is not None and percentile >= 0.55
                  else "low(<0.55)" if percentile is not None else "legacy_unknown")
        screening_contract = extra.get("screening_contract") or {}
        source_contract = screening_contract.get("source_factor_contract") or screening_contract
        expected_dependency_hash = factor_contract.fingerprint(
            factor_contract.stock_data_dependencies(factor_config.model_contract("sector")))
        controlled = bool(
            category == "auto"
            and extra.get("trigger") == "scheduled"
            and extra.get("screening_function") == "screen_quant"
            and extra.get("screening_run_id")
            and source_contract.get("schema_hash") == current_contract["schema_hash"]
            and screening_contract.get("dependency_hash") == expected_dependency_hash
            and percentile is not None
        )

        cached = db.get_cached_returns_v2(sid, RETURN_CALC_VERSION)
        if any(cached.get(h, {}).get("status") != "success" for h in HORIZONS):
            calculated = _forward_returns_v2(pro, selection["code"], sel_day)
            computed_calls += 1
            for horizon, item in calculated.items():
                cached[horizon] = db.save_return_v2(
                    sid, horizon, RETURN_CALC_VERSION, **item)

        returns: dict[int, float] = {}
        return_status: dict[int, str] = {}
        for horizon in HORIZONS:
            item = cached.get(horizon) or {}
            return_status[horizon] = item.get("status", "missing")
            if item.get("status") != "success" or item.get("ret_pct") is None:
                continue
            ret = float(item["ret_pct"])
            excess = float(item["excess_pct"]) if item.get("excess_pct") is not None else None
            returns[horizon] = ret
            cat_returns[category][horizon].append(ret)
            if excess is not None:
                cat_excess[category][horizon].append(excess)
            if controlled:
                auto_by_driver[driver][horizon].append(excess if excess is not None else ret)
                auto_by_bucket[bucket][horizon].append(ret)
                if horizon == 30 and excess is not None:
                    controlled_30d.append({"id": sid, "date": sel_day, "driver": driver,
                                           "excess": excess, "return": ret})
        details.append({
            "id": sid, "date": str(selection["sel_date"]), "code": selection["code"],
            "name": selection.get("name", ""), "category": category, "driver": driver,
            "score_percentile": percentile, "bucket": bucket, "controlled_auto": controlled,
            "returns_pct": returns, "return_status": return_status,
            "return_calc_version": RETURN_CALC_VERSION,
        })

    def summarize(values_by_horizon: dict[int, list[float]]) -> dict[str, Any]:
        output = {}
        for horizon in HORIZONS:
            values = values_by_horizon.get(horizon, [])
            if values:
                output[f"{horizon}d"] = {
                    "n": len(values), "avg_pct": round(sum(values) / len(values), 2),
                    "win_rate": round(sum(value > 0 for value in values) / len(values) * 100, 1),
                }
        return output

    controlled_30d.sort(key=lambda row: (row["date"], row["id"]))
    sample_count = len(controlled_30d)
    distinct_dates = len({row["date"] for row in controlled_30d})
    oos_count = max(MIN_OOS_SAMPLES, (sample_count + 4) // 5) if sample_count else 0
    oos = controlled_30d[-oos_count:] if sample_count >= oos_count else []
    oos_avg = sum(row["excess"] for row in oos) / len(oos) if oos else None
    oos_win = sum(row["excess"] > 0 for row in oos) / len(oos) if oos else None
    reasons = []
    if sample_count < MIN_OPTIMIZATION_SAMPLES:
        reasons.append(f"当前契约30日成熟受控样本 {sample_count}，至少需要 {MIN_OPTIMIZATION_SAMPLES}")
    if distinct_dates < 10:
        reasons.append(f"样本仅覆盖 {distinct_dates} 个选股日，至少需要10个独立日期")
    if len(oos) < MIN_OOS_SAMPLES:
        reasons.append(f"时序样本外样本 {len(oos)}，至少需要 {MIN_OOS_SAMPLES}")
    if oos_avg is not None and oos_avg <= 0:
        reasons.append("时序样本外30日平均超额不为正")
    if oos_win is not None and oos_win <= 0.5:
        reasons.append("时序样本外30日超额胜率不高于50%")
    current_dependency_hash = factor_contract.fingerprint(
        factor_contract.stock_data_dependencies(factor_config.model_contract("sector")))
    gate = {
        "eligible": not reasons,
        "schema_hash": current_contract["schema_hash"],
        "dependency_hash": current_dependency_hash,
        "factor_version": current_contract["factor_version"],
        "return_calc_version": RETURN_CALC_VERSION,
        "controlled_sample_count": sample_count,
        "distinct_selection_dates": distinct_dates,
        "oos_sample_count": len(oos),
        "oos_avg_excess_pct": round(oos_avg, 4) if oos_avg is not None else None,
        "oos_excess_win_rate": round(oos_win * 100, 2) if oos_win is not None else None,
        "reasons": reasons,
    }
    tuning_hints = (_tuning_hints(auto_by_driver) if gate["eligible"]
                    else ["禁止自动调参：" + "；".join(reasons)])
    sample_identity = [{"id": row["id"], "date": row["date"]} for row in controlled_30d]
    sample_hash = hashlib.sha256(
        json.dumps(sample_identity, sort_keys=True).encode("utf-8")).hexdigest()
    result = {
        "source": "selection_backtest", "fetched_at": common.now_str(),
        "return_calc_version": RETURN_CALC_VERSION,
        "factor_contract": factor_config.model_contract("stock"),
        "total_selections": len(sels), "recomputed_samples": computed_calls,
        "by_category_return": {key: summarize(value) for key, value in cat_returns.items()},
        "by_category_excess": {key: summarize(value) for key, value in cat_excess.items()},
        "auto_by_driver_excess": {key: summarize(value) for key, value in auto_by_driver.items()},
        "auto_by_bucket_return": {key: summarize(value) for key, value in auto_by_bucket.items()},
        "optimization_gate": gate, "sample_hash": sample_hash,
        "tuning_hints": tuning_hints, "details": details[:100],
        "note": "仅当前因子契约下、来自可核验screen_quant运行的auto样本可进入优化门禁。",
    }
    if p.get("save_snapshot", True):
        snapshot_payload = {key: value for key, value in result.items() if key != "details"}
        snapshot_payload["sample_ids"] = sample_identity
        result["snapshot_id"] = db.save_snapshot("selection", snapshot_payload)
    else:
        result["snapshot_id"] = None
    return result


@register("selection_dashboard", "review",
          "量化选股看板：按当前权限全量或按日期/热点/类别查询持久化选股；访客仅可见 auto/manual，"
          "watch/holding 仅管理员可见。补最近交易日价格、涨幅、换手、成交额及相对选股价表现",
          params=[{"name": "date_from", "type": "string", "required": False, "desc": "起始日期 YYYYMMDD"},
                  {"name": "date_to", "type": "string", "required": False, "desc": "结束日期 YYYYMMDD"},
                  {"name": "hotspot", "type": "string", "required": False, "desc": "热点/主线关键词"},
                  {"name": "category", "type": "string", "required": False,
                   "desc": "auto|manual|watch|holding；省略为当前权限下全部，watch/holding 仅管理员"},
                  {"name": "limit", "type": "int", "required": False, "default": 200}],
          returns="rows / hotspots / quote_trade_date / quote_errors")
def selection_dashboard(p: dict) -> dict:
    category = str(p.get("category") or "").strip()
    if category and category not in VALID_CATEGORIES:
        return {"source": "selection_dashboard", "fetched_at": common.now_str(),
                "rows": [], "error": f"category 须为 {sorted(VALID_CATEGORIES)}"}
    date_from = _to_date(p["date_from"]) if p.get("date_from") else None
    date_to = _to_date(p["date_to"]) if p.get("date_to") else None
    records = db.fetch_selections(date_from, date_to, category or None)
    keyword = str(p.get("hotspot") or "").strip().lower()
    if keyword:
        records = [row for row in records if keyword in " ".join([
            str((row.get("extra") or {}).get("hotspot", "")),
            str((row.get("extra") or {}).get("event", "")),
            str(row.get("reason", "")),
        ]).lower()]
    limit = min(max(int(p.get("limit", 200)), 1), 1000)
    records = records[:limit]

    quote_date: Optional[str] = None
    quote_map: dict[str, dict[str, Any]] = {}
    basic_map: dict[str, dict[str, Any]] = {}
    quote_errors: list[str] = []
    try:
        quote_date = common.last_trade_date()
        pro = common.get_pro()
        daily = common.cached_call(
            "selection_dashboard_daily", {"trade_date": quote_date},
            lambda: pro.daily(trade_date=quote_date), historical=True)
        quote_map = {str(row.get("ts_code", "")).upper(): row
                     for row in daily.get("rows", []) if row.get("ts_code")}
        basics = common.cached_call(
            "selection_dashboard_basic", {"trade_date": quote_date},
            lambda: pro.daily_basic(trade_date=quote_date,
                                    fields="ts_code,turnover_rate,turnover_rate_f,volume_ratio"),
            historical=True)
        basic_map = {str(row.get("ts_code", "")).upper(): row
                     for row in basics.get("rows", []) if row.get("ts_code")}
    except Exception as exc:
        quote_errors.append(f"最近行情获取失败：{type(exc).__name__}: {exc}"[:500])

    rows: list[dict[str, Any]] = []
    hotspot_counts: dict[str, int] = defaultdict(int)
    for record in records:
        extra = record.get("extra") or {}
        hotspot = str(extra.get("hotspot") or "未分类")
        hotspot_counts[hotspot] += 1
        code = str(record.get("code", "")).upper()
        quote = quote_map.get(code, {})
        basic = basic_map.get(code, {})
        selected_price = extra.get("selected_price")
        current_price = quote.get("close")
        since_return = None
        try:
            if selected_price is not None and current_price is not None and float(selected_price):
                since_return = round((float(current_price) / float(selected_price) - 1) * 100, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            since_return = None
        rows.append({
            "id": record.get("id"), "date": str(record.get("sel_date")),
            "logged_at": record.get("logged_at").strftime("%Y-%m-%d %H:%M:%S")
            if record.get("logged_at") else None,
            "code": code, "name": record.get("name", ""),
            "category": record.get("category", ""), "score": float(record.get("score") or 0),
            "score_raw": extra.get("score_raw"),
            "score_percentile": extra.get("score_percentile"),
            "driver": record.get("driver", ""), "hotspot": hotspot,
            "event": extra.get("event", ""), "market_role": extra.get("market_role", ""),
            "reason": record.get("reason", ""),
            "selected_price": float(selected_price) if selected_price is not None else None,
            "selected_price_date": extra.get("price_trade_date"),
            "latest_price": float(current_price) if current_price is not None else None,
            "latest_chg_pct": float(quote["pct_chg"]) if quote.get("pct_chg") is not None else None,
            "since_selection_pct": since_return,
            "turnover_rate": float(basic["turnover_rate"]) if basic.get("turnover_rate") is not None else None,
            "amount": float(quote["amount"]) if quote.get("amount") is not None else None,
            "factors": extra.get("factors") or {},
            "factor_contract": extra.get("factor_contract") or {},
            "factor_metadata": extra.get("factor_metadata") or {},
            "screening_run_id": extra.get("screening_run_id"),
            "screening_function": extra.get("screening_function"),
            "factor_error": extra.get("factor_error", ""),
            "trigger": extra.get("trigger", ""),
        })
    hotspots = [{"name": name, "count": count} for name, count in sorted(
        hotspot_counts.items(), key=lambda item: (-item[1], item[0]))]
    return {"source": "selection_dashboard", "fetched_at": common.now_str(),
            "quote_trade_date": quote_date, "quote_errors": quote_errors,
            "total": len(rows), "hotspots": hotspots, "rows": rows,
            "note": "最新行情为最近交易日收盘数据；缺失项保持为空，不做推断"}


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(selection_backtest({}), ensure_ascii=False, indent=2))

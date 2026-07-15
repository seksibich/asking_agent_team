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
from registry import ParamError, register

BENCH_INDEX = "000300.SH"
FACTOR_VERSION = factors.STOCK_FACTOR_VERSION
LOOKBACK_DEFAULT = 260
MIN_COVERAGE_RATIO = 0.80


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
    payload = common.cached_call("daily_basic_slice", {"trade_date": date},
                                 lambda: pro.daily_basic(trade_date=date,
                                                         fields="ts_code,turnover_rate"),
                                 historical=True)
    return {
        str(row.get("ts_code", "")).strip().upper(): row.get("turnover_rate")
        for row in payload.get("rows", [])
        if row.get("ts_code")
    }


def _basic_map(pro, date: str) -> dict[str, dict[str, Any]]:
    """全市场某日估值/市值切片（供规模/价值/盈利收益率等候选因子，永久缓存复用）。"""
    payload = common.cached_call(
        "daily_basic_val_slice", {"trade_date": date},
        lambda: pro.daily_basic(trade_date=date,
                                fields="ts_code,pe_ttm,pe,pb,circ_mv,total_mv"),
        historical=True)
    return {
        str(row.get("ts_code", "")).strip().upper(): row
        for row in payload.get("rows", [])
        if row.get("ts_code")
    }


def _active_universe(pro, date: str) -> set[str]:
    """读取当日可选股票集合，并统一排除 ST/退市标的。"""
    payload = common.cached_call(
        "stock_universe", {"trade_date": date},
        lambda: pro.stock_basic(list_status="L", fields="ts_code,name,market"))
    rows = payload.get("rows", [])
    codes = {
        str(row.get("ts_code", "")).strip().upper()
        for row in rows
        if row.get("ts_code")
        and "ST" not in str(row.get("name", "")).upper()
        and "退" not in str(row.get("name", ""))
    }
    if not codes:
        raise RuntimeError("stock_basic returned empty eligible universe")
    return codes


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _compute_for_date(pro, target: str, lookback: int) -> dict[str, Any]:
    """为交易日计算因子，记录覆盖质量并以整日结果替换落库。"""
    started = datetime.now()
    errors: list[str] = []

    def finish(status: str, universe_count: int = 0, computed_count: int = 0,
               coverage_ratio: float = 0.0, reason: str = "") -> dict[str, Any]:
        if reason:
            errors.append(reason)
        finished = datetime.now()
        db.upsert_daily_factor_run({
            "trade_date": target,
            "factor_version": FACTOR_VERSION,
            "lookback": lookback,
            "universe_count": universe_count,
            "computed_count": computed_count,
            "coverage_ratio": round(coverage_ratio, 4),
            "status": status,
            "errors": errors[:50],
            "started_at": started,
            "finished_at": finished,
        })
        return {
            "date": target,
            "status": status,
            "stocks": computed_count,
            "universe_count": universe_count,
            "coverage_ratio": round(coverage_ratio, 4),
            "errors": errors[:50],
        }

    try:
        dates = _trade_dates(pro, target, lookback)
    except Exception as exc:
        return finish("failed", reason=f"交易日历获取失败：{_error_text(exc)}")
    if not dates:
        return finish("failed", reason="无法获取交易日历")
    if dates[-1] != target:
        return finish("skipped", reason="目标日期不是交易日，未写入因子")

    try:
        universe = _active_universe(pro, target)
    except Exception as exc:
        return finish("failed", reason=f"股票池获取失败：{_error_text(exc)}")

    frames: list[pd.DataFrame] = []
    for day in dates:
        try:
            sl = _daily_slice(pro, day)
            if sl.empty:
                errors.append(f"{day} 日线为空")
                continue
            required = {"ts_code", "trade_date", "close", "vol"}
            missing = sorted(required - set(sl.columns))
            if missing:
                errors.append(f"{day} 日线缺少字段：{','.join(missing)}")
                continue
            keep = [c for c in ("ts_code", "trade_date", "close", "vol", "amount") if c in sl.columns]
            frames.append(sl[keep])
        except Exception as exc:
            errors.append(f"{day} 日线获取失败：{_error_text(exc)}")
    if not frames:
        return finish("failed", len(universe), reason="回看窗口没有有效日线数据")

    allbars = pd.concat(frames, ignore_index=True)
    if "ts_code" in allbars.columns:
        allbars["ts_code"] = allbars["ts_code"].astype(str).str.upper()
        allbars = allbars[allbars["ts_code"].isin(universe)]
    turnover: dict[str, float] = {}
    try:
        turnover = _turnover_map(pro, target)
        if not turnover:
            errors.append(f"{target} 换手率数据为空")
    except Exception as exc:
        errors.append(f"{target} 换手率获取失败：{_error_text(exc)}")
    basic: dict[str, dict[str, Any]] = {}
    try:
        basic = _basic_map(pro, target)
        if not basic:
            errors.append(f"{target} 估值数据为空")
    except Exception as exc:
        errors.append(f"{target} 估值数据获取失败：{_error_text(exc)}")

    bar_cols = [c for c in ("trade_date", "close", "vol", "amount") if c in allbars.columns]
    items: list[dict[str, Any]] = []
    for code, group in allbars.groupby("ts_code"):
        try:
            fac = factors.compute_stock_factors(group[bar_cols], turnover.get(code), basic.get(code))
            if fac is not None:
                items.append({"code": code, "factors": fac})
        except Exception as exc:
            errors.append(f"{code} 因子计算失败：{_error_text(exc)}")

    coverage_ratio = len(items) / len(universe) if universe else 0.0
    quality_ok = coverage_ratio >= MIN_COVERAGE_RATIO and not errors
    status = "success" if quality_ok else "partial"
    # 无论成功或部分成功都先清理目标日旧结果，防止残留股票污染覆盖统计。
    db.delete_daily_factors(target)
    if items:
        db.bulk_upsert_daily_factors(target, items)
    return finish(status, len(universe), len(items), coverage_ratio)


@register("precompute_daily_factors", "screening",
          "全市场个股因子预计算：按交易日整批计算、覆盖质量校验并落库；不合格结果不供选股读取",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"},
                  {"name": "lookback", "type": "int", "required": False, "default": LOOKBACK_DEFAULT,
                   "desc": "因子计算回看交易日数（最少252，保证12-1动量）"},
                  {"name": "full", "type": "bool", "required": False, "default": False,
                   "desc": "true=为最近lookback个交易日逐日补算，单日失败继续后续日期"}],
          returns="status / dates_computed / date_results / failed_dates / partial_dates / coverage_threshold")
def precompute_daily_factors(p: dict) -> dict:
    try:
        pro = common.get_pro()
        end = p.get("date") or common.last_trade_date()
    except Exception as exc:
        return {
            "source": "precompute_daily_factors",
            "fetched_at": common.now_str(),
            "factor_version": FACTOR_VERSION,
            "status": "failed",
            "dates_computed": [],
            "date_results": [{"date": p.get("date"), "status": "failed", "stocks": 0,
                              "errors": [f"交易日历或数据服务初始化失败：{_error_text(exc)}"]}],
            "failed_dates": [p.get("date")] if p.get("date") else [],
            "partial_dates": [],
            "retryable_dates": [p.get("date")] if p.get("date") else [],
            "coverage_threshold": MIN_COVERAGE_RATIO,
        }
    lookback = int(p.get("lookback", LOOKBACK_DEFAULT))
    if lookback < 252:
        raise ParamError("lookback 不能小于 252")

    date_results: list[dict[str, Any]] = []
    if p.get("full"):
        try:
            targets = _trade_dates(pro, end, lookback)
        except Exception as exc:
            targets = []
            date_results.append({"date": end, "status": "failed", "stocks": 0,
                                 "errors": [f"交易日历获取失败：{_error_text(exc)}"]})
        if not targets and not date_results:
            date_results.append({"date": end, "status": "failed", "stocks": 0,
                                 "errors": ["交易日历为空"]})
        for index, target in enumerate(targets):
            if index < 60:
                date_results.append({"date": target, "status": "skipped",
                                     "stocks": 0, "reason": "历史窗口不足60个交易日"})
                continue
            try:
                date_results.append(_compute_for_date(pro, target, lookback))
            except Exception as exc:
                date_results.append({"date": target, "status": "failed", "stocks": 0,
                                     "errors": [_error_text(exc)]})
    else:
        try:
            date_results.append(_compute_for_date(pro, end, lookback))
        except Exception as exc:
            date_results.append({"date": end, "status": "failed", "stocks": 0,
                                 "errors": [_error_text(exc)]})

    dates_computed = [item["date"] for item in date_results if item.get("status") == "success"]
    failed_dates = [item["date"] for item in date_results if item.get("status") == "failed"]
    partial_dates = [item["date"] for item in date_results if item.get("status") == "partial"]
    successful_count = len(dates_computed)
    if failed_dates or partial_dates:
        overall_status = "partial" if successful_count else "failed"
    elif successful_count:
        overall_status = "success"
    else:
        overall_status = "skipped"

    return {
        "source": "precompute_daily_factors",
        "fetched_at": common.now_str(),
        "factor_version": FACTOR_VERSION,
        "lookback": lookback,
        "status": overall_status,
        "dates_computed": dates_computed,
        "stocks_per_date": {item["date"]: item.get("stocks", 0) for item in date_results},
        "date_results": date_results,
        "failed_dates": failed_dates,
        "partial_dates": partial_dates,
        "retryable_dates": failed_dates + partial_dates,
        "coverage_threshold": MIN_COVERAGE_RATIO,
        "note": "只有 status=success 且覆盖率达标的日期会被 screen_quant/screen_trend 读取",
    }


@register("precompute_status", "screening",
          "预计算覆盖状态：日期覆盖数量、任务质量、因子版本和失败信息",
          params=[{"name": "limit", "type": "int", "required": False, "default": 30,
                   "desc": "返回最近多少个交易日"}],
          returns="latest_date / latest_usable_date / runs[{status,coverage_ratio,errors}]")
def precompute_status(p: dict) -> dict:
    limit = int(p.get("limit", 30))
    runs = db.daily_factor_run_status(limit)
    coverage = db.factor_date_counts(limit)
    usable = [run["trade_date"] for run in runs if run.get("status") == "success"
              and run.get("factor_version") == FACTOR_VERSION]
    return {
        "source": "precompute_status",
        "fetched_at": common.now_str(),
        "factor_version": FACTOR_VERSION,
        "latest_date": db.latest_factor_date(),
        "latest_usable_date": max(usable) if usable else None,
        "coverage": coverage,
        "runs": runs,
        "coverage_threshold": MIN_COVERAGE_RATIO,
        "note": "只有 success 且因子版本一致的日期会被选股读取；failed/partial 可按 retryable_dates 重跑",
    }


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(precompute_daily_factors({}), ensure_ascii=False, indent=2))

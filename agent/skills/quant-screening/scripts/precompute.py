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
import threading
import uuid
from datetime import datetime, time, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import common
import db
import factor_contract
import factor_config
import factors
from registry import ParamError, register

BENCH_INDEX = "000300.SH"
FACTOR_VERSION = factors.STOCK_FACTOR_VERSION
STOCK_CONTRACT = factor_contract.base_contract("stock")
SECTOR_CONTRACT = factor_contract.base_contract("sector")
LOOKBACK_DEFAULT = 260
MIN_COVERAGE_RATIO = 0.80
MARKET_CLOSE_TIME = time(15, 0)


def _trade_dates(pro, end: str, n: int) -> list[str]:
    """通过交易所日历返回截至 end 的最近 n 个交易日（升序）。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=n * 2 + 30)).strftime("%Y%m%d")
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
    if df is None or df.empty or not {"cal_date", "is_open"}.issubset(df.columns):
        return []
    open_days = df[df["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist()
    return sorted(open_days)[-n:]


def _shanghai_now() -> datetime:
    """返回上海时区当前时间，避免容器系统时区影响目标日期。"""
    return datetime.now(ZoneInfo(common.TZ))


def _default_target_date(pro, now: Optional[datetime] = None) -> str:
    """确定增量预计算目标日：交易日收盘后取当天，否则取上一交易日。"""
    current = now or _shanghai_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(common.TZ))
    else:
        current = current.astimezone(ZoneInfo(common.TZ))
    cutoff = current.date()
    if current.time().replace(tzinfo=None) < MARKET_CLOSE_TIME:
        cutoff -= timedelta(days=1)
    dates = _trade_dates(pro, cutoff.strftime("%Y%m%d"), 1)
    if not dates:
        raise RuntimeError("无法确定待补算的最近交易日")
    return dates[-1]


def _is_historical_date(date: str) -> bool:
    """仅已结束的历史日期可永久缓存；当天数据需允许稍后重试。"""
    return date < _shanghai_now().strftime("%Y%m%d")


def _daily_slice(pro, date: str) -> pd.DataFrame:
    """全市场某日 daily 切片；历史日永久缓存，当天不缓存空结果。"""
    historical = _is_historical_date(date)
    payload = common.cached_call("daily_slice", {"trade_date": date},
                                 lambda: pro.daily(trade_date=date),
                                 use_cache=historical, historical=historical)
    return pd.DataFrame(payload.get("rows", []))


def _turnover_map(pro, date: str) -> dict[str, float]:
    historical = _is_historical_date(date)
    payload = common.cached_call("daily_basic_slice", {"trade_date": date},
                                 lambda: pro.daily_basic(trade_date=date,
                                                         fields="ts_code,turnover_rate"),
                                 use_cache=historical, historical=historical)
    return {
        str(row.get("ts_code", "")).strip().upper(): row.get("turnover_rate")
        for row in payload.get("rows", [])
        if row.get("ts_code")
    }


def _basic_map(pro, date: str) -> dict[str, dict[str, Any]]:
    """全市场某日估值/市值切片（历史日永久缓存，当天允许重试）。"""
    historical = _is_historical_date(date)
    payload = common.cached_call(
        "daily_basic_val_slice", {"trade_date": date},
        lambda: pro.daily_basic(trade_date=date,
                                fields="ts_code,pe_ttm,pe,pb,circ_mv,total_mv"),
        use_cache=historical, historical=historical)
    return {
        str(row.get("ts_code", "")).strip().upper(): row
        for row in payload.get("rows", [])
        if row.get("ts_code")
    }


def _active_universe(pro, date: str) -> set[str]:
    """按目标日上市/退市区间构造股票池，避免历史补算的幸存者偏差。"""
    rows: list[dict[str, Any]] = []
    for status in ("L", "D", "P"):
        payload = common.cached_call(
            "stock_universe_history", {"list_status": status},
            lambda s=status: pro.stock_basic(
                list_status=s, fields="ts_code,name,market,list_date,delist_date"),
            historical=True)
        rows.extend(payload.get("rows", []))
    codes = set()
    for row in rows:
        code = str(row.get("ts_code", "")).strip().upper()
        listed = str(row.get("list_date") or "")
        delisted = str(row.get("delist_date") or "")
        if not code or not listed or listed > date or (delisted and delisted <= date):
            continue
        # 北交所（.BJ）不在申万一级行业成分体系内，且短线量化策略不覆盖，
        # 从源头排除，避免每日刷屏「缺少有效行业成分映射」并拉低覆盖率。
        if code.endswith(".BJ"):
            continue
        name = str(row.get("name", ""))
        if "ST" in name.upper() or "退" in name:
            continue
        codes.add(code)
    if not codes:
        raise RuntimeError("stock_basic returned empty eligible universe")
    return codes


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _sector_strength_map(pro, target: str,
                         sector_contract: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """计算行业评分并按目标日历史成分映射，写库由最终原子发布统一完成。"""
    import screen_sector

    scores = screen_sector.compute_sector_scores(
        pro, target, weights=sector_contract["weights"])
    if not scores:
        return {}, []
    sector_items = [{
        "code": row["code"], "name": row.get("name", ""),
        "score": row.get("score", 0), "percentile": row.get("percentile", 0),
        "factors": {key: row.get(key) for key in (
            "sec_mom_12_1", "sec_mom_20d", "sec_mom_5d",
            "sec_vol_confirm", "sec_low_vol", "last_close") if key in row},
    } for row in scores]

    stock_map: dict[str, dict[str, Any]] = {}
    for sector in scores:
        payload = common.cached_call(
            "sw_l1_members", {"code": sector["code"], "date": target},
            lambda code=sector["code"]: pro.index_member(index_code=code),
            historical=_is_historical_date(target))
        members = pd.DataFrame(payload.get("rows", []))
        code_col = next((c for c in ("con_code", "ts_code", "code") if c in members.columns), None)
        if not code_col:
            continue
        if _is_historical_date(target) and not {"in_date", "out_date"}.issubset(members.columns):
            raise RuntimeError(
                f"{sector.get('name') or sector['code']} 历史成分缺少 in_date/out_date，拒绝用当前成分冒充 {target}")
        if "in_date" in members.columns:
            members = members[members["in_date"].fillna("").astype(str) <= target]
        if "out_date" in members.columns:
            out_dates = members["out_date"].fillna("").astype(str)
            members = members[(out_dates == "") | (out_dates > target)]
        context = {
            "industry_code": sector["code"], "industry_name": sector.get("name", ""),
            "industry_score": float(sector.get("score", 0) or 0),
            "industry_strength": float(sector.get("percentile", 0) or 0),
            "membership_as_of": target,
        }
        for code in members[code_col].astype(str).str.upper():
            old = stock_map.get(code)
            if old is None or context["industry_strength"] > old["industry_strength"]:
                stock_map[code] = context
    return stock_map, sector_items


def _compute_for_date(pro, target: str, lookback: int, job_id: str,
                      progress: Optional[Callable[[int, str], None]] = None) -> dict[str, Any]:
    """为交易日计算因子；仅当前任务租约可原子发布完整契约结果。"""
    started = datetime.now()
    errors: list[str] = []
    sector_items: list[dict[str, Any]] = []
    sector_count = 0
    sector_runtime_contract = factor_config.model_contract("sector")
    dependencies = factor_contract.stock_data_dependencies(sector_runtime_contract)
    dependency_hash = factor_contract.fingerprint(dependencies)

    def report(percent: int, message: str) -> None:
        if progress:
            progress(max(0, min(100, percent)), message)

    report(2, "读取交易日历")

    def finish(status: str, universe_count: int = 0, computed_count: int = 0,
               coverage_ratio: float = 0.0, reason: str = "") -> dict[str, Any]:
        if reason:
            errors.append(reason)
        finished = datetime.now()
        record = {
            "trade_date": target, "factor_version": FACTOR_VERSION,
            "schema_hash": STOCK_CONTRACT["schema_hash"],
            "dependency_hash": dependency_hash, "dependencies": dependencies,
            "factor_components": STOCK_CONTRACT["components"], "run_id": job_id,
            "lookback": lookback, "universe_count": universe_count,
            "computed_count": computed_count, "coverage_ratio": round(coverage_ratio, 4),
            "status": status, "errors": errors[:50],
            "started_at": started, "finished_at": finished,
        }
        if db.precompute_job_owned(job_id):
            existing = db.get_daily_factor_run(target)
            preserve_success = bool(
                existing and existing.get("status") == "success"
                and existing.get("factor_version") == FACTOR_VERSION
                and existing.get("schema_hash") == STOCK_CONTRACT["schema_hash"]
                and existing.get("dependency_hash") == dependency_hash)
            if not preserve_success:
                db.upsert_daily_factor_run(record)
            else:
                errors.append("本次失败未覆盖同契约的既有成功质量记录")
        else:
            errors.append("任务租约已失效，结果未写入")
        return {
            "date": target,
            "status": status,
            "stocks": computed_count,
            "sectors": sector_count,
            "universe_count": universe_count,
            "coverage_ratio": round(coverage_ratio, 4),
            "factor_version": FACTOR_VERSION,
            "schema_hash": STOCK_CONTRACT["schema_hash"],
            "dependency_hash": dependency_hash,
            "dependencies": dependencies,
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

    report(8, "读取可计算股票池")
    try:
        universe = _active_universe(pro, target)
    except Exception as exc:
        return finish("failed", reason=f"股票池获取失败：{_error_text(exc)}")

    frames: list[pd.DataFrame] = []
    total_dates = max(1, len(dates))
    for day_index, day in enumerate(dates):
        if day_index % 10 == 0 or day_index == total_dates - 1:
            report(12 + int(38 * (day_index + 1) / total_dates),
                   f"拉取历史日线 {day_index + 1}/{total_dates}")
        try:
            sl = _daily_slice(pro, day)
            if sl.empty:
                if day == target:
                    return finish("failed", len(universe),
                                  reason=f"{target} 收盘行情尚未就绪，未写入因子")
                errors.append(f"{day} 日线为空")
                continue
            required = {"ts_code", "trade_date", "close", "vol"}
            missing = sorted(required - set(sl.columns))
            if missing:
                if day == target:
                    return finish("failed", len(universe),
                                  reason=f"{target} 收盘行情字段不完整，未写入因子：{','.join(missing)}")
                errors.append(f"{day} 日线缺少字段：{','.join(missing)}")
                continue
            keep = [c for c in ("ts_code", "trade_date", "close", "vol", "amount") if c in sl.columns]
            frames.append(sl[keep])
        except Exception as exc:
            if day == target:
                return finish("failed", len(universe),
                              reason=f"{target} 收盘行情获取失败，未写入因子：{_error_text(exc)}")
            errors.append(f"{day} 日线获取失败：{_error_text(exc)}")
    if not frames:
        return finish("failed", len(universe), reason="回看窗口没有有效日线数据")

    allbars = pd.concat(frames, ignore_index=True)
    if "ts_code" in allbars.columns:
        allbars["ts_code"] = allbars["ts_code"].astype(str).str.upper()
        allbars = allbars[allbars["ts_code"].isin(universe)]
    turnover: dict[str, float] = {}
    report(52, "读取换手率与估值切片")
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

    industry_map: dict[str, dict[str, Any]] = {}
    report(60, "计算行业强度与成分映射")
    try:
        industry_map, sector_items = _sector_strength_map(
            pro, target, sector_runtime_contract)
        sector_count = len(sector_items)
        if not industry_map or not sector_count:
            errors.append(f"{target} 行业评分或成分映射为空")
    except Exception as exc:
        errors.append(f"{target} 行业评分计算失败：{_error_text(exc)}")

    bar_cols = [c for c in ("trade_date", "close", "vol", "amount") if c in allbars.columns]
    items: list[dict[str, Any]] = []
    missing_industry: list[str] = []
    total_stocks = max(1, int(allbars["ts_code"].nunique()))
    for stock_index, (code, group) in enumerate(allbars.groupby("ts_code")):
        if stock_index % 100 == 0 or stock_index == total_stocks - 1:
            report(68 + int(24 * (stock_index + 1) / total_stocks),
                   f"计算个股因子 {stock_index + 1}/{total_stocks}")
        try:
            fac = factors.compute_stock_factors(group[bar_cols], turnover.get(code), basic.get(code))
            if fac is not None:
                industry = industry_map.get(code)
                if not industry:
                    # 预期内跳过（如无申万行业归属的特殊标的），循环后汇总为一条，避免逐只刷屏。
                    missing_industry.append(code)
                    continue
                fac["industry_strength"] = float(industry["industry_strength"])
                fac["_meta"] = industry
                valid, invalid_fields = factor_contract.validate_payload("stock", fac)
                if not valid:
                    errors.append(f"{code} 因子契约不完整：{','.join(invalid_fields)}")
                    continue
                items.append({"code": code, "factors": fac})
        except Exception as exc:
            errors.append(f"{code} 因子计算失败：{_error_text(exc)}")

    if missing_industry:
        sample = "、".join(missing_industry[:5])
        suffix = f" 等（示例 {sample}）" if len(missing_industry) > 5 else f"（{sample}）"
        errors.append(f"{len(missing_industry)} 只股票缺少 {target} 有效行业成分映射被跳过，未写入中性伪值{suffix}")

    coverage_ratio = len(items) / len(universe) if universe else 0.0
    quality_ok = coverage_ratio >= MIN_COVERAGE_RATIO and sector_count > 0
    status = "success" if quality_ok else "partial"
    report(96, "校验任务租约并原子发布因子契约结果")
    db.save_factor_contract(STOCK_CONTRACT)
    db.save_factor_contract(SECTOR_CONTRACT)
    finished = datetime.now()
    run_record = {
        "trade_date": target, "factor_version": FACTOR_VERSION,
        "schema_hash": STOCK_CONTRACT["schema_hash"],
        "dependency_hash": dependency_hash, "dependencies": dependencies,
        "factor_components": STOCK_CONTRACT["components"], "run_id": job_id,
        "lookback": lookback, "universe_count": len(universe),
        "computed_count": len(items), "coverage_ratio": round(coverage_ratio, 4),
        "status": status, "errors": errors[:50],
        "started_at": started, "finished_at": finished,
    }
    published = db.publish_daily_factor_bundle(
        job_id, target, items, sector_items, run_record,
        FACTOR_VERSION, STOCK_CONTRACT["schema_hash"],
        SECTOR_CONTRACT["factor_version"], SECTOR_CONTRACT["schema_hash"],
        dependency_hash, dependencies)
    if published is None:
        errors.append("任务租约已失效，个股、行业和质量结果均未发布")
        return {"date": target, "status": "failed", "stocks": 0, "sectors": 0,
                "universe_count": len(universe), "coverage_ratio": 0,
                "dependency_hash": dependency_hash, "dependencies": dependencies,
                "errors": errors[-50:]}
    if published == "preserved_success":
        errors.append("本次部分结果未覆盖同契约、同依赖的既有成功快照")
    report(100, "交易日因子契约结果已原子发布")
    return {
        "date": target, "status": status, "stocks": len(items), "sectors": sector_count,
        "universe_count": len(universe), "coverage_ratio": round(coverage_ratio, 4),
        "factor_version": FACTOR_VERSION, "schema_hash": STOCK_CONTRACT["schema_hash"],
        "dependency_hash": dependency_hash, "dependencies": dependencies,
        "publication": published,
        "errors": errors[:50],
    }


def _record_non_success_attempt(target: str, lookback: int, job_id: str,
                                status: str, reason: str) -> dict[str, Any]:
    """为跳过或未预期异常补齐日级审计；同契约既有成功记录不被覆盖。"""
    dependencies = factor_contract.stock_data_dependencies(
        factor_config.model_contract("sector"))
    dependency_hash = factor_contract.fingerprint(dependencies)
    errors = [reason[:500]]
    record = {
        "trade_date": target, "factor_version": FACTOR_VERSION,
        "schema_hash": STOCK_CONTRACT["schema_hash"],
        "dependency_hash": dependency_hash, "dependencies": dependencies,
        "factor_components": STOCK_CONTRACT["components"], "run_id": job_id,
        "lookback": lookback, "universe_count": 0, "computed_count": 0,
        "coverage_ratio": 0.0, "status": status, "errors": errors,
        "started_at": datetime.now(), "finished_at": datetime.now(),
    }
    publication = "lease_lost"
    if db.precompute_job_owned(job_id):
        existing = db.get_daily_factor_run(target)
        preserve = bool(
            existing and existing.get("status") == "success"
            and existing.get("factor_version") == FACTOR_VERSION
            and existing.get("schema_hash") == STOCK_CONTRACT["schema_hash"]
            and existing.get("dependency_hash") == dependency_hash)
        if preserve:
            publication = "preserved_success"
            errors.append("本次非成功结果未覆盖同契约、同依赖的既有成功质量记录")
        else:
            db.upsert_daily_factor_run(record)
            publication = "quality_recorded"
    return {
        "date": target, "status": status, "stocks": 0, "sectors": 0,
        "universe_count": 0, "coverage_ratio": 0.0,
        "factor_version": FACTOR_VERSION, "schema_hash": STOCK_CONTRACT["schema_hash"],
        "dependency_hash": dependency_hash, "dependencies": dependencies,
        "publication": publication, "errors": errors,
    }


def _execute_precompute(p: dict, notify: Callable[..., bool], job_id: str) -> dict[str, Any]:
    """执行实际计算；每个交易日前与最终发布时都校验唯一任务租约。"""
    pro = common.get_pro()
    end = p.get("date") or _default_target_date(pro)
    lookback = int(p.get("lookback", LOOKBACK_DEFAULT))
    date_results: list[dict[str, Any]] = []

    if p.get("full"):
        targets = _trade_dates(pro, end, lookback)
        if not targets:
            raise RuntimeError("交易日历为空")
    else:
        targets = [end]

    total = len(targets)
    notify(progress=1, stage="准备数据", message="已确定待计算交易日",
           current_date=end, completed_count=0, total_count=total)
    for index, target in enumerate(targets):
        if not db.precompute_job_owned(job_id):
            raise RuntimeError("预计算任务租约已失效，旧 worker 已停止")
        if p.get("full") and index < 60:
            date_results.append(_record_non_success_attempt(
                target, lookback, job_id, "skipped", "历史窗口不足60个交易日"))
            notify(progress=int((index + 1) / total * 100), stage="跳过历史日期",
                   message=f"{target} 历史窗口不足", current_date=target,
                   completed_count=index + 1, total_count=total)
            continue

        def date_progress(percent: int, message: str) -> None:
            overall = int((index + percent / 100) / total * 100)
            notify(progress=min(99, overall), stage="计算交易日", message=message,
                   current_date=target, completed_count=index, total_count=total)

        try:
            date_results.append(_compute_for_date(pro, target, lookback, job_id, date_progress))
        except Exception as exc:
            date_results.append(_record_non_success_attempt(
                target, lookback, job_id, "failed", f"未预期异常：{_error_text(exc)}"))
        notify(progress=int((index + 1) / total * 100), stage="交易日已完成",
               message=f"{target} 处理完成", current_date=target,
               completed_count=index + 1, total_count=total)

    dates_computed = [item["date"] for item in date_results if item.get("status") == "success"]
    failed_dates = [item["date"] for item in date_results if item.get("status") == "failed"]
    partial_dates = [item["date"] for item in date_results if item.get("status") == "partial"]
    if failed_dates and not dates_computed and not partial_dates:
        overall_status = "failed"
    elif failed_dates or partial_dates:
        overall_status = "partial"
    elif dates_computed:
        overall_status = "success"
    else:
        overall_status = "skipped"
    dependencies = factor_contract.stock_data_dependencies(
        factor_config.model_contract("sector"))
    dependency_hash = factor_contract.fingerprint(dependencies)

    return {
        "source": "precompute_daily_factors",
        "fetched_at": common.now_str(),
        "factor_version": FACTOR_VERSION,
        "schema_hash": STOCK_CONTRACT["schema_hash"],
        "dependency_hash": dependency_hash,
        "dependencies": dependencies,
        "factor_components": STOCK_CONTRACT["components"],
        "lookback": lookback,
        "status": overall_status,
        "dates_computed": dates_computed,
        "stocks_per_date": {item["date"]: item.get("stocks", 0) for item in date_results},
        "date_results": date_results,
        "failed_dates": failed_dates,
        "partial_dates": partial_dates,
        "retryable_dates": failed_dates + partial_dates,
        "coverage_threshold": MIN_COVERAGE_RATIO,
        "note": "只有 success、覆盖率达标、公式版本/结构哈希/完整依赖指纹一致且行与质量记录 run_id 相同的日期可被筛选读取",
    }


def _run_precompute_job(job_id: str, params: dict[str, Any]) -> None:
    """后台线程入口；任何异常都会把唯一任务可靠地落为失败终态。"""
    db.update_precompute_job(job_id, status="running", stage="启动任务",
                             message="后台预计算已启动")
    try:
        result = _execute_precompute(
            params, lambda **changes: db.update_precompute_job(job_id, **changes), job_id)
        status = result.get("status", "failed")
        stage = {
            "success": "计算完成", "partial": "部分完成",
            "skipped": "任务已跳过", "failed": "计算失败",
        }.get(status, "任务结束")
        db.update_precompute_job(
            job_id, status=status, progress=100, stage=stage,
            message=f"成功 {len(result.get('dates_computed', []))} 日，"
                    f"部分 {len(result.get('partial_dates', []))} 日，"
                    f"失败 {len(result.get('failed_dates', []))} 日",
            result=result, error=None, finished_at=datetime.now())
    except Exception as exc:  # noqa: BLE001
        db.update_precompute_job(
            job_id, status="failed", stage="任务异常", message="后台预计算执行失败",
            error=_error_text(exc), finished_at=datetime.now())


@register("precompute_daily_factors", "screening",
          "启动全市场因子后台预计算；全服务唯一，重复调用返回当前任务及实时进度",
          params=[{"name": "date", "type": "string", "required": False,
                   "desc": "YYYYMMDD；默认交易日收盘后取当天，否则取上一交易日"},
                  {"name": "lookback", "type": "int", "required": False, "default": LOOKBACK_DEFAULT,
                   "desc": "因子计算回看交易日数（最少252，保证12-1动量）"},
                  {"name": "full", "type": "bool", "required": False, "default": False,
                   "desc": "true=为最近lookback个交易日逐日补算，单日失败继续后续日期"}],
          returns="accepted / already_running / job{job_id,status,progress,stage,current_date}")
def precompute_daily_factors(p: dict) -> dict:
    lookback = int(p.get("lookback", LOOKBACK_DEFAULT))
    if lookback < 252:
        raise ParamError("lookback 不能小于 252")
    date_value = str(p.get("date") or "").strip()
    if date_value:
        try:
            datetime.strptime(date_value, "%Y%m%d")
        except ValueError as exc:
            raise ParamError("date 必须是 YYYYMMDD 格式的有效日期") from exc

    params = {"lookback": lookback, "full": bool(p.get("full"))}
    if date_value:
        params["date"] = date_value
    job_id = uuid.uuid4().hex
    started, job = db.claim_precompute_job(job_id, params)
    if started:
        try:
            threading.Thread(
                target=_run_precompute_job,
                args=(job_id, params),
                name=f"precompute-{job_id[:8]}",
                daemon=True,
            ).start()
        except Exception as exc:  # pragma: no cover - 仅线程运行时异常
            db.update_precompute_job(
                job_id, status="failed", stage="启动失败", message="后台线程启动失败",
                error=_error_text(exc), finished_at=datetime.now())
        job = db.get_precompute_job() or job
    return {
        "source": "precompute_daily_factors",
        "fetched_at": common.now_str(),
        "accepted": started,
        "already_running": not started and bool(job.get("active")),
        "status": job.get("status"),
        "job": job,
    }


# 列表接口的异常摘要限额：只回传前若干条、每条截断，完整明细按需经 precompute_run_errors 拉取。
ERROR_PREVIEW_COUNT = 2
ERROR_PREVIEW_LEN = 80


def _coerce_errors(errors: Any) -> list[str]:
    """把质量记录里的 errors 统一成字符串列表。"""
    if errors is None:
        return []
    if isinstance(errors, list):
        return [str(e) for e in errors]
    return [str(errors)]


def _summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    """列表返回只保留异常摘要（条数 + 截断预览），避免超长文本撑爆响应与表格。"""
    item = dict(run)
    errors = _coerce_errors(item.get("errors"))
    preview = [e[:ERROR_PREVIEW_LEN] for e in errors[:ERROR_PREVIEW_COUNT]]
    item.pop("errors", None)
    item["error_count"] = len(errors)
    item["errors_preview"] = preview
    item["errors_truncated"] = (
        len(errors) > ERROR_PREVIEW_COUNT
        or any(len(e) > ERROR_PREVIEW_LEN for e in errors[:ERROR_PREVIEW_COUNT])
    )
    return item


@register("precompute_status", "screening",
          "预计算覆盖状态：日期覆盖数量、任务质量、因子版本和失败信息摘要（异常仅回传条数与预览，完整明细用 precompute_run_errors 按日拉取）",
          params=[{"name": "limit", "type": "int", "required": False, "default": 30,
                   "desc": "返回最近多少个交易日"}],
          returns="latest_date / latest_usable_date / runs[{status,coverage_ratio,error_count,errors_preview,errors_truncated}]")
def precompute_status(p: dict) -> dict:
    limit = int(p.get("limit", 30))
    current = _shanghai_now()
    completed_cutoff = current.date()
    if current.time().replace(tzinfo=None) < MARKET_CLOSE_TIME:
        completed_cutoff -= timedelta(days=1)
    cutoff_text = completed_cutoff.strftime("%Y%m%d")
    current_dependencies = factor_contract.stock_data_dependencies(
        factor_config.model_contract("sector"))
    current_dependency_hash = factor_contract.fingerprint(current_dependencies)
    runs = [_summarize_run(run) for run in db.daily_factor_run_status(limit)
            if str(run.get("trade_date") or "") <= cutoff_text]
    coverage = [row for row in db.usable_factor_date_counts(
        FACTOR_VERSION, STOCK_CONTRACT["schema_hash"], current_dependency_hash, limit,
        MIN_COVERAGE_RATIO) if str(row.get("trade_date") or "") <= cutoff_text]
    usable = [row["trade_date"] for row in coverage]
    task = db.get_precompute_job()
    if (task and not task.get("active")
            and str(task.get("current_date") or "") > cutoff_text):
        task = None
    return {
        "source": "precompute_status",
        "fetched_at": common.now_str(),
        "factor_version": FACTOR_VERSION,
        "schema_hash": STOCK_CONTRACT["schema_hash"],
        "factor_components": STOCK_CONTRACT["components"],
        "dependency_hash": current_dependency_hash,
        "dependencies": current_dependencies,
        "latest_date": coverage[0]["trade_date"] if coverage else None,
        "latest_usable_date": max(usable) if usable else None,
        "task": task,
        "coverage": coverage,
        "runs": runs,
        "coverage_threshold": MIN_COVERAGE_RATIO,
        "note": "只有 success、覆盖率达标、公式版本/结构哈希/完整依赖指纹一致且 run_id 绑定一致的日期会被筛选读取；活跃任务可通过 task 持续读取进度",
    }


@register("precompute_run_errors", "screening",
          "按交易日拉取该日预计算的完整异常明细；供前端在列表点击「查看」时按需加载，避免状态接口回传超长文本",
          params=[{"name": "trade_date", "type": "string", "required": True,
                   "desc": "YYYYMMDD 交易日"}],
          returns="trade_date / status / error_count / errors[]")
def precompute_run_errors(p: dict) -> dict:
    trade_date = str(p.get("trade_date") or "").strip()
    if not trade_date:
        raise ParamError("trade_date 必填")
    run = db.get_daily_factor_run(trade_date)
    base = {
        "source": "precompute_run_errors",
        "fetched_at": common.now_str(),
        "trade_date": trade_date,
    }
    if not run:
        base.update({"status": None, "run_id": None, "finished_at": None,
                     "error_count": 0, "errors": []})
        return base
    errors = _coerce_errors(run.get("errors"))
    base.update({
        "status": run.get("status"),
        "run_id": run.get("run_id"),
        "finished_at": run.get("finished_at"),
        "error_count": len(errors),
        "errors": errors,
    })
    return base


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(precompute_daily_factors({}), ensure_ascii=False, indent=2))

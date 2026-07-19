"""板块选择 / 板块轮动量化（趋势有效因子）。

依据回测经验：A股行业/板块层面动量为正（轮动具延续性），故用
板块 12-1 / 20 日 / 5 日动量 + 量能确认 + 低波动 合成打分排名。

数据源优先申万一级行业指数（sw_daily），无权限时回退到 tushare 概念/板块指数。
可选：对排名靠前的板块，进一步在成分股内做个股量化选股（trend/quant）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
import uuid

import pandas as pd

import common
import db
import factor_contract
import factors
import factor_config
from registry import ParamError, register

VALID_MODES = {"latest_complete", "historical", "intraday"}
INTRADAY_PHASES = {"morning", "lunch", "afternoon", "closed_pending"}
INTRADAY_MIN_SECTORS = 28
INTRADAY_MIN_MEMBER_COVERAGE = 0.75
FACTOR_FIELDS = tuple(factors.SECTOR_FACTOR_WEIGHTS) + ("last_close",)


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
    return [{"code": r[code_col], "name": r[name_col]}
            for r in df.to_dict(orient="records")]


def _sector_history(pro, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """获取板块指数日线历史（申万）。"""
    try:
        df = pro.sw_daily(ts_code=code, start_date=start, end_date=end)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        return pro.index_daily(ts_code=code, start_date=start, end_date=end)
    except Exception:
        return None


def _history_covers_date(frame: Optional[pd.DataFrame], target: str) -> bool:
    """确认指数日线确实包含目标日，禁止用上一交易日冒充现场日线评分。"""
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return False
    dates = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
    return target in set(dates)


def compute_sector_scores(pro, end: str,
                          weights: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
    """按目标交易日现场计算申万一级行业评分，不接受目标日缺失的旧日线。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    w = factor_config.effective_weights("sector")
    if weights is not None:
        if set(weights) != set(w):
            raise ValueError("自定义行业权重必须包含契约中的全部因子")
        w = {name: float(weights[name]) for name in w}
        if abs(sum(w.values()) - 1.0) > 0.01:
            raise ValueError("自定义行业权重之和必须为1.0")
    rows: list[dict[str, Any]] = []
    for industry in _sw_l1_industries(pro):
        history = _sector_history(pro, industry["code"], start, end)
        if not _history_covers_date(history, end):
            continue
        fac = factors.compute_sector_factors(history)
        if fac is None:
            continue
        fac["code"] = industry["code"]
        fac["name"] = industry["name"]
        rows.append(fac)
    if not rows:
        return []
    table = factors.composite_score(pd.DataFrame(rows), w, strict=True)
    table["percentile"] = table["score_percentile"]
    columns = ["code", "name", *FACTOR_FIELDS, "score", "percentile"]
    return table[[column for column in columns if column in table.columns]].sort_values(
        "score", ascending=False).round(6).to_dict(orient="records")


def _normalize_requested_date(value: Optional[str]) -> Optional[str]:
    """接受 YYYYMMDD 或 YYYY-MM-DD，并拒绝不存在的日历日期。"""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    formats = ("%Y%m%d", "%Y-%m-%d")
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ParamError("date 必须是合法的 YYYYMMDD 或 YYYY-MM-DD 日期")


def _validate_weights(weights: Optional[dict[str, float]]) -> tuple[dict[str, float], dict[str, Any]]:
    """校验权重并生成带完整上游依赖的行业评分契约。"""
    current = factor_config.effective_weights("sector")
    if weights is not None:
        if set(weights) != set(current):
            raise ParamError("自定义行业权重必须包含契约中的全部因子")
        try:
            current = {name: float(weights[name]) for name in current}
        except (TypeError, ValueError) as exc:
            raise ParamError("自定义行业权重必须全部为数值") from exc
        if abs(sum(current.values()) - 1.0) > 0.01:
            raise ParamError("自定义行业权重之和必须为1.0")
    contract = factor_contract.weighted_contract(
        "sector", current, factor_config.current_weight_version("sector") or "default")
    if weights is not None:
        contract["weight_version"] = f"request:{contract['weight_hash'][:12]}"
    dependencies = factor_contract.stock_data_dependencies(contract)
    contract["data_dependencies"] = dependencies
    contract["dependency_hash"] = factor_contract.fingerprint(dependencies)
    return current, contract


def _public_sector(row: dict[str, Any]) -> dict[str, Any]:
    """把持久化行业行转换为兼容旧返回且显式保留 factors 的结构。"""
    factor_values = dict(row.get("factors") or {})
    if not factor_values:
        factor_values = {name: row[name] for name in FACTOR_FIELDS if name in row}
    item = {
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "score": float(row.get("score") or 0),
        "percentile": float(row.get("percentile") or 0),
        "factors": factor_values,
    }
    item.update(factor_values)
    return item


def _history_sector(row: dict[str, Any], trade_date: str) -> dict[str, Any]:
    """生成看板趋势与热力图所需的逐日逐行业记录。"""
    public = _public_sector(row)
    return {
        "trade_date": trade_date,
        "code": public["code"],
        "name": public["name"],
        "score": public["score"],
        "percentile": public["percentile"],
        "factors": public["factors"],
    }


def _trade_day_gap(date_from: str, date_to: str) -> int:
    """计算两个日期之间经过的交易日数量；同日为 0。"""
    if date_from >= date_to:
        return 0
    frame = common.get_pro().trade_cal(
        exchange="SSE", start_date=date_from, end_date=date_to)
    if frame is None or frame.empty:
        raise RuntimeError("无法计算快照陈旧交易日数")
    open_dates = frame.loc[frame["is_open"].astype(int) == 1, "cal_date"].astype(str)
    return int(((open_dates > date_from) & (open_dates <= date_to)).sum())


def _unavailable_result(requested_trade_date: Optional[str], market_session: str,
                        reason: str, available_dates: list[str]) -> dict[str, Any]:
    """返回字段完整且不伪造评分的明确不可用结果。"""
    return {
        "source": "screen/sector", "fetched_at": common.now_str(),
        "trade_date": None, "data_source": "unavailable", "sectors": [],
        "requested_trade_date": requested_trade_date, "effective_date": None,
        "baseline_trade_date": None, "market_session": market_session,
        "data_mode": "unavailable", "is_final": False,
        "is_complete": False, "is_stale": False, "stale_trade_days": 0,
        "fallback_reason": reason, "available_dates": available_dates,
        "history": [], "error": reason,
    }


def _load_history(contract: dict[str, Any], as_of: str, history_days: int,
                  on_demand_date: Optional[str] = None,
                  on_demand_sectors: Optional[list[dict[str, Any]]] = None,
                  ) -> tuple[list[str], list[dict[str, Any]]]:
    """读取同一契约历史；现场目标日只追加真实计算结果，不写数据库。"""
    available = db.usable_sector_score_dates(
        as_of=as_of, factor_version=contract["factor_version"],
        schema_hash=contract["schema_hash"],
        dependency_hash=contract["dependency_hash"], limit=history_days)
    if on_demand_date and on_demand_date not in available:
        available = [on_demand_date, *available]
    available = available[:history_days]
    rows: list[dict[str, Any]] = []
    persisted_dates = [date for date in available if date != on_demand_date]
    if persisted_dates:
        persisted = db.fetch_usable_sector_score_history(
            min(persisted_dates), max(persisted_dates),
            contract["factor_version"], contract["schema_hash"],
            contract["dependency_hash"])
        allowed = set(persisted_dates)
        rows.extend(_history_sector(row, str(row["trade_date"]))
                    for row in persisted if str(row["trade_date"]) in allowed)
    if on_demand_date and on_demand_sectors:
        rows.extend(_history_sector(row, on_demand_date) for row in on_demand_sectors)
    rows.sort(key=lambda row: (row["trade_date"], -row["score"], row["code"]))
    return available, rows


def _active_sector_members(pro, sector_code: str, target: str) -> set[str]:
    """读取目标日有效申万一级行业成分；盘中只进当日缓存，不写 final 历史。"""
    payload = common.cached_call(
        "sw_l1_members_intraday", {"code": sector_code, "date": target},
        lambda: pro.index_member(index_code=sector_code),
        historical=False, data_status="provisional", trade_date=target,
    )
    members = pd.DataFrame(payload.get("rows") or [])
    code_col = next((name for name in ("con_code", "ts_code", "code")
                     if name in members.columns), None)
    if members.empty or not code_col:
        return set()
    if "is_new" in members.columns:
        is_new = members["is_new"].fillna("").astype(str).str.upper()
        if (is_new == "Y").any():
            members = members[is_new == "Y"]
    if "in_date" in members.columns:
        values = members["in_date"].fillna("").astype(str).str.replace("-", "", regex=False)
        members = members[(values == "") | (values <= target)]
    if "out_date" in members.columns:
        values = members["out_date"].fillna("").astype(str).str.replace("-", "", regex=False)
        members = members[(values == "") | (values > target)]
    return {
        str(code).strip().upper() for code in members[code_col].tolist()
        if str(code).strip().upper().endswith((".SH", ".SZ"))
    }


def _intraday_sector_overlay(pro, sectors: list[dict[str, Any]], target: str,
                             market_session: str) -> dict[str, Any]:
    """把经质量门禁的当日全市场快照聚合为临时行业宽度，绝不写 final 表。"""
    unavailable = {
        "status": "unavailable", "available": False, "is_reliable": False,
        "data_mode": "intraday", "is_final": False, "quote_date": target,
        "as_of": common.now_str(), "rows": [],
    }
    if market_session not in INTRADAY_PHASES:
        return {**unavailable, "reason": f"当前市场阶段 {market_session} 不提供盘中行业覆盖"}
    try:
        import market_data
        snapshot = market_data.fetch_realtime_market_snapshot()
    except Exception as exc:
        return {**unavailable, "reason": f"全市场实时快照获取失败：{type(exc).__name__}: {exc}"[:300]}

    coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
    if (snapshot.get("quote_date") != target or snapshot.get("degraded") is True
            or coverage.get("validation_passed") is not True):
        errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else []
        reason = "；".join(str(item) for item in errors[:3]) or "日期或全市场覆盖率门禁未通过"
        return {**unavailable, "source": snapshot.get("source"), "coverage": coverage,
                "reason": reason[:500], "as_of": snapshot.get("as_of") or common.now_str()}

    quotes = pd.DataFrame(snapshot.get("rows") or [])
    if quotes.empty or not {"TS_CODE", "PCT_CHANGE"}.issubset(quotes.columns):
        return {**unavailable, "source": snapshot.get("source"), "coverage": coverage,
                "reason": "实时快照缺少股票代码或涨跌幅字段"}
    quotes["TS_CODE"] = quotes["TS_CODE"].fillna("").astype(str).str.upper().str.strip()
    quotes["PCT_CHANGE"] = pd.to_numeric(quotes["PCT_CHANGE"], errors="coerce")
    quotes = quotes[
        quotes["TS_CODE"].str.fullmatch(r"\d{6}\.(?:SH|SZ)", na=False)
        & quotes["PCT_CHANGE"].between(-30, 30, inclusive="both")
    ].drop_duplicates("TS_CODE")
    quote_map = dict(zip(quotes["TS_CODE"], quotes["PCT_CHANGE"]))

    overlays: list[dict[str, Any]] = []
    member_errors: list[str] = []
    for sector in sectors:
        code = str(sector.get("code") or "")
        try:
            members = _active_sector_members(pro, code, target)
        except Exception as exc:
            member_errors.append(f"{code}: {type(exc).__name__}: {exc}"[:200])
            continue
        changes = [float(quote_map[member]) for member in members if member in quote_map]
        member_count = len(members)
        ratio = len(changes) / member_count if member_count else 0.0
        if member_count < 5 or ratio < INTRADAY_MIN_MEMBER_COVERAGE:
            continue
        overlays.append({
            "code": code, "name": str(sector.get("name") or ""),
            "status": "available", "available": True, "is_reliable": True,
            "data_mode": "intraday", "is_final": False,
            "change_pct": round(sum(changes) / len(changes), 3),
            "adv_ratio": round(sum(value > 0 for value in changes) / len(changes) * 100, 2),
            "decline_ratio": round(sum(value < 0 for value in changes) / len(changes) * 100, 2),
            "flat_ratio": round(sum(value == 0 for value in changes) / len(changes) * 100, 2),
            "sample_count": len(changes), "member_count": member_count,
            "coverage_ratio": round(ratio, 4),
            "quote_date": target, "as_of": snapshot.get("as_of") or common.now_str(),
            "source": snapshot.get("source"),
        })
    if len(overlays) < min(INTRADAY_MIN_SECTORS, len(sectors)):
        reason = f"可靠行业覆盖不足：{len(overlays)}/{len(sectors)}"
        if member_errors:
            reason += f"；成分读取异常 {len(member_errors)} 个"
        return {**unavailable, "source": snapshot.get("source"), "coverage": coverage,
                "reason": reason, "as_of": snapshot.get("as_of") or common.now_str(),
                "errors": member_errors[:5]}
    return {
        "status": "available", "available": True, "is_reliable": True,
        "data_mode": "intraday", "is_final": False,
        "quote_date": target, "as_of": snapshot.get("as_of") or common.now_str(),
        "source": snapshot.get("source"), "coverage": coverage,
        "sector_count": len(overlays), "rows": overlays,
        "note": "临时行业宽度仅用于盘中观察，不参与申万完整日评分且不写历史表",
    }


def run(weights: Optional[dict[str, float]] = None, top_n: int = 10,
        with_stocks: bool = False, stocks_per_sector: int = 5,
        mode: str = "latest_complete", date: Optional[str] = None,
        history_days: int = 20) -> dict[str, Any]:
    """按最新完整、严格历史或盘中基线三种口径返回行业评分。"""
    normalized_mode = str(mode or "latest_complete").strip().lower()
    if normalized_mode not in VALID_MODES:
        raise ParamError("mode 必须是 latest_complete、historical 或 intraday")
    requested_date = _normalize_requested_date(date)
    history_limit = int(history_days)
    if not 3 <= history_limit <= 120:
        raise ParamError("history_days 必须在 3 到 120 之间")
    normalized_top_n = max(1, min(int(top_n), 100))
    if with_stocks and normalized_mode != "latest_complete":
        raise ParamError("with_stocks 仅支持 latest_complete 模式")

    current_weights, contract = _validate_weights(weights)
    clock = common.market_clock()
    market_session = str(clock["phase"])
    last_ready = str(clock["last_data_ready_date"])
    last_calendar = str(clock["last_calendar_trade_date"])
    fallback_reason: Optional[str] = None
    on_demand_date: Optional[str] = None
    on_demand_sectors: Optional[list[dict[str, Any]]] = None
    overlay: Optional[dict[str, Any]] = None

    if normalized_mode == "historical":
        if requested_date is None:
            raise ParamError("historical 模式必须提供 date")
        if requested_date > last_ready:
            raise ParamError(f"请求日 {requested_date} 尚未达到日终数据就绪线 {last_ready}")
        if not common.is_trade_open(requested_date):
            raise ParamError(f"请求日 {requested_date} 不是交易日")
        target_date = requested_date
        persisted = db.fetch_usable_daily_sector_scores(
            target_date, contract["factor_version"], contract["schema_hash"],
            contract["dependency_hash"])
        if not persisted:
            available = db.usable_sector_score_dates(
                as_of=target_date, factor_version=contract["factor_version"],
                schema_hash=contract["schema_hash"],
                dependency_hash=contract["dependency_hash"], limit=history_limit)
            suffix = f"；此前可用日期：{','.join(available[:5])}" if available else ""
            raise ParamError(f"请求日 {target_date} 没有同契约合格行业快照{suffix}")
        sectors = [_public_sector(row) for row in persisted]
        effective_date = target_date
        data_source = "persisted"
        requested_trade_date = target_date
        is_complete = True
    elif normalized_mode == "intraday":
        requested_trade_date = requested_date or last_calendar
        if requested_date and requested_date != last_calendar:
            raise ParamError(f"intraday 仅支持当前市场日期 {last_calendar}")
        effective_date = db.latest_usable_sector_score_date(
            as_of=last_ready, factor_version=contract["factor_version"],
            schema_hash=contract["schema_hash"],
            dependency_hash=contract["dependency_hash"])
        if not effective_date:
            return _unavailable_result(
                requested_trade_date, market_session,
                "没有可作为盘中基线的同契约 final 行业快照", [])
        persisted = db.fetch_usable_daily_sector_scores(
            effective_date, contract["factor_version"], contract["schema_hash"],
            contract["dependency_hash"])
        if not persisted:
            return _unavailable_result(
                requested_trade_date, market_session,
                "最新 final 基线未通过完整契约复核", [])
        sectors = [_public_sector(row) for row in persisted]
        data_source = "persisted"
        is_complete = False
        target_date = requested_trade_date
        overlay = _intraday_sector_overlay(
            common.get_pro(), sectors, target_date, market_session)
        if overlay.get("available") is not True:
            fallback_reason = str(overlay.get("reason") or "盘中覆盖层未通过质量门禁")
    else:
        requested_trade_date = requested_date or last_ready
        if requested_trade_date > last_ready:
            raise ParamError(f"请求日 {requested_trade_date} 晚于数据就绪日 {last_ready}")
        if not common.is_trade_open(requested_trade_date):
            raise ParamError(f"请求日 {requested_trade_date} 不是交易日")
        target_date = requested_trade_date
        persisted = db.fetch_usable_daily_sector_scores(
            target_date, contract["factor_version"], contract["schema_hash"],
            contract["dependency_hash"])
        if persisted:
            sectors = [_public_sector(row) for row in persisted]
            effective_date = target_date
            data_source = "persisted"
        else:
            compute_error = ""
            try:
                calculated = compute_sector_scores(common.get_pro(), target_date, weights)
                if not calculated:
                    raise RuntimeError("目标日行业指数日线为空或覆盖不足")
                sectors = [_public_sector(row) for row in calculated]
                effective_date = target_date
                data_source = "on_demand_daily"
                on_demand_date = target_date
                on_demand_sectors = sectors
            except Exception as exc:
                compute_error = f"{type(exc).__name__}: {exc}"[:300]
                fallback_date = db.latest_usable_sector_score_date(
                    as_of=target_date, factor_version=contract["factor_version"],
                    schema_hash=contract["schema_hash"],
                    dependency_hash=contract["dependency_hash"])
                if not fallback_date:
                    return _unavailable_result(
                        requested_trade_date, market_session,
                        f"目标日无持久化快照，现场日线计算失败，且无更早合格快照：{compute_error}", [])
                fallback_rows = db.fetch_usable_daily_sector_scores(
                    fallback_date, contract["factor_version"], contract["schema_hash"],
                    contract["dependency_hash"])
                if not fallback_rows:
                    return _unavailable_result(
                        requested_trade_date, market_session,
                        f"现场日线计算失败，旧快照复核也失败：{compute_error}", [])
                sectors = [_public_sector(row) for row in fallback_rows]
                effective_date = fallback_date
                data_source = "persisted"
                fallback_reason = (
                    f"目标日 {target_date} 无合格持久化快照且现场日线计算失败；"
                    f"明确回退至 {fallback_date}：{compute_error}")
        is_complete = True

    if not sectors:
        return _unavailable_result(
            requested_trade_date, market_session, "没有可返回的行业评分", [])
    sectors.sort(key=lambda row: row["score"], reverse=True)
    top_sectors = sectors[:normalized_top_n]
    available_dates, history = _load_history(
        contract, effective_date, history_limit, on_demand_date, on_demand_sectors)
    stale_trade_days = _trade_day_gap(effective_date, target_date)
    is_stale = effective_date != target_date
    if is_stale:
        is_complete = False

    run_id = uuid.uuid4().hex
    db.save_screening_snapshot(factor_contract.base_contract("sector"), {
        "run_id": run_id, "function_name": "screen_sector", "trade_date": effective_date,
        "factor_version": contract["factor_version"], "schema_hash": contract["schema_hash"],
        "weight_version": contract["weight_version"], "contract": contract,
        "candidate_codes": [row["code"] for row in top_sectors],
        "candidates": top_sectors,
        "params": {
            "mode": normalized_mode, "date": requested_date,
            "history_days": history_limit, "top_n": normalized_top_n,
            "custom_weights": weights is not None,
            "with_stocks": bool(with_stocks), "stocks_per_sector": int(stocks_per_sector),
        },
    })
    result: dict[str, Any] = {
        "source": "screen/sector", "fetched_at": common.now_str(),
        "trade_date": effective_date, "data_source": data_source,
        "requested_trade_date": requested_trade_date,
        "effective_date": effective_date, "baseline_trade_date": effective_date,
        "market_session": market_session,
        "data_mode": "intraday" if normalized_mode == "intraday" else "final",
        "is_final": bool(is_complete and not is_stale),
        "is_complete": is_complete,
        "is_stale": is_stale, "stale_trade_days": stale_trade_days,
        "fallback_reason": fallback_reason, "available_dates": available_dates,
        "history": history, "screening_run_id": run_id,
        "factor_contract": contract, "weights": current_weights,
        "factor_note": "行业层面动量为正：12-1/20日/5日动量 + 量能确认 + 低波动；percentile 为行业横截面强度分位",
        "sectors": top_sectors,
        "note": "行业轮动量化排名，仍须由 Agent 叠加涨价、景气周期与事件催化交叉验证",
    }
    if overlay is not None:
        result["intraday_overlay"] = overlay
    if with_stocks:
        import quant_screen
        picks = {}
        for sector in top_sectors[:3]:
            sub = quant_screen.run([sector["name"]], top_n=stocks_per_sector)
            picks[sector["name"]] = sub.get("candidates", [])
        result["stock_picks_by_sector"] = picks
    return result


@register(
    "screen_sector", "screening",
    "按最新完整、严格历史或盘中 final 基线口径执行申万一级行业轮动评分",
    params=[
        {"name": "weights", "type": "object", "required": False,
         "desc": "可选完整行业因子权重；历史快照必须与该权重契约一致"},
        {"name": "top_n", "type": "int", "required": False, "default": 10},
        {"name": "mode", "type": "string", "required": False,
         "default": "latest_complete", "desc": "latest_complete|historical|intraday"},
        {"name": "date", "type": "string", "required": False,
         "desc": "YYYYMMDD 或 YYYY-MM-DD；historical 必填"},
        {"name": "history_days", "type": "int", "required": False, "default": 20,
         "desc": "同一契约历史交易日数量，范围 3-120"},
        {"name": "with_stocks", "type": "bool", "required": False, "default": False,
         "desc": "仅 latest_complete 可用；在前3板块内选个股"},
        {"name": "stocks_per_sector", "type": "int", "required": False, "default": 5},
    ],
    returns=("sectors/trade_date/data_source，并返回 requested_trade_date、effective_date、"
             "baseline_trade_date、market_session、完整性/陈旧度、fallback_reason、"
             "available_dates 与逐日逐行业 history；intraday 明示覆盖层是否可用"),
)
def screen_sector(p: dict) -> dict:
    return run(
        weights=p.get("weights"), top_n=p.get("top_n", 10),
        with_stocks=p.get("with_stocks", False),
        stocks_per_sector=p.get("stocks_per_sector", 5),
        mode=p.get("mode", "latest_complete"), date=p.get("date"),
        history_days=p.get("history_days", 20),
    )


if __name__ == "__main__":
    import json
    print(json.dumps(run(with_stocks=False), ensure_ascii=False, indent=2))

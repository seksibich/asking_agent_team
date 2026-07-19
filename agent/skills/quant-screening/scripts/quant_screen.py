"""量化多因子选股（趋势 + 情绪，回测有效因子）。

因子引擎见 scripts/factors.py。个股层面采用：
12-1 动量 + 1个月反转 + 低波动 + 低换手 + 趋势确认 + 量能确认。
（个股短期为反转，故用 reversal_1m；中期趋势用 mom_12_1。）

底层数据来自 tushare daily / daily_basic。因子横截面标准化后加权合成排名。
量化候选须由 Agent 叠加 涨价>逻辑>预期>情绪 四维交叉验证后方可入选。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import db
import factor_contract
import factors
import factor_config
from registry import register


def _table_from_db(end: str, code_filter: Optional[set],
                   contract: dict[str, Any], dependency_hash: str) -> Optional[pd.DataFrame]:
    """仅加载质量合格且行级公式版本、结构哈希和完整成分均一致的预计算结果。"""
    try:
        if not db.has_usable_daily_factors(
                end, contract["factor_version"], schema_hash=contract["schema_hash"],
                dependency_hash=dependency_hash):
            return None
        rows = db.fetch_daily_factors(end)
    except Exception:
        return None
    recs = []
    for row in rows:
        code = row["code"]
        if code_filter is not None and code not in code_filter:
            continue
        if (row.get("factor_version") != contract["factor_version"]
                or row.get("schema_hash") != contract["schema_hash"]
                or row.get("dependency_hash") != dependency_hash):
            continue
        fac = dict(row["factors"])
        valid, _ = factor_contract.validate_payload("stock", fac)
        if not valid:
            continue
        meta = fac.pop("_meta", None) or {}
        fac["industry_name"] = meta.get("industry_name", "未映射")
        fac["industry_score"] = meta.get("industry_score", 0.0)
        fac["code"] = code
        recs.append(fac)
    return pd.DataFrame(recs) if recs else None


def _attach_quotes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按交易日批量补充候选行情，最多请求六个日截面而非逐股请求。"""
    report: dict[str, Any] = {
        "requested_count": len(rows), "covered_count": 0,
        "missing_codes": [], "errors": [],
    }
    if not rows:
        return report
    end = str(common.market_clock()["last_data_ready_date"])
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d")
    pro = common.get_pro()
    codes = {str(row.get("code") or "").upper() for row in rows if row.get("code")}
    try:
        calendar = pro.trade_cal(exchange="SSE", start_date=start, end_date=end)
        calendar_frame = pd.DataFrame(calendar)
        trade_dates = sorted(calendar_frame[
            calendar_frame["is_open"].astype(int).eq(1)
        ]["cal_date"].astype(str).tolist())[-6:]
    except Exception as exc:
        trade_dates = [end]
        report["errors"].append(f"交易日历读取失败：{type(exc).__name__}: {exc}"[:300])

    frames: list[pd.DataFrame] = []
    for trade_date in trade_dates:
        try:
            payload = common.cached_call(
                "daily_market_cross_section", {"td": trade_date},
                lambda td=trade_date: pro.daily(trade_date=td),
                historical=True, data_status="final", expected_end=trade_date)
            frame = pd.DataFrame(payload.get("rows") or [])
            if frame.empty:
                report["errors"].append(f"{trade_date} 日行情为空")
                continue
            code_column = "ts_code" if "ts_code" in frame.columns else "TS_CODE"
            frame[code_column] = frame[code_column].astype(str).str.upper()
            frames.append(frame[frame[code_column].isin(codes)].rename(
                columns={code_column: "ts_code"}))
        except Exception as exc:
            report["errors"].append(
                f"{trade_date} 日行情失败：{type(exc).__name__}: {exc}"[:300])

    history = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    by_code = {code: part.sort_values("trade_date") for code, part in history.groupby("ts_code")} \
        if not history.empty and "trade_date" in history.columns else {}
    for row in rows:
        code = str(row.get("code") or "").upper()
        frame = by_code.get(code)
        if frame is None or frame.empty or str(frame.iloc[-1]["trade_date"]) != end:
            report["missing_codes"].append(code)
            continue
        closes = pd.to_numeric(frame["close"], errors="coerce").dropna().tolist()
        if not closes:
            report["missing_codes"].append(code)
            continue
        row["last"] = round(float(closes[-1]), 2)
        if "pct_chg" in frame.columns:
            pct = pd.to_numeric(pd.Series([frame.iloc[-1]["pct_chg"]]), errors="coerce").iloc[0]
            if pd.notna(pct):
                row["chg"] = round(float(pct), 2)
        if len(closes) >= 6 and float(closes[-6]) != 0:
            row["ret5"] = round((float(closes[-1]) / float(closes[-6]) - 1) * 100, 2)
        report["covered_count"] += 1
    report.update({"trade_date": end, "request_count": len(trade_dates),
                   "degraded": bool(report["missing_codes"] or report["errors"])})
    return report


def _build_factor_table(pro, codes: list[str], end: str) -> pd.DataFrame:
    """为候选股构建因子原始值表。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    turnover_map: dict[str, Any] = {}
    basic_map: dict[str, dict[str, Any]] = {}
    try:
        basics = pro.daily_basic(trade_date=end,
                                 fields="ts_code,turnover_rate,pe_ttm,pe,pb,circ_mv,total_mv")
        for r in basics.to_dict(orient="records"):
            turnover_map[r["ts_code"]] = r.get("turnover_rate")
            basic_map[r["ts_code"]] = r
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    for code in codes:
        try:
            df = pro.daily(ts_code=code, start_date=start, end_date=end)
        except Exception:
            continue
        fac = factors.compute_stock_factors(df, turnover_map.get(code), basic_map.get(code))
        if fac is None:
            continue
        fac["code"] = code
        rows.append(fac)
    return pd.DataFrame(rows)


def _stock_name_members(pro, stock_names: list[str]) -> set[str]:
    """按个股名称匹配上市股票；多个名称取并集，精确名称优先于模糊名称。"""
    from screen_trend import _norm_terms

    terms = _norm_terms(stock_names)
    if not terms:
        return set()
    try:
        payload = common.cached_call(
            "stock_basic_ind", {"d": common.today_str()},
            lambda: pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry"))
        basic = pd.DataFrame(payload.get("rows", []))
    except Exception:
        return set()
    if basic.empty or "ts_code" not in basic.columns or "name" not in basic.columns:
        return set()

    valid = basic[~basic["name"].astype(str).str.contains("ST|退", na=False)].copy()
    names = valid["name"].astype(str)
    codes: set[str] = set()
    for term in terms:
        exact = valid[names.str.casefold() == term.casefold()]
        matched = exact if not exact.empty else valid[names.str.contains(term, case=False, regex=False, na=False)]
        codes.update(matched["ts_code"].astype(str).tolist())
    return codes


_BOARD_LABELS = {
    "main": "沪深主板",
    "star": "科创板",
    "gem": "创业板",
}


def _board_of(code: str) -> str:
    """按 tushare 股票代码归类市场；北交所独立识别但不纳入当前量化股票池。"""
    raw = str(code or "").strip().upper()
    num, _, exchange = raw.partition(".")
    if exchange == "BJ":
        return "bj"
    if num.startswith(("688", "689")):
        return "star"
    if num.startswith(("300", "301")):
        return "gem"
    if exchange in {"SH", "SZ"}:
        return "main"
    return "unknown"


def _normalize_boards(boards: Optional[list]) -> set[str]:
    """严格校验市场筛选；省略参数时覆盖当前支持的三个沪深市场。"""
    if boards is None:
        return set(_BOARD_LABELS)
    if not isinstance(boards, list) or not boards:
        from registry import ParamError
        raise ParamError("boards 必须是非空数组；省略该参数表示全部支持市场")
    normalized = [str(board or "").strip().lower() for board in boards]
    invalid = sorted({board for board in normalized if board not in _BOARD_LABELS})
    if invalid:
        from registry import ParamError
        allowed = ", ".join(_BOARD_LABELS)
        raise ParamError(
            f"boards 仅支持 {allowed}；北交所暂不纳入量化股票池；非法值：{', '.join(invalid)}")
    return set(normalized)


def run(industries: Optional[list[str]] = None,
        stock_names: Optional[list[str]] = None,
        weights: Optional[dict[str, float]] = None,
        top_n: int = 30,
        boards: Optional[list[str]] = None) -> dict[str, Any]:
    """量化选股主入口；市场范围与个股名称或行业条件叠加过滤。

    boards 支持沪深主板、科创板、创业板，多选取并集；北交所独立排除。
    仅读取质量合格的预计算 daily_factors，并在合成评分前完成市场过滤。
    """
    selected_boards = _normalize_boards(boards)
    effective_boards = [board for board in _BOARD_LABELS if board in selected_boards]

    base_weights = factor_config.effective_weights("stock")
    if weights is not None:
        if set(weights) != set(base_weights):
            return {"source": "screen/quant", "fetched_at": common.now_str(),
                    "boards": effective_boards, "candidates": [],
                    "error": "自定义权重必须包含契约中的全部因子（含权重0因子）",
                    "expected_factors": list(base_weights)}
        try:
            w = {name: float(weights[name]) for name in base_weights}
            if abs(sum(w.values()) - 1.0) > 0.01:
                raise ValueError("权重之和必须为1.0")
            contract = factor_contract.weighted_contract("stock", w, "request")
            contract["weight_version"] = f"request:{contract['weight_hash'][:12]}"
        except (TypeError, ValueError) as exc:
            return {"source": "screen/quant", "fetched_at": common.now_str(),
                    "boards": effective_boards, "candidates": [],
                    "error": f"自定义权重无效：{exc}"}
    else:
        w = base_weights
        contract = factor_config.model_contract("stock")

    sector_dependency = factor_config.model_contract("sector")
    data_dependencies = factor_contract.stock_data_dependencies(sector_dependency)
    dependency_hash = factor_contract.fingerprint(data_dependencies)
    contract["data_dependencies"] = data_dependencies
    contract["dependency_hash"] = dependency_hash

    end = db.latest_usable_factor_date(
        contract["factor_version"], contract["schema_hash"], dependency_hash)
    if not end:
        return {"source": "screen/quant", "fetched_at": common.now_str(),
                "boards": effective_boards, "candidates": [],
                "note": "没有与当前完整因子契约一致的成功预计算数据"}

    from screen_trend import _industry_members, _norm_terms
    name_terms = _norm_terms(stock_names)
    industry_terms = _norm_terms(industries)
    code_filter: Optional[set[str]] = None
    filter_type = "market"
    filter_terms: list[str] = []
    if name_terms:
        filter_type = "stock_names"
        filter_terms = name_terms
        code_filter = _stock_name_members(common.get_pro(), name_terms)
    elif industry_terms:
        filter_type = "industries"
        filter_terms = industry_terms
        code_filter = set(_industry_members(common.get_pro(), industry_terms))

    if code_filter is not None and not code_filter:
        label = "个股名称" if filter_type == "stock_names" else "行业/主线/概念"
        return {
            "source": "screen/quant", "fetched_at": common.now_str(), "trade_date": end,
            "filter_type": filter_type, "filter_terms": filter_terms,
            "boards": effective_boards, "candidates": [],
            "note": f"未找到匹配的{label}，请检查名称或缩短关键词",
        }

    data_source = "precomputed"
    tbl = _table_from_db(end, code_filter, contract, dependency_hash)
    if tbl is None or tbl.empty:
        return {
            "source": "screen/quant", "fetched_at": common.now_str(),
            "trade_date": end, "filter_type": filter_type, "filter_terms": filter_terms,
            "boards": effective_boards, "factor_contract": contract, "candidates": [],
            "error": "没有与当前因子公式版本和结构哈希完全一致的合格预计算数据",
            "note": "为防止缺失行业因子或旧版成分被静默补0，量化筛选不再用不完整实时数据降级；请先补算。",
        }

    tbl = tbl[tbl["code"].map(_board_of).isin(selected_boards)].copy()
    if tbl.empty:
        return {
            "source": "screen/quant", "fetched_at": common.now_str(),
            "trade_date": end, "filter_type": filter_type, "filter_terms": filter_terms,
            "boards": effective_boards, "factor_contract": contract, "candidates": [],
            "note": "当前个股/行业与市场范围的交集没有合格候选",
        }

    try:
        tbl = factors.composite_score(tbl, w, strict=True)
    except ValueError as exc:
        return {"source": "screen/quant", "fetched_at": common.now_str(),
                "trade_date": end, "boards": effective_boards,
                "factor_contract": contract, "candidates": [], "error": str(exc)}
    tbl = tbl.sort_values("score", ascending=False)

    active = contract["active_components"]
    cols = (["code", "price", "industry_name", "industry_score"] + active
            + ["score", "score_percentile"])
    cols = [column for column in cols if column in tbl.columns]
    out = tbl[cols].head(max(1, min(int(top_n), 200))).round(6).to_dict(orient="records")
    run_id = uuid.uuid4().hex
    try:
        names = common.stock_names_map()
        for rank, row in enumerate(out, start=1):
            row["name"] = names.get(row.get("code"), "")
            row["rank"] = rank
            row["score_raw"] = row["score"]
            row["screening_run_id"] = run_id
            row["factor_version"] = contract["factor_version"]
            row["schema_hash"] = contract["schema_hash"]
            row["weight_version"] = contract["weight_version"]
    except Exception:
        for rank, row in enumerate(out, start=1):
            row.update({"rank": rank, "score_raw": row["score"],
                        "screening_run_id": run_id,
                        "factor_version": contract["factor_version"],
                        "schema_hash": contract["schema_hash"],
                        "weight_version": contract["weight_version"]})
    quote_quality = _attach_quotes(out)
    db.save_screening_snapshot(factor_contract.base_contract("stock"), {
        "run_id": run_id, "function_name": "screen_quant", "trade_date": end,
        "factor_version": contract["factor_version"], "schema_hash": contract["schema_hash"],
        "weight_version": contract["weight_version"], "contract": contract,
        "candidate_codes": [row["code"] for row in out],
        "candidates": out,
        "params": {"filter_type": filter_type, "filter_terms": filter_terms,
                   "boards": effective_boards, "top_n": int(top_n),
                   "custom_weights": weights is not None},
    })
    return {
        "source": "screen/quant",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": end,
        "data_source": data_source,
        "filter_type": filter_type,
        "filter_terms": filter_terms,
        "boards": effective_boards,
        "screening_run_id": run_id,
        "factor_contract": contract,
        "weights": w,
        "quote_quality": quote_quality,
        "factor_note": "响应展示启用因子，但契约和落库快照始终保存全部因子，包括当前权重为0的候选因子。",
        "candidates": out,
        "note": "score_raw 为横截面标准化原始分；score_percentile 为0~1分位，跨样本分桶统一使用分位。",
    }


@register("screen_quant", "screening",
          "量化多因子选股；支持按市场、个股名称或行业/主线/概念限定范围，"
          "个股名称非空时优先；候选须按涨价、逻辑、预期、情绪四维复核",
          params=[{"name": "industries", "type": "array", "required": False,
                   "desc": "限定行业/主线/概念名；正式行业分类优先，无命中时回退概念；多词条取交集"},
                  {"name": "stock_names", "type": "array", "required": False,
                   "desc": "限定个股名称；支持逗号分隔，多个名称取并集；非空时优先于 industries"},
                  {"name": "boards", "type": "array", "required": False,
                   "desc": "市场范围：main沪深主板、star科创板、gem创业板；北交所暂不纳入量化股票池"},
                  {"name": "weights", "type": "object", "required": False, "desc": "自定义因子权重"},
                  {"name": "top_n", "type": "int", "required": False, "default": 30}],
          returns="boards / candidates（含各因子值与合成 score）")
def screen_quant(p: dict) -> dict:
    return run(
        industries=p.get("industries"),
        stock_names=p.get("stock_names"),
        weights=p.get("weights"),
        top_n=p.get("top_n", 30),
        boards=p.get("boards"),
    )


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    inds = args[0].split(",") if args else None
    print(json.dumps(run(inds), ensure_ascii=False, indent=2))

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
from registry import register


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
    return [{"code": r[code_col], "name": r[name_col]} for r in df.to_dict(orient="records")]


def _sector_history(pro, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """获取板块指数日线历史（申万）。"""
    try:
        df = pro.sw_daily(ts_code=code, start_date=start, end_date=end)
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        df = pro.index_daily(ts_code=code, start_date=start, end_date=end)
        return df
    except Exception:
        return None


def compute_sector_scores(pro, end: str,
                          weights: Optional[dict[str, float]] = None) -> list[dict[str, Any]]:
    """计算某交易日全部申万一级行业评分，并给出 0~1 横截面分位。"""
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
    w = factor_config.effective_weights("sector")
    if weights is not None:
        if set(weights) != set(w):
            raise ValueError("自定义行业权重必须包含契约中的全部因子")
        w = {name: float(weights[name]) for name in w}
        if abs(sum(w.values()) - 1.0) > 0.01:
            raise ValueError("自定义行业权重之和必须为1.0")
    rows: list[dict[str, Any]] = []
    for ind in _sw_l1_industries(pro):
        hist = _sector_history(pro, ind["code"], start, end)
        fac = factors.compute_sector_factors(hist) if hist is not None else None
        if fac is None:
            continue
        fac["code"] = ind["code"]
        fac["name"] = ind["name"]
        rows.append(fac)
    if not rows:
        return []
    tbl = factors.composite_score(pd.DataFrame(rows), w, strict=True)
    tbl["percentile"] = tbl["score_percentile"]
    cols = ["code", "name", "sec_mom_12_1", "sec_mom_20d", "sec_mom_5d",
            "sec_vol_confirm", "sec_low_vol", "last_close", "score", "percentile"]
    return tbl[[c for c in cols if c in tbl.columns]].sort_values(
        "score", ascending=False).round(6).to_dict(orient="records")


def run(weights: Optional[dict[str, float]] = None, top_n: int = 10,
        with_stocks: bool = False, stocks_per_sector: int = 5) -> dict[str, Any]:
    """行业轮动评分入口：优先读取最近完成交易日的合格持久化结果，未命中才实时计算。"""
    end: Optional[str] = None
    w = factor_config.effective_weights("sector")
    if weights is not None:
        if set(weights) != set(w):
            return {"source": "screen/sector", "fetched_at": common.now_str(),
                    "trade_date": end, "sectors": [],
                    "error": "自定义行业权重必须包含契约中的全部因子"}
        try:
            w = {name: float(weights[name]) for name in w}
            if abs(sum(w.values()) - 1.0) > 0.01:
                raise ValueError("权重之和必须为1.0")
        except (TypeError, ValueError) as exc:
            return {"source": "screen/sector", "fetched_at": common.now_str(),
                    "trade_date": end, "sectors": [], "error": str(exc)}
    contract = factor_contract.weighted_contract(
        "sector", w, factor_config.current_weight_version("sector") or "default")
    if weights is not None:
        contract["weight_version"] = f"request:{contract['weight_hash'][:12]}"

    dependency_summary = factor_contract.stock_data_dependencies(contract)
    dependency_hash = factor_contract.fingerprint(dependency_summary)
    contract["data_dependencies"] = dependency_summary
    contract["dependency_hash"] = dependency_hash
    if weights is None:
        stock_contract = factor_contract.base_contract("stock")
        end = db.latest_usable_factor_date(
            stock_contract["factor_version"], stock_contract["schema_hash"], dependency_hash)
    if not end:
        try:
            end = common.last_completed_trade_date()
        except Exception:
            end = db.latest_sector_score_date()
    if not end:
        return {"source": "screen/sector", "fetched_at": common.now_str(),
                "trade_date": None, "data_source": "unavailable", "sectors": [],
                "note": "无法确定最近已完成交易日且没有合格持久化行业评分"}

    data_source = "persisted"
    persisted = [] if weights else db.fetch_usable_daily_sector_scores(
        end, contract["factor_version"], contract["schema_hash"], dependency_hash)
    if persisted:
        sectors: list[dict[str, Any]] = []
        for row in persisted:
            item = {"code": row["code"], "name": row.get("name", ""),
                    "score": row["score"], "percentile": row["percentile"]}
            item.update(row.get("factors") or {})
            sectors.append(item)
    else:
        data_source = "realtime"
        try:
            pro = common.get_pro()
            sectors = compute_sector_scores(pro, end, weights)
        except Exception as exc:
            return {"source": "screen/sector", "fetched_at": common.now_str(),
                    "trade_date": end, "data_source": data_source, "sectors": [],
                    "note": f"行业评分实时计算失败：{type(exc).__name__}: {exc}"[:500]}

    if not sectors:
        return {"source": "screen/sector", "fetched_at": common.now_str(),
                "trade_date": end, "data_source": data_source, "sectors": [],
                "note": "无法获取行业指数或行业历史数据不足（检查 index_classify/sw_daily 权限）"}

    top_sectors = sectors[:max(1, min(int(top_n), 100))]
    run_id = uuid.uuid4().hex
    db.save_factor_contract(factor_contract.base_contract("sector"))
    db.save_screening_run({
        "run_id": run_id, "function_name": "screen_sector", "trade_date": end,
        "factor_version": contract["factor_version"], "schema_hash": contract["schema_hash"],
        "weight_version": contract["weight_version"], "contract": contract,
        "candidate_codes": [row["code"] for row in top_sectors],
        "candidates": top_sectors,
        "params": {"top_n": int(top_n), "custom_weights": weights is not None},
    })
    result: dict[str, Any] = {
        "source": "screen/sector",
        "fetched_at": common.now_str(),
        "trade_date": end,
        "data_source": data_source,
        "screening_run_id": run_id,
        "factor_contract": contract,
        "weights": w,
        "factor_note": "行业层面动量为正：12-1/20日/5日动量 + 量能确认 + 低波动；percentile 为行业横截面强度分位",
        "sectors": top_sectors,
        "note": "行业轮动量化排名，仍须由 Agent 叠加涨价、景气周期与事件催化交叉验证",
    }

    if with_stocks:
        import quant_screen
        picks = {}
        for sec in top_sectors[:3]:
            sub = quant_screen.run([sec["name"]], top_n=stocks_per_sector)
            picks[sec["name"]] = sub.get("candidates", [])
        result["stock_picks_by_sector"] = picks

    return result


@register("screen_sector", "screening",
          "选板块/板块轮动量化（板块动量为正：12-1/20日/5日动量+量能+低波动）",
          params=[{"name": "weights", "type": "object", "required": False},
                  {"name": "top_n", "type": "int", "required": False, "default": 10},
                  {"name": "with_stocks", "type": "bool", "required": False, "default": False,
                   "desc": "true 则在前3板块内选个股"},
                  {"name": "stocks_per_sector", "type": "int", "required": False, "default": 5}],
          returns="sectors 排名（+可选 stock_picks_by_sector）")
def screen_sector(p: dict) -> dict:
    return run(p.get("weights"), p.get("top_n", 10),
               p.get("with_stocks", False), p.get("stocks_per_sector", 5))


if __name__ == "__main__":
    import json
    print(json.dumps(run(with_stocks=False), ensure_ascii=False, indent=2))

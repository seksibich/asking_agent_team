"""趋势 + 行业逻辑选股。

在指定行业/主线内筛选处于趋势中的个股（均线多头、量价配合、非高位），
并用 factors.py 因子库的趋势侧权重对候选排序，与量化选股口径一致。
排除 ST。候选须 Agent 按四维（涨价>逻辑>预期>情绪）复核。
"""
from __future__ import annotations

import re
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


def _norm_terms(industries) -> list[str]:
    """规范化行业/主线/概念词条：兼容中英文逗号切分、去首尾空格、去空项、大小写不敏感去重。

    接受字符串或列表；列表元素也可含逗号。匹配时统一用大小写不敏感的子串匹配，
    故此处保留原始大小写，仅用小写做去重键。
    """
    if industries is None:
        return []
    raw = industries if isinstance(industries, list) else [industries]
    terms: list[str] = []
    seen: set[str] = set()
    for item in raw:
        for part in re.split(r"[，,]", str(item)):
            t = part.strip()
            if not t:
                continue
            key = t.lower()
            if key not in seen:
                seen.add(key)
                terms.append(t)
    return terms


def _members_col(df: pd.DataFrame) -> Optional[str]:
    """成分股结果里代表成分股代码的列名（不同接口列名不一）。"""
    for c in ("con_code", "ts_code", "code"):
        if c in df.columns:
            return c
    return None


def _sw_industry_codes(pro, term: str) -> list[str]:
    """申万 L1/L2/L3 行业中名称含 term 的行业 → 成分股代码。

    覆盖“通信设备/PCB/电子布”等一到三级行业分级（src=SW2021）。
    """
    codes: list[str] = []
    for lv in ("L3", "L2", "L1"):
        try:
            cls = common.cached_call(f"sw_classify_{lv}", {"lv": lv, "d": common.today_str()},
                                     lambda lv=lv: pro.index_classify(level=lv, src="SW2021"))
            cdf = pd.DataFrame(cls.get("rows", []))
        except Exception:
            continue
        if cdf.empty:
            continue
        namecol = "industry_name" if "industry_name" in cdf.columns else ("name" if "name" in cdf.columns else None)
        codecol = "index_code" if "index_code" in cdf.columns else ("ts_code" if "ts_code" in cdf.columns else None)
        if not namecol or not codecol:
            continue
        hit = cdf[cdf[namecol].astype(str).str.contains(term, case=False, regex=False, na=False)]
        for icode in hit[codecol].astype(str).tolist()[:12]:
            try:
                m = common.cached_call("sw_member", {"c": icode, "d": common.today_str()},
                                       lambda icode=icode: pro.index_member(index_code=icode))
                mdf = pd.DataFrame(m.get("rows", []))
            except Exception:
                continue
            col = _members_col(mdf)
            if not mdf.empty and col:
                # index_member 默认包含历史调入调出记录，只保留当前有效成分；
                # 否则已调出多年的旧成员也会被误判为当前行业股票。
                if "is_new" in mdf.columns:
                    mdf = mdf[mdf["is_new"].astype(str).str.upper().eq("Y")]
                elif "out_date" in mdf.columns:
                    out_date = mdf["out_date"].fillna("").astype(str).str.strip()
                    mdf = mdf[(out_date == "") | (out_date >= common.today_str())]
                codes.extend(mdf[col].astype(str).tolist())
    return codes


def _ths_concept_codes(pro, term: str) -> list[str]:
    """同花顺概念/行业指数中名称含 term 的板块 → 成分股代码。

    覆盖“机器人/PCB铜箔/固态电池”等主题、产业链概念（比 stock_basic 行业更细）。
    """
    codes: list[str] = []
    try:
        idx = common.cached_call("ths_index_all", {"d": common.today_str()}, lambda: pro.ths_index())
        idf = pd.DataFrame(idx.get("rows", []))
    except Exception:
        return codes
    if idf.empty or "name" not in idf.columns or "ts_code" not in idf.columns:
        return codes
    if "type" in idf.columns:   # N=概念指数, I=行业指数
        idf = idf[idf["type"].astype(str).isin(["N", "I"])]
    hit = idf[idf["name"].astype(str).str.contains(term, case=False, regex=False, na=False)]
    for tcode in hit["ts_code"].astype(str).tolist()[:8]:
        try:
            m = common.cached_call("ths_member", {"c": tcode, "d": common.today_str()},
                                   lambda tcode=tcode: pro.ths_member(ts_code=tcode))
            mdf = pd.DataFrame(m.get("rows", []))
        except Exception:
            continue
        col = _members_col(mdf)
        if not mdf.empty and col:
            codes.extend(mdf[col].astype(str).tolist())
    return codes


def _dc_concept_codes(pro, term: str) -> list[str]:
    """东财板块中名称含 term 的板块 → 成分股代码（补充热点概念）。"""
    codes: list[str] = []
    td = common.last_data_ready_date()
    try:
        idx = common.cached_call(
            "dc_index_day", {"td": td}, lambda: pro.dc_index(trade_date=td),
            historical=True, data_status="final", trade_date=td, expected_end=td)
        idf = pd.DataFrame(idx.get("rows", []))
    except Exception:
        return codes
    if idf.empty or "name" not in idf.columns or "ts_code" not in idf.columns:
        return codes
    hit = idf[idf["name"].astype(str).str.contains(term, case=False, regex=False, na=False)]
    for bcode in hit["ts_code"].astype(str).tolist()[:6]:
        try:
            m = common.cached_call("dc_member", {"c": bcode, "td": td},
                                   lambda bcode=bcode: pro.dc_member(ts_code=bcode, trade_date=td))
            mdf = pd.DataFrame(m.get("rows", []))
        except Exception:
            continue
        col = _members_col(mdf)
        if not mdf.empty and col:
            codes.extend(mdf[col].astype(str).tolist())
    return codes


def _term_members(pro, term: str, bdf: pd.DataFrame) -> set[str]:
    """获取单个行业/主题词条的成分股。

    优先采用正式行业分类（stock_basic 行业 + 申万 L1-L3）。正式分类有命中时，
    不再混入同花顺/东财概念成员，避免宽泛概念或错误概念归属污染行业结果；仅当
    正式分类完全无命中时，才回退概念源，以保留“机器人”“PCB铜箔”等主题检索能力。
    """
    classified: set[str] = set()
    if not bdf.empty and "industry" in bdf.columns:
        sel = bdf[bdf["industry"].astype(str).str.contains(term, case=False, regex=False, na=False)]
        classified.update(sel["ts_code"].astype(str).tolist())
    classified.update(_sw_industry_codes(pro, term))
    if classified:
        return classified

    concepts = set(_ths_concept_codes(pro, term))
    concepts.update(_dc_concept_codes(pro, term))
    return concepts


def _industry_members(pro, industries: list[str]) -> list[str]:
    """获取多个行业/主线/概念词条的成分股，**多词条取交集**（层层收窄），剔除 ST。

    - 单个词条内：跨数据源（stock_basic 行业 / 申万 L1-L3 / 同花顺·东财概念）取并集，
      尽量把该词条的成分股找全。
    - 多个词条间：取**交集** —— 只保留同时属于全部词条的个股。
      例如 ["通信","PCB","铜箔"] 返回同时属于通信、PCB、铜箔三者的股票（产业链下钻定位），
      而非并集的“任一命中”。
    - 任一词条在所有数据源都无命中 → 交集为空 → 返回空（提示词条过细/拼写有误）。
    """
    terms = _norm_terms(industries)
    if not terms:
        return []
    try:
        basic = common.cached_call("stock_basic_ind", {"d": common.today_str()},
                                   lambda: pro.stock_basic(exchange="", list_status="L",
                                                           fields="ts_code,name,industry"))
        bdf = pd.DataFrame(basic.get("rows", []))
    except Exception:
        bdf = pd.DataFrame()

    term_sets = [_term_members(pro, term, bdf) for term in terms]
    result: set[str] = set.intersection(*term_sets) if term_sets else set()
    # 统一剔除 ST/退（用 stock_basic 名称映射）
    if not bdf.empty and "name" in bdf.columns and "ts_code" in bdf.columns:
        st = set(bdf[bdf["name"].astype(str).str.contains("ST|退", na=False)]["ts_code"].astype(str))
        result -= st
    return sorted(result)


def _passes_trend_filter(fac: dict[str, Any]) -> bool:
    """趋势硬过滤：多头排列（trend_ma>=2 表示 price>ma20>ma60）。"""
    return fac.get("trend_ma", 0) >= 2.0


def run(industries: Optional[list[str]] = None, top_n: int = 30) -> dict[str, Any]:
    """趋势选股主入口。"""
    pro = common.get_pro()
    end: Optional[str] = None
    if not industries:
        return {
            "source": "screen/trend",
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "未指定行业/主线。建议先用 screen_sector 或 price_hike_scan 定主线再选股",
            "candidates": [],
        }
    ind_codes = _industry_members(pro, industries)
    rows: list[dict[str, Any]] = []
    data_source = "precomputed"

    stock_contract = factor_contract.base_contract("stock")
    trend_contract = factor_config.model_contract("trend")
    sector_dependency = factor_config.model_contract("sector")
    data_dependencies = factor_contract.stock_data_dependencies(sector_dependency)
    dependency_hash = factor_contract.fingerprint(data_dependencies)
    trend_contract["source_factor_contract"] = stock_contract
    trend_contract["data_dependencies"] = data_dependencies
    trend_contract["dependency_hash"] = dependency_hash
    end = db.latest_usable_factor_date(
        stock_contract["factor_version"], stock_contract["schema_hash"], dependency_hash)
    if not end:
        return {"source": "screen/trend", "fetched_at": common.now_str(),
                "industries": industries, "trade_date": None,
                "factor_contract": trend_contract, "candidates": [],
                "error": "没有与当前完整因子契约一致的成功预计算数据"}
    # 仅使用质量合格、公式版本与结构哈希均一致的完整预计算因子。
    db_hit = False
    try:
        db_hit = db.has_usable_daily_factors(
            end, stock_contract["factor_version"], schema_hash=stock_contract["schema_hash"],
            dependency_hash=dependency_hash)
    except Exception:
        db_hit = False
    if db_hit:
        fac_map = {row["code"]: row for row in db.fetch_daily_factors(end)}
        for code in ind_codes:
            record = fac_map.get(code)
            if (not record or record.get("schema_hash") != stock_contract["schema_hash"]
                    or record.get("dependency_hash") != dependency_hash):
                continue
            fac = dict(record["factors"])
            valid, _ = factor_contract.validate_payload("stock", fac)
            if valid and _passes_trend_filter(fac):
                fac["code"] = code
                rows.append(fac)

    if not rows:
        return {"source": "screen/trend", "fetched_at": common.now_str(),
                "industries": industries, "trade_date": end,
                "factor_contract": trend_contract, "candidates": [],
                "error": "没有与当前因子契约一致的合格预计算数据",
                "note": "为防止缺失行业强度或旧版因子被静默补0，趋势筛选不再降级到不完整实时计算。"}

    tbl = factors.composite_score(
        pd.DataFrame(rows), factor_config.effective_weights("trend"), strict=True)
    tbl = tbl.sort_values("score", ascending=False)
    active = trend_contract["active_components"]
    cols = ["code", "price"] + active + ["score", "score_percentile"]
    cols = [column for column in cols if column in tbl.columns]
    candidates = tbl[cols].head(max(1, min(int(top_n), 200))).round(6).to_dict(orient="records")
    run_id = uuid.uuid4().hex
    try:
        names = common.stock_names_map()
    except Exception:
        names = {}
    for rank, row in enumerate(candidates, start=1):
        row.update({"name": names.get(row.get("code"), ""), "rank": rank,
                    "score_raw": row["score"], "screening_run_id": run_id,
                    "factor_version": trend_contract["factor_version"],
                    "schema_hash": trend_contract["schema_hash"],
                    "weight_version": trend_contract["weight_version"]})
    db.save_factor_contract(factor_contract.base_contract("trend"))
    db.save_screening_run({
        "run_id": run_id, "function_name": "screen_trend", "trade_date": end,
        "factor_version": trend_contract["factor_version"],
        "schema_hash": trend_contract["schema_hash"],
        "weight_version": trend_contract["weight_version"], "contract": trend_contract,
        "candidate_codes": [row["code"] for row in candidates],
        "candidates": candidates,
        "params": {"industries": industries, "top_n": int(top_n)},
    })
    return {
        "source": "screen/trend",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industries": industries,
        "trade_date": end,
        "data_source": data_source,
        "screening_run_id": run_id,
        "factor_contract": trend_contract,
        "candidates": candidates,
        "note": "score_raw 为原始横截面分，score_percentile 为0~1分位；正式候选需继续四维复核。",
    }


@register("screen_trend", "screening",
          "趋势+行业逻辑选股（行业内多头排列过滤 + 趋势因子排序）",
          params=[{"name": "industries", "type": "array", "required": True,
                   "desc": "行业/主线名列表（先定主线再选股）"},
                  {"name": "top_n", "type": "int", "required": False, "default": 30}],
          returns="candidates（趋势因子排序）")
def screen_trend(p: dict) -> dict:
    return run(p.get("industries"), p.get("top_n", 30))


if __name__ == "__main__":
    import json
    args = sys.argv[1:]
    inds = args[0].split(",") if args else None
    print(json.dumps(run(inds), ensure_ascii=False, indent=2))

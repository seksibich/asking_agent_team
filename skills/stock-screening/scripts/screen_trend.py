"""趋势 + 行业逻辑选股。

在指定行业/主线内筛选处于趋势中的个股（均线多头、量价配合、非高位），
并用 factors.py 因子库的趋势侧权重对候选排序，与量化选股口径一致。
排除 ST。候选须 Agent 按四维（涨价>逻辑>预期>情绪）复核。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import db
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
    td = common.last_trade_date()
    try:
        idx = common.cached_call("dc_index_day", {"td": td}, lambda: pro.dc_index(trade_date=td))
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
    """单个词条的成分股集合：在各数据源内取**并集**（覆盖粗→细的分级）。

      1) stock_basic.industry 模糊匹配（tushare 行业字段，较粗，如“通信设备”）
      2) 申万 L1/L2/L3 行业（index_classify + index_member；覆盖“PCB/电子布”等三级细分）
      3) 同花顺概念/行业指数（ths_index + ths_member；覆盖“机器人/PCB铜箔”等主题概念）
      4) 东财板块（dc_index + dc_member；补充热点概念）
    某源无权限/积分时自动跳过，不影响其它源；因此即便 stock_basic 无“机器人”行业，
    也能通过概念源命中该词条。
    """
    s: set[str] = set()
    if not bdf.empty and "industry" in bdf.columns:
        sel = bdf[bdf["industry"].astype(str).str.contains(term, case=False, regex=False, na=False)]
        s.update(sel["ts_code"].astype(str).tolist())
    s.update(_sw_industry_codes(pro, term))
    s.update(_ths_concept_codes(pro, term))
    s.update(_dc_concept_codes(pro, term))
    return s


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
    end = common.last_trade_date()
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

    # 优先读预计算因子（daily_factors）
    db_hit = False
    try:
        db_hit = db.has_daily_factors(end)
    except Exception:
        db_hit = False
    if db_hit:
        fac_map = {r["code"]: r["factors"] for r in db.fetch_daily_factors(end)}
        for code in ind_codes:
            fac = fac_map.get(code)
            if fac and _passes_trend_filter(fac):
                fac = dict(fac)
                fac["code"] = code
                rows.append(fac)

    # 回退实时逐只
    if not rows:
        data_source = "realtime"
        start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=420)).strftime("%Y%m%d")
        try:
            basics = pro.daily_basic(trade_date=end, fields="ts_code,turnover_rate")
            turnover_map = {r["ts_code"]: r.get("turnover_rate") for r in basics.to_dict(orient="records")}
        except Exception:
            turnover_map = {}
        for code in ind_codes[: max(top_n * 5, 120)]:
            try:
                df = pro.daily(ts_code=code, start_date=start, end_date=end)
            except Exception:
                continue
            fac = factors.compute_stock_factors(df, turnover_map.get(code))
            if fac is None or not _passes_trend_filter(fac):
                continue
            fac["code"] = code
            rows.append(fac)

    if not rows:
        return {"source": "screen/trend", "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "industries": industries, "candidates": [], "note": "无满足趋势过滤的候选"}

    tbl = factors.composite_score(pd.DataFrame(rows), factor_config.effective_weights("trend"))
    tbl = tbl.sort_values("score", ascending=False)
    cols = ["code", "price", "mom_12_1", "trend_ma", "high_52w", "vol_confirm", "score"]
    cols = [c for c in cols if c in tbl.columns]
    _cands = tbl[cols].head(top_n).round(4).to_dict(orient="records")
    try:
        nm = common.stock_names_map()
        for r in _cands:
            r["name"] = nm.get(r.get("code"), "")
    except Exception:
        pass
    return {
        "source": "screen/trend",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "industries": industries,
        "trade_date": end,
        "data_source": data_source,
        "candidates": _cands,
        "note": "趋势初筛候选，需 Agent 按 涨价>逻辑>预期>情绪 四维复核",
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

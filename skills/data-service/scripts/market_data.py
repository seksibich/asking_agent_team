"""行情/资金/基本面/宏观/新闻/板块 数据功能（tushare 封装）。

面向 15000 积分档位，登记较完整的接口集。每个功能用 @register 注册，
loader 自动发现，/functions 自动索引，data_version 自动变化。

返回统一为 {source, fetched_at, rows}（rows 为记录列表）。
日级数据走当日缓存；实时/新闻类不缓存。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

import common
from registry import ParamError, register

try:
    import tushare as ts
except ImportError:
    ts = None  # type: ignore

G_MKT = "market"
G_MONEY = "money"
G_FUND = "fundamental"
G_MACRO = "macro"
G_NEWS = "news"
G_OVS = "overseas"
G_HOT = "hot"
G_SEC = "sector"
G_META = "meta"

DEFAULT_INDEX = ["000001.SH", "399001.SZ", "399006.SZ"]


def _normalize_tokens(value: Any) -> list[str]:
    """兼容数组、逗号字符串与单值，返回去重后的非空字符串列表。"""
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = value.replace("，", ",").split(",")
    elif value is None:
        raw = []
    else:
        raw = [value]
    values: list[str] = []
    for item in raw:
        value_text = str(item).strip()
        if value_text and value_text not in values:
            values.append(value_text)
    return values


def _normalize_codes(value: Any) -> list[str]:
    """兼容代码数组、逗号字符串与空值，返回去重后的规范代码列表。"""
    codes = [value.upper() for value in _normalize_tokens(value)]
    return codes or list(DEFAULT_INDEX)


def _normalize_names(value: Any) -> list[str]:
    """规范化股票名称关键词，支持数组或逗号分隔字符串。"""
    return _normalize_tokens(value)


def _wrap(source: str, df: pd.DataFrame) -> dict[str, Any]:
    rows = df.to_dict(orient="records") if isinstance(df, pd.DataFrame) else (df or [])
    return {"source": source, "fetched_at": common.now_str(), "rows": rows}


def _cached(name: str, params: dict[str, Any], fetch, use_cache: bool = True,
            historical: bool = False) -> dict[str, Any]:
    return common.cached_call(name, params, fetch, use_cache=use_cache, historical=historical)


def _stock_basic_snapshot() -> dict[str, Any]:
    """读取当日上市股票基础信息快照，名称解析和过滤共用该缓存。"""
    pro = common.get_pro()
    return _cached("stock_basic", {"d": common.today_str()},
                   lambda: pro.stock_basic(list_status="L",
                                           fields="ts_code,name,industry,market,list_date"))


def _filter_stock_basic_rows(rows: list[dict[str, Any]],
                             codes: list[str], names: list[str]) -> list[dict[str, Any]]:
    """按代码精确匹配、按名称关键词包含匹配，两个条件取并集。"""
    if not codes and not names:
        return rows
    code_set = set(codes)
    name_queries = [name.casefold() for name in names]
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_code = str(row.get("ts_code", "")).strip().upper()
        row_name = str(row.get("name", "")).strip().casefold()
        code_match = bool(code_set and row_code in code_set)
        name_match = bool(name_queries and any(query in row_name for query in name_queries))
        if code_match or name_match:
            filtered.append(row)
    return filtered


# ================= market =================
@register("market_index", G_MKT,
          "大盘/指数日线（默认三大指数）：codes 兼容数组或逗号字符串；单日为空时回退最近10个自然日的最新记录，部分失败结构化返回",
          params=[{"name": "codes", "type": "array", "required": False,
                   "default": DEFAULT_INDEX, "desc": "指数代码数组或逗号分隔字符串"}],
          returns="rows / requested_codes / requested_date / actual_dates / missing_codes / degraded")
def market_index(p: dict) -> dict:
    pro = common.get_pro()
    requested_date = common.last_trade_date()
    codes = _normalize_codes(p.get("codes"))
    range_start = (datetime.strptime(requested_date, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")

    def fetch() -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for code in codes:
            frame = pd.DataFrame()
            try:
                frame = pro.index_daily(ts_code=code, trade_date=requested_date)
            except Exception:
                frame = pd.DataFrame()
            if frame is None or frame.empty:
                try:
                    frame = pro.index_daily(ts_code=code, start_date=range_start, end_date=requested_date)
                except Exception:
                    frame = pd.DataFrame()
            if frame is not None and not frame.empty:
                if "trade_date" in frame.columns:
                    frame = frame.sort_values("trade_date", ascending=False).head(1)
                frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    result = _cached("index_daily_resilient", {"codes": codes, "td": requested_date}, fetch)
    rows = result.get("rows") or []
    returned_codes = {str(row.get("ts_code", "")) for row in rows}
    missing_codes = [code for code in codes if code not in returned_codes]
    actual_dates = {str(row.get("ts_code", "")): str(row.get("trade_date", "")) for row in rows}
    used_fallback = any(date and date != requested_date for date in actual_dates.values())
    result.update({
        "requested_codes": codes,
        "requested_date": requested_date,
        "actual_dates": actual_dates,
        "missing_codes": missing_codes,
        "degraded": bool(missing_codes or used_fallback),
    })
    return result


@register("market_realtime", G_MKT,
          "批量实时行情快照（东财源），支持股票代码和股票名称关键词混合查询",
          params=[{"name": "codes", "type": "array", "required": False,
                   "desc": "股票代码数组或逗号分隔字符串"},
                  {"name": "names", "type": "array", "required": False,
                   "desc": "股票名称关键词数组或逗号分隔字符串，按名称包含匹配"},
                  {"name": "name", "type": "string", "required": False,
                   "desc": "单个股票名称关键词，兼容简化调用"}],
          returns="rows / requested_codes / requested_names / resolved / missing_codes / missing_names / degraded")
def market_realtime(p: dict) -> dict:
    if ts is None:
        raise RuntimeError("tushare not installed")
    requested_codes = _normalize_tokens(p.get("codes"))
    requested_names = _normalize_names(p.get("names") or p.get("name"))
    if not requested_codes and not requested_names:
        raise ParamError("至少提供 codes、names 或 name 之一")

    basic_rows: list[dict[str, Any]] = []
    if requested_names:
        basic = _stock_basic_snapshot()
        basic_rows = list(basic.get("rows") or [])
    name_rows: list[dict[str, Any]] = []
    if requested_names:
        name_rows = _filter_stock_basic_rows(basic_rows, [], requested_names)

    resolved_codes = list(requested_codes)
    resolved: list[dict[str, str]] = []
    basic_by_code = {str(row.get("ts_code", "")).strip().upper(): row for row in basic_rows}
    for code in requested_codes:
        row = basic_by_code.get(code, {})
        resolved.append({"code": code, "name": str(row.get("name", "")), "matched_by": "code"})
    for row in name_rows:
        code = str(row.get("ts_code", "")).strip().upper()
        if code and code not in resolved_codes:
            resolved_codes.append(code)
            resolved.append({"code": code, "name": str(row.get("name", "")), "matched_by": "name"})

    missing_names = [name for name in requested_names
                     if not any(name.casefold() in str(row.get("name", "")).casefold()
                                for row in name_rows)]
    if not resolved_codes:
        result = _wrap("realtime_quote", pd.DataFrame())
    else:
        df = ts.realtime_quote(ts_code=",".join(resolved_codes))
        result = _wrap("realtime_quote", df)

    returned_codes = {str(row.get("ts_code") or row.get("TS_CODE") or "").strip().upper()
                      for row in result.get("rows") or []}
    missing_codes = [code for code in resolved_codes if code not in returned_codes]
    result.update({
        "requested_codes": requested_codes,
        "requested_names": requested_names,
        "resolved": resolved,
        "missing_codes": missing_codes,
        "missing_names": missing_names,
        "degraded": bool(missing_codes or missing_names),
    })
    return result


@register("market_daily", G_MKT, "个股/指数日线（不复权）；个股接口空数据时自动回退指数日线",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True, "desc": "YYYYMMDD"},
                  {"name": "end", "type": "string", "required": True, "desc": "YYYYMMDD"}],
          returns="股票或指数日线 OHLC/pct_chg/vol/amount")
def market_daily(p: dict) -> dict:
    pro = common.get_pro()
    code = p["code"].strip().upper()
    start, end = p["start"], p["end"]

    def fetch() -> pd.DataFrame:
        try:
            frame = pro.daily(ts_code=code, start_date=start, end_date=end)
        except Exception:
            frame = pd.DataFrame()
        resolved_source = "daily"
        if frame is None or frame.empty:
            resolved_source = "index_daily"
            try:
                frame = pro.index_daily(ts_code=code, start_date=start, end_date=end)
            except Exception:
                frame = pd.DataFrame()
        frame = frame if frame is not None else pd.DataFrame()
        if not frame.empty:
            frame = frame.copy()
            frame["_resolved_source"] = resolved_source
        return frame

    result = _cached("daily_with_index_fallback_v2", {"c": code, "s": start, "e": end},
                     fetch, historical=True)
    rows = result.get("rows") or []
    sources = {str(row.pop("_resolved_source", "daily")) for row in rows}
    resolved_source = next(iter(sources)) if len(sources) == 1 else ("mixed" if sources else "none")
    result.update({"code": code, "resolved_source": resolved_source,
                   "degraded": resolved_source != "daily"})
    return result


@register("market_adj_daily", G_MKT, "个股前复权日线（回测/趋势用）",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}],
          returns="前复权日线")
def market_adj_daily(p: dict) -> dict:
    if ts is None:
        raise RuntimeError("tushare not installed")
    common.get_pro()
    df = ts.pro_bar(ts_code=p["code"], adj="qfq", start_date=p["start"], end_date=p["end"])
    return _wrap("pro_bar_qfq", df if df is not None else pd.DataFrame())


@register("market_weekly", G_MKT, "个股/指数周线",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}])
def market_weekly(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("weekly", {"c": p["code"], "s": p["start"], "e": p["end"]},
                   lambda: pro.weekly(ts_code=p["code"], start_date=p["start"], end_date=p["end"]),
                   historical=True)


@register("market_limit", G_MKT, "每日涨跌停/炸板统计（情绪面）",
          params=[{"name": "date", "type": "string", "required": False, "desc": "YYYYMMDD，默认最近交易日"}])
def market_limit(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("limit_list_d", {"td": td}, lambda: pro.limit_list_d(trade_date=td))


@register("market_lianban", G_MKT, "涨停最强/连板板块统计",
          params=[{"name": "date", "type": "string", "required": False}])
def market_lianban(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("limit_cpt_list", {"td": td}, lambda: pro.limit_cpt_list(trade_date=td))


@register("market_stk_limit", G_MKT, "每日个股涨跌停价",
          params=[{"name": "date", "type": "string", "required": False}])
def market_stk_limit(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("stk_limit", {"td": td}, lambda: pro.stk_limit(trade_date=td))


@register("market_index_dailybasic", G_MKT, "大盘每日指标（PE/PB/换手/总市值）",
          params=[{"name": "date", "type": "string", "required": False}])
def market_index_dailybasic(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("index_dailybasic", {"td": td}, lambda: pro.index_dailybasic(trade_date=td))


# ================= money =================
@register("money_flow", G_MONEY, "个股资金流向（东财）",
          params=[{"name": "code", "type": "string", "required": True}])
def money_flow(p: dict) -> dict:
    pro = common.get_pro()
    td = common.last_trade_date()
    return _cached("moneyflow_dc", {"c": p["code"], "td": td},
                   lambda: pro.moneyflow_dc(ts_code=p["code"], trade_date=td))


@register("money_flow_ind", G_MONEY, "行业板块资金流向（东财）",
          params=[{"name": "date", "type": "string", "required": False}])
def money_flow_ind(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("moneyflow_ind_dc", {"td": td}, lambda: pro.moneyflow_ind_dc(trade_date=td))


@register("money_hsgt", G_MONEY, "北向资金全天净流入",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hsgt(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("moneyflow_hsgt", {"td": td}, lambda: pro.moneyflow_hsgt(trade_date=td))


@register("money_hsgt_top10", G_MONEY, "沪深股通十大成交股",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hsgt_top10(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("hsgt_top10", {"td": td}, lambda: pro.hsgt_top10(trade_date=td))


@register("money_toplist", G_MONEY, "龙虎榜每日明细（上榜个股）",
          params=[{"name": "date", "type": "string", "required": False}])
def money_toplist(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("top_list", {"td": td}, lambda: pro.top_list(trade_date=td))


@register("money_topinst", G_MONEY, "龙虎榜机构/营业部席位明细",
          params=[{"name": "date", "type": "string", "required": False}])
def money_topinst(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("top_inst", {"td": td}, lambda: pro.top_inst(trade_date=td))


@register("money_hm_list", G_MONEY, "游资名录", params=[])
def money_hm_list(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("hm_list", {}, lambda: pro.hm_list())


@register("money_hm_detail", G_MONEY, "游资每日交易明细",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hm_detail(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("hm_detail", {"td": td}, lambda: pro.hm_detail(trade_date=td))


# ================= fundamental =================
@register("fundamental_daily_basic", G_FUND, "个股每日指标（PE/PB/换手/市值），PE仅作风险背景",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "date", "type": "string", "required": False}])
def fundamental_daily_basic(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("daily_basic", {"c": p["code"], "td": td},
                   lambda: pro.daily_basic(ts_code=p["code"], trade_date=td))


@register("fundamental_income", G_FUND, "利润表（已披露业绩，仅披露期作验证）",
          params=[{"name": "code", "type": "string", "required": True}])
def fundamental_income(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("income", {"c": p["code"]}, lambda: pro.income(ts_code=p["code"]))


@register("fundamental_forecast", G_FUND, "业绩预告（前瞻预期，预期驱动核心）",
          params=[{"name": "period", "type": "string", "required": False, "desc": "YYYYMMDD 报告期"},
                  {"name": "code", "type": "string", "required": False}])
def fundamental_forecast(p: dict) -> dict:
    pro = common.get_pro()
    kw: dict[str, Any] = {}
    if p.get("period"):
        kw["period"] = p["period"]
    if p.get("code"):
        kw["ts_code"] = p["code"]
    return _wrap("forecast", pro.forecast(**kw))


@register("fundamental_express", G_FUND, "业绩快报",
          params=[{"name": "period", "type": "string", "required": False},
                  {"name": "code", "type": "string", "required": False}])
def fundamental_express(p: dict) -> dict:
    pro = common.get_pro()
    kw: dict[str, Any] = {}
    if p.get("period"):
        kw["period"] = p["period"]
    if p.get("code"):
        kw["ts_code"] = p["code"]
    return _wrap("express", pro.express(**kw))


@register("fundamental_fina_indicator", G_FUND, "财务指标（ROE/增速等）",
          params=[{"name": "code", "type": "string", "required": True}])
def fundamental_fina_indicator(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("fina_indicator", {"c": p["code"]},
                   lambda: pro.fina_indicator(ts_code=p["code"]))


# ================= macro（涨价强相关）=================
@register("macro_ppi", G_MACRO, "PPI 工业品出厂价格指数（涨价链宏观锚）",
          params=[{"name": "start", "type": "string", "required": False, "desc": "YYYYMM"},
                  {"name": "end", "type": "string", "required": False}])
def macro_ppi(p: dict) -> dict:
    pro = common.get_pro()
    return _wrap("cn_ppi", pro.cn_ppi(start_m=p.get("start"), end_m=p.get("end")))


@register("macro_cpi", G_MACRO, "CPI 居民消费价格指数",
          params=[{"name": "start", "type": "string", "required": False},
                  {"name": "end", "type": "string", "required": False}])
def macro_cpi(p: dict) -> dict:
    pro = common.get_pro()
    return _wrap("cn_cpi", pro.cn_cpi(start_m=p.get("start"), end_m=p.get("end")))


@register("macro_pmi", G_MACRO, "PMI 采购经理指数",
          params=[{"name": "start", "type": "string", "required": False},
                  {"name": "end", "type": "string", "required": False}])
def macro_pmi(p: dict) -> dict:
    pro = common.get_pro()
    return _wrap("cn_pmi", pro.cn_pmi(start_m=p.get("start"), end_m=p.get("end")))


@register("macro_m", G_MACRO, "货币供应量 M0/M1/M2",
          params=[{"name": "start", "type": "string", "required": False},
                  {"name": "end", "type": "string", "required": False}])
def macro_m(p: dict) -> dict:
    pro = common.get_pro()
    return _wrap("cn_m", pro.cn_m(start_m=p.get("start"), end_m=p.get("end")))


# ================= news / 时事 =================
@register("news_flash", G_NEWS, "财经快讯（当日）",
          params=[{"name": "src", "type": "string", "required": False, "default": "sina",
                   "desc": "sina/10jqka/wallstreetcn 等"}])
def news_flash(p: dict) -> dict:
    pro = common.get_pro()
    day = common.today_str()
    s = f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00"
    e = f"{day[:4]}-{day[4:6]}-{day[6:]} 23:59:59"
    return _wrap("news", pro.news(src=p["src"], start_date=s, end_date=e))


@register("news_filter", G_NEWS, "关键词过滤新闻（时事/行业/涨价事件）",
          params=[{"name": "keyword", "type": "string", "required": True},
                  {"name": "src", "type": "string", "required": False, "default": "sina"}])
def news_filter(p: dict) -> dict:
    pro = common.get_pro()
    day = common.today_str()
    s = f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00"
    e = f"{day[:4]}-{day[4:6]}-{day[6:]} 23:59:59"
    df = pro.news(src=p["src"], start_date=s, end_date=e)
    if not df.empty and "content" in df.columns:
        df = df[df["content"].astype(str).str.contains(p["keyword"], na=False)]
    return _wrap("news_filter", df)


@register("news_anns", G_NEWS, "上市公司公告（当日）",
          params=[{"name": "date", "type": "string", "required": False}])
def news_anns(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _wrap("anns_d", pro.anns_d(trade_date=td))


@register("news_cctv", G_NEWS, "新闻联播文字稿（时政风向）",
          params=[{"name": "date", "type": "string", "required": False}])
def news_cctv(p: dict) -> dict:
    pro = common.get_pro()
    d = p.get("date") or common.today_str()
    return _wrap("cctv_news", pro.cctv_news(date=d))


# ================= overseas 外盘 =================
@register("overseas_us", G_OVS, "美股日线（隔夜外盘）",
          params=[{"name": "codes", "type": "string", "required": True, "desc": "如 AAPL,MSFT 或指数"}])
def overseas_us(p: dict) -> dict:
    pro = common.get_pro()
    frames = []
    for c in p["codes"].split(","):
        try:
            frames.append(pro.us_daily(ts_code=c.strip()).head(5))
        except Exception:
            continue
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return _wrap("us_daily", df)


@register("overseas_hk", G_OVS, "港股日线",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": False},
                  {"name": "end", "type": "string", "required": False}])
def overseas_hk(p: dict) -> dict:
    pro = common.get_pro()
    return _wrap("hk_daily", pro.hk_daily(ts_code=p["code"], start_date=p.get("start"),
                                          end_date=p.get("end")))


# ================= hot 热度 =================
@register("hot_dc", G_HOT, "东方财富热榜", params=[])
def hot_dc(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("dc_hot", {"td": common.today_str()}, lambda: pro.dc_hot(), use_cache=False)


@register("hot_ths", G_HOT, "同花顺热榜", params=[])
def hot_ths(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("ths_hot", {"td": common.today_str()}, lambda: pro.ths_hot(), use_cache=False)


@register("hot_kpl_list", G_HOT, "涨停原因分类（开盘啦，题材归属）",
          params=[{"name": "date", "type": "string", "required": False}])
def hot_kpl_list(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("kpl_list", {"td": td}, lambda: pro.kpl_list(trade_date=td))


@register("hot_kpl_concept", G_HOT, "题材/概念强度排名（开盘啦）",
          params=[{"name": "date", "type": "string", "required": False}])
def hot_kpl_concept(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("kpl_concept", {"td": td}, lambda: pro.kpl_concept(trade_date=td))


# ================= sector 板块/行业 =================
@register("sector_dc", G_SEC, "板块行情排名（东财）",
          params=[{"name": "date", "type": "string", "required": False}])
def sector_dc(p: dict) -> dict:
    pro = common.get_pro()
    td = p.get("date") or common.last_trade_date()
    return _cached("dc_index", {"td": td}, lambda: pro.dc_index(trade_date=td))


@register("sector_index_classify", G_SEC, "行业分类清单（申万等）",
          params=[{"name": "level", "type": "string", "required": False, "default": "L1"},
                  {"name": "src", "type": "string", "required": False, "default": "SW2021"}])
def sector_index_classify(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("index_classify", {"lv": p["level"], "src": p["src"]},
                   lambda: pro.index_classify(level=p["level"], src=p["src"]))


@register("sector_sw_daily", G_SEC, "申万行业指数日线（板块动量用）",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}])
def sector_sw_daily(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("sw_daily", {"c": p["code"], "s": p["start"], "e": p["end"]},
                   lambda: pro.sw_daily(ts_code=p["code"], start_date=p["start"], end_date=p["end"]),
                   historical=True)


@register("sector_ths_daily", G_SEC, "同花顺板块/概念指数日线",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}])
def sector_ths_daily(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("ths_daily", {"c": p["code"], "s": p["start"], "e": p["end"]},
                   lambda: pro.ths_daily(ts_code=p["code"], start_date=p["start"], end_date=p["end"]),
                   historical=True)


# ================= meta 基础 =================
@register("meta_stock_basic", G_META,
          "股票基础信息，支持代码精确过滤和名称关键词包含匹配",
          params=[{"name": "codes", "type": "array", "required": False,
                   "desc": "股票代码数组或逗号分隔字符串"},
                  {"name": "names", "type": "array", "required": False,
                   "desc": "股票名称关键词数组或逗号分隔字符串"},
                  {"name": "name", "type": "string", "required": False,
                   "desc": "单个股票名称关键词，兼容简化调用"}],
          returns="rows / requested_codes / requested_names / matched_codes / missing_codes / missing_names")
def meta_stock_basic(p: dict) -> dict:
    requested_codes = _normalize_tokens(p.get("codes"))
    requested_names = _normalize_names(p.get("names") or p.get("name"))
    result = _stock_basic_snapshot()
    all_rows = list(result.get("rows") or [])
    rows = _filter_stock_basic_rows(all_rows, requested_codes, requested_names)
    matched_codes = [str(row.get("ts_code", "")).strip().upper() for row in rows]
    missing_codes = [code for code in requested_codes if code not in matched_codes]
    missing_names = [name for name in requested_names
                     if not any(name.casefold() in str(row.get("name", "")).casefold()
                                for row in rows)]
    result.update({
        "rows": rows,
        "requested_codes": requested_codes,
        "requested_names": requested_names,
        "matched_codes": matched_codes,
        "missing_codes": missing_codes,
        "missing_names": missing_names,
        "filtered": bool(requested_codes or requested_names),
    })
    return result


@register("meta_trade_cal", G_META, "交易日历",
          params=[{"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}])
def meta_trade_cal(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("trade_cal", {"s": p["start"], "e": p["end"]},
                   lambda: pro.trade_cal(exchange="SSE", start_date=p["start"], end_date=p["end"]))

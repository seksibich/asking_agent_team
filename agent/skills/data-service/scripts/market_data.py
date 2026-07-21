"""行情/资金/基本面/宏观/新闻/板块 数据功能（tushare 封装）。

面向 15000 积分档位，登记较完整的接口集。每个功能用 @register 注册，
loader 自动发现，/functions 自动索引，data_version 自动变化。

返回统一为 {source, fetched_at, rows}（rows 为记录列表）。
日级数据走当日缓存；实时/新闻类不缓存。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import re
import threading
import time
from typing import Any, Callable, Optional

import pandas as pd
import requests

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
REALTIME_COLUMNS = [
    "TS_CODE", "NAME", "PRICE", "PCT_CHANGE", "CLOSE", "OPEN", "HIGH", "LOW",
    "VOLUME", "AMOUNT", "TIME", "DATE", "quote_source",
]
_REALTIME_CACHE_TTL = 30.0
_REALTIME_MIN_STOCKS = 4500
_REALTIME_MIN_COVERAGE = 0.90
_REALTIME_MIN_PCT_COVERAGE = 0.90
_REALTIME_CACHE: dict[str, dict[str, Any]] = {}
_REALTIME_CACHE_LOCK = threading.Lock()
_REALTIME_FETCH_LOCK = threading.Lock()


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
            historical: bool = False, data_status: str = "auto",
            trade_date: Optional[str] = None,
            expected_end: Optional[str] = None) -> dict[str, Any]:
    """转发公共缓存参数，保持各接口调用简洁。"""
    return common.cached_call(
        name, params, fetch, use_cache=use_cache, historical=historical,
        data_status=data_status, trade_date=trade_date, expected_end=expected_end,
    )


def _last_data_ready_date() -> str:
    """读取公共模块的数据就绪日，并兼容旧版公共模块函数名。"""
    resolver = getattr(common, "last_data_ready_date", None)
    if not callable(resolver):
        resolver = getattr(common, "last_completed_trade_date", None)
    if not callable(resolver):
        raise RuntimeError("公共模块缺少数据就绪日函数")
    ready = str(resolver()).strip().replace("-", "")
    try:
        datetime.strptime(ready, "%Y%m%d")
    except ValueError as exc:
        raise RuntimeError(f"数据就绪日无效：{ready or '空'}") from exc
    return ready


def _resolve_eod_date(p: dict[str, Any]) -> tuple[str, str, Optional[str]]:
    """把日终接口请求日期解析为安全有效日，不静默使用未就绪的当天数据。"""
    ready = _last_data_ready_date()
    requested = str(p.get("date") or ready).strip().replace("-", "")
    try:
        datetime.strptime(requested, "%Y%m%d")
    except ValueError as exc:
        raise ParamError("date 必须是有效 YYYYMMDD") from exc
    if requested > ready:
        reason = f"请求日 {requested} 尚未达到日终安全就绪线，已回退 {ready}"
        return requested, ready, reason
    return requested, requested, None


def _decorate_eod(result: dict[str, Any], requested: str, effective: str,
                  fallback_reason: Optional[str] = None) -> dict[str, Any]:
    """补充日终日期状态；显式回退必须向调用方完整披露。"""
    actual_date = result.get("effective_date")
    resolved_status = result.get("data_status")
    if fallback_reason:
        resolved_status = "fallback_final" if result.get("is_final") else "fallback_incomplete"
    result.update({
        "requested_date": requested,
        "effective_date": effective,
        "actual_data_date": actual_date,
        "data_status": resolved_status,
        "is_final": bool(result.get("is_final")),
        "fallback_reason": fallback_reason,
    })
    return result


def _eod_cached(name: str, params: dict[str, Any], fetch: Callable[[], pd.DataFrame],
                requested: str, effective: str,
                fallback_reason: Optional[str]) -> dict[str, Any]:
    """执行单日日终查询，并按目标日覆盖情况决定是否永久缓存。"""
    result = _cached(
        name, params, fetch, historical=True, data_status="final",
        trade_date=effective, expected_end=effective,
    )
    return _decorate_eod(result, requested, effective, fallback_reason)


def _timestamp_date(value: Any) -> Optional[str]:
    """仅在时间戳包含可识别日期时返回 YYYYMMDD。"""
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10}|\d{13}", text):
        timestamp = float(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(timestamp, common.shanghai_now().tzinfo).strftime("%Y%m%d")
    match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
    return "".join(match.groups()) if match else None


def _realtime_anchor() -> dict[str, str]:
    """用沪深300实时行情锚定快照日期，拒绝陈旧或未来数据。"""
    if ts is None:
        raise RuntimeError("tushare not installed")
    frame = ts.realtime_quote(ts_code="000300.SH")
    row = frame.iloc[0].to_dict() if frame is not None and not frame.empty else {}
    anchor_date = str(row.get("DATE") or "").replace("-", "")
    if anchor_date != common.today_str():
        raise RuntimeError(f"沪深300实时日期不是今天：{anchor_date or '空'}")
    return {"date": anchor_date, "time": str(row.get("TIME") or "").strip()}


def _normalize_realtime_frame(frame: pd.DataFrame, anchor: dict[str, str],
                              source: str) -> pd.DataFrame:
    """把东财或新浪字段统一为全市场快照字段，并保留真实日期锚。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=REALTIME_COLUMNS)
    normalized = frame.copy()
    for column in REALTIME_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = None
    normalized["DATE"] = normalized["DATE"].fillna("").astype(str).str.replace("-", "", regex=False)
    normalized.loc[normalized["DATE"].str.strip() == "", "DATE"] = anchor["date"]
    if source == "dc":
        normalized["TIME"] = normalized["TIME"].fillna("").astype(str)
        normalized.loc[normalized["TIME"].str.strip() == "", "TIME"] = anchor["time"]
    normalized["quote_source"] = source
    for column in ("PRICE", "PCT_CHANGE", "CLOSE", "OPEN", "HIGH", "LOW",
                   "VOLUME", "AMOUNT"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized["TS_CODE"] = normalized["TS_CODE"].fillna("").astype(str).str.upper().str.strip()
    normalized["NAME"] = normalized["NAME"].fillna("").astype(str).str.strip()
    normalized["TIME"] = normalized["TIME"].fillna("").astype(str).str.strip()
    return normalized[REALTIME_COLUMNS]


def _fetch_sina_market(anchor: dict[str, str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """复用 Tushare 新浪协议并发抓取全市场，每个 HTTP 请求均设置严格超时。"""
    from tushare.stock.rtq import (
        zh_sina_a_stock_cookies, zh_sina_a_stock_count_url,
        zh_sina_a_stock_headers, zh_sina_a_stock_payload, zh_sina_a_stock_url,
    )
    from tushare.util.format_stock_code import format_stock_code

    session = common.get_session()
    count_response = session.get(
        zh_sina_a_stock_count_url, headers=zh_sina_a_stock_headers,
        cookies=zh_sina_a_stock_cookies, timeout=(3.05, 6),
    )
    count_response.raise_for_status()
    count_values = re.findall(r"\d+", count_response.text)
    if not count_values:
        raise RuntimeError("新浪全市场分页数解析失败")
    page_count = (int(count_values[0]) + 79) // 80
    if page_count <= 0:
        raise RuntimeError("新浪全市场分页数无效")

    def fetch_page(page: int) -> list[dict[str, Any]]:
        payload = dict(zh_sina_a_stock_payload)
        payload["page"] = str(page)
        response = session.get(
            zh_sina_a_stock_url, headers=zh_sina_a_stock_headers,
            cookies=zh_sina_a_stock_cookies, params=payload, timeout=(3.05, 6),
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError("新浪分页返回结构异常")
        return data

    rows: list[dict[str, Any]] = []
    failures: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=min(8, page_count)) as executor:
        futures = {executor.submit(fetch_page, page): page
                   for page in range(1, page_count + 1)}
        for future in as_completed(futures):
            page = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:
                failures[page] = f"{type(exc).__name__}: {exc}"[:200]

    raw = pd.DataFrame(rows)
    if raw.empty:
        frame = pd.DataFrame(columns=REALTIME_COLUMNS)
    else:
        frame = pd.DataFrame({
            "TS_CODE": raw.get("symbol", pd.Series(index=raw.index, dtype=str)).map(format_stock_code),
            "NAME": raw.get("name"),
            "PRICE": raw.get("trade"),
            "PCT_CHANGE": raw.get("changepercent"),
            "CLOSE": raw.get("settlement"),
            "OPEN": raw.get("open"),
            "HIGH": raw.get("high"),
            "LOW": raw.get("low"),
            "VOLUME": raw.get("volume"),
            "AMOUNT": raw.get("amount"),
            "TIME": raw.get("ticktime"),
            "DATE": anchor["date"],
            "quote_source": "sina",
        })
    metadata = {
        "page_count": page_count,
        "pages_succeeded": page_count - len(failures),
        "pages_failed": len(failures),
        "failed_pages": sorted(failures),
        "page_errors": failures,
        "max_concurrency": 8,
        "request_timeout_seconds": {"connect": 3.05, "read": 6},
    }
    return _normalize_realtime_frame(frame, anchor, "sina"), metadata


def _assess_market_snapshot(frame: pd.DataFrame, anchor: dict[str, str],
                            source: str, metadata: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
    """校验日期锚、有效涨跌幅覆盖率、分页完整度与全市场股票数量。"""
    if frame is None or frame.empty:
        coverage = {
            "provider": source, "raw_rows": 0, "stock_count": 0,
            "expected_stock_count": _REALTIME_MIN_STOCKS,
            "stock_coverage_ratio": 0.0, "pct_change_coverage_ratio": 0.0,
            "page_coverage_ratio": 0.0, "coverage_ratio": 0.0,
            "coverage_threshold": _REALTIME_MIN_COVERAGE,
            "minimum_stock_count": _REALTIME_MIN_STOCKS,
            "pct_change_threshold": _REALTIME_MIN_PCT_COVERAGE,
            **metadata,
        }
        return pd.DataFrame(columns=REALTIME_COLUMNS), coverage, ["未返回任何行情记录"]

    code_valid = frame["TS_CODE"].str.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", na=False)
    valid = frame[code_valid].drop_duplicates("TS_CODE").copy()
    page_count = max(int(metadata.get("page_count") or 1), 1)
    page_ratio = min(float(metadata.get("pages_succeeded", 1)) / page_count, 1.0)
    expected_rows = (page_count * 80 if source == "sina"
                     else max(_REALTIME_MIN_STOCKS, len(valid)))
    stock_ratio = min(len(valid) / max(expected_rows, 1), 1.0)
    pct_valid_count = int(valid["PCT_CHANGE"].notna().sum())
    pct_ratio = pct_valid_count / max(len(valid), 1)
    price_valid_count = int(valid["PRICE"].notna().sum())
    price_ratio = price_valid_count / max(len(valid), 1)
    coverage_ratio = min(page_ratio, stock_ratio, pct_ratio)

    date_values = valid["DATE"].fillna("").astype(str).str.replace("-", "", regex=False)
    timestamp_dates = [date for value in valid["TIME"].tolist()
                       if (date := _timestamp_date(value)) is not None]
    invalid_dates = sorted(
        ({date for date in date_values if date and date != anchor["date"]}
         | {date for date in timestamp_dates if date != anchor["date"]})
    )
    date_ratio = float((date_values == anchor["date"]).mean()) if len(valid) else 0.0
    issues: list[str] = []
    if len(valid) < _REALTIME_MIN_STOCKS:
        issues.append(f"有效股票数不足：{len(valid)} < {_REALTIME_MIN_STOCKS}")
    if page_ratio < _REALTIME_MIN_COVERAGE:
        issues.append(f"分页覆盖不足：{page_ratio:.4f}")
    if stock_ratio < _REALTIME_MIN_COVERAGE:
        issues.append(f"股票覆盖不足：{stock_ratio:.4f}")
    if pct_ratio < _REALTIME_MIN_PCT_COVERAGE:
        issues.append(f"有效涨跌幅覆盖不足：{pct_ratio:.4f}")
    if date_ratio < 1.0 or invalid_dates:
        issues.append(f"行情日期不属于当日锚：{','.join(invalid_dates) or '存在空日期'}")

    coverage = {
        **metadata,
        "provider": source,
        "anchor_code": "000300.SH",
        "anchor_date": anchor["date"],
        "anchor_time": anchor["time"],
        "raw_rows": len(frame),
        "stock_count": len(valid),
        "expected_stock_count": expected_rows,
        "valid_price_count": price_valid_count,
        "valid_pct_change_count": pct_valid_count,
        "page_coverage_ratio": round(page_ratio, 4),
        "stock_coverage_ratio": round(stock_ratio, 4),
        "price_coverage_ratio": round(price_ratio, 4),
        "pct_change_coverage_ratio": round(pct_ratio, 4),
        "date_coverage_ratio": round(date_ratio, 4),
        "coverage_ratio": round(coverage_ratio, 4),
        "coverage_threshold": _REALTIME_MIN_COVERAGE,
        "minimum_stock_count": _REALTIME_MIN_STOCKS,
        "pct_change_threshold": _REALTIME_MIN_PCT_COVERAGE,
        "timestamp_dates": sorted(set(timestamp_dates)),
        "validation_passed": not issues,
    }
    return valid[REALTIME_COLUMNS], coverage, issues


def _empty_realtime_snapshot(errors: list[str], provider_chain: list[str],
                             quote_date: Optional[str] = None) -> dict[str, Any]:
    """构造不含伪造行情的降级结果；空结果不进入进程缓存。"""
    return {
        "source": "realtime_market_none",
        "fetched_at": common.now_str(),
        "rows": [],
        "provider_chain": provider_chain,
        "quote_date": quote_date,
        "as_of": common.now_str(),
        "coverage": {},
        "degraded": True,
        "errors": errors,
    }


def fetch_realtime_market_snapshot(prefer: str = "dc") -> dict[str, Any]:
    """返回统一全市场实时快照，东财不足时并发切换新浪底层分页接口。

    返回固定包含 rows、provider_chain、quote_date、as_of、coverage、degraded、
    errors。非空结果进程内缓存三十秒；空结果不缓存，也不伪造日期或行情。
    """
    preferred = str(prefer or "dc").strip().lower()
    if preferred not in {"dc", "sina"}:
        raise ValueError("prefer 仅支持 dc 或 sina")
    cache_key = preferred

    def cached_payload() -> Optional[dict[str, Any]]:
        now_mono = time.monotonic()
        with _REALTIME_CACHE_LOCK:
            cached = _REALTIME_CACHE.get(cache_key)
            if (cached and now_mono - float(cached["cached_at"]) < _REALTIME_CACHE_TTL
                    and cached["payload"].get("quote_date") == common.today_str()
                    and cached["payload"].get("rows")):
                return dict(cached["payload"])
        return None

    cached = cached_payload()
    if cached is not None:
        return cached

    with _REALTIME_FETCH_LOCK:
        cached = cached_payload()
        if cached is not None:
            return cached
        provider_chain: list[str] = []
        errors: list[str] = []
        try:
            anchor = _realtime_anchor()
        except Exception as exc:
            errors.append(f"date_anchor: {type(exc).__name__}: {exc}"[:300])
            return _empty_realtime_snapshot(errors, provider_chain)

        sources = ["sina"] if preferred == "sina" else ["dc", "sina"]
        candidates: list[tuple[float, dict[str, Any]]] = []
        for source in sources:
            provider_chain.append(source)
            try:
                if source == "dc":
                    if ts is None:
                        raise RuntimeError("tushare not installed")
                    frame = _normalize_realtime_frame(ts.realtime_list(src="dc"), anchor, "dc")
                    metadata = {
                        "page_count": 1, "pages_succeeded": 1,
                        "pages_failed": 0, "failed_pages": [], "page_errors": {},
                    }
                else:
                    frame, metadata = _fetch_sina_market(anchor)
                valid, coverage, issues = _assess_market_snapshot(
                    frame, anchor, source, metadata)
                if issues:
                    errors.extend(f"{source}: {issue}" for issue in issues)
                payload = {
                    "source": f"realtime_market_{source}",
                    "fetched_at": common.now_str(),
                    "rows": valid.astype(object).where(pd.notna(valid), None).to_dict(orient="records"),
                    "provider_chain": list(provider_chain),
                    "quote_date": anchor["date"],
                    "as_of": common.now_str(),
                    "coverage": coverage,
                    "degraded": bool(issues),
                    "errors": list(errors),
                }
                candidates.append((float(coverage.get("coverage_ratio") or 0), payload))
                if not issues:
                    break
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}"[:300])

        if not candidates:
            return _empty_realtime_snapshot(errors, provider_chain, anchor["date"])
        _, payload = max(candidates, key=lambda item: item[0])
        payload["provider_chain"] = list(provider_chain)
        payload["errors"] = list(errors)
        if (payload["rows"] and payload.get("degraded") is not True
                and payload.get("coverage", {}).get("validation_passed") is True):
            with _REALTIME_CACHE_LOCK:
                _REALTIME_CACHE[cache_key] = {
                    "cached_at": time.monotonic(), "payload": payload,
                }
        return dict(payload)


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
          "大盘/指数日线（默认三大指数）：默认安全就绪日；显式请求未就绪日期会披露回退",
          params=[{"name": "codes", "type": "array", "required": False,
                   "default": DEFAULT_INDEX, "desc": "指数代码数组或逗号分隔字符串"},
                  {"name": "date", "type": "string", "required": False,
                   "desc": "YYYYMMDD，默认最近数据就绪交易日"}],
          returns="rows / requested_codes / requested_date / effective_date / actual_dates / missing_codes / degraded")
def market_index(p: dict) -> dict:
    pro = common.get_pro()
    requested_date, effective_date, fallback_reason = _resolve_eod_date(p)
    codes = _normalize_codes(p.get("codes"))
    range_start = (datetime.strptime(effective_date, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")

    def fetch() -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for code in codes:
            frame = pd.DataFrame()
            try:
                frame = pro.index_daily(ts_code=code, trade_date=effective_date)
            except Exception:
                frame = pd.DataFrame()
            if frame is None or frame.empty:
                try:
                    frame = pro.index_daily(
                        ts_code=code, start_date=range_start, end_date=effective_date)
                except Exception:
                    frame = pd.DataFrame()
            if frame is not None and not frame.empty:
                if "trade_date" in frame.columns:
                    frame = frame.sort_values("trade_date", ascending=False).head(1)
                frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    result = _eod_cached(
        "index_daily_resilient", {"codes": codes, "td": effective_date}, fetch,
        requested_date, effective_date, fallback_reason,
    )
    rows = result.get("rows") or []
    returned_codes = {str(row.get("ts_code", "")) for row in rows}
    missing_codes = [code for code in codes if code not in returned_codes]
    actual_dates = {str(row.get("ts_code", "")): str(row.get("trade_date", "")) for row in rows}
    used_source_fallback = any(
        date and date != effective_date for date in actual_dates.values()
    )
    if used_source_fallback and not result.get("fallback_reason"):
        result["fallback_reason"] = "目标日指数数据不完整，部分代码使用此前最近记录"
    result.update({
        "requested_codes": codes,
        "actual_dates": actual_dates,
        "missing_codes": missing_codes,
        "degraded": bool(missing_codes or used_source_fallback or fallback_reason),
    })
    return result


@register("market_realtime", G_MKT,
          "批量实时行情快照，统一东财与新浪降级链，支持代码和名称关键词查询",
          params=[{"name": "codes", "type": "array", "required": False,
                   "desc": "股票代码数组或逗号分隔字符串"},
                  {"name": "names", "type": "array", "required": False,
                   "desc": "股票名称关键词数组或逗号分隔字符串，按名称包含匹配"},
                  {"name": "name", "type": "string", "required": False,
                   "desc": "单个股票名称关键词，兼容简化调用"}],
          returns="rows / provider_chain / quote_date / coverage / degraded / errors / missing_codes / missing_names")
def market_realtime(p: dict) -> dict:
    """从统一全市场快照筛选代码；响应始终披露来源链、日期与覆盖情况。"""
    requested_codes = [code.upper() for code in _normalize_tokens(p.get("codes"))]
    requested_names = _normalize_names(p.get("names") or p.get("name"))
    if not requested_codes and not requested_names:
        raise ParamError("至少提供 codes、names 或 name 之一")

    basic_rows: list[dict[str, Any]] = []
    if requested_names:
        basic = _stock_basic_snapshot()
        basic_rows = list(basic.get("rows") or [])
    name_rows = (_filter_stock_basic_rows(basic_rows, [], requested_names)
                 if requested_names else [])

    resolved_codes = list(requested_codes)
    resolved: list[dict[str, str]] = []
    basic_by_code = {str(row.get("ts_code", "")).strip().upper(): row
                     for row in basic_rows}
    for code in requested_codes:
        row = basic_by_code.get(code, {})
        resolved.append({"code": code, "name": str(row.get("name", "")),
                         "matched_by": "code"})
    for row in name_rows:
        code = str(row.get("ts_code", "")).strip().upper()
        if code and code not in resolved_codes:
            resolved_codes.append(code)
            resolved.append({"code": code, "name": str(row.get("name", "")),
                             "matched_by": "name"})

    missing_names = [name for name in requested_names
                     if not any(name.casefold() in str(row.get("name", "")).casefold()
                                for row in name_rows)]
    if resolved_codes:
        result = fetch_realtime_market_snapshot()
        code_set = set(resolved_codes)
        result["rows"] = [row for row in result.get("rows") or []
                          if str(row.get("TS_CODE", "")).upper() in code_set]
    else:
        result = _empty_realtime_snapshot(
            ["未能把名称关键词解析为股票代码"], [], None)

    returned_codes = {str(row.get("TS_CODE") or row.get("ts_code") or "").strip().upper()
                      for row in result.get("rows") or []}
    missing_codes = [code for code in resolved_codes if code not in returned_codes]
    result.update({
        "requested_codes": requested_codes,
        "requested_names": requested_names,
        "resolved": resolved,
        "missing_codes": missing_codes,
        "missing_names": missing_names,
        "degraded": bool(result.get("degraded") or missing_codes or missing_names),
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
                     fetch, historical=True, expected_end=end)
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
                   historical=True, expected_end=p["end"])


@register("market_limit", G_MKT, "每日涨跌停/炸板统计（情绪面）",
          params=[{"name": "date", "type": "string", "required": False,
                   "desc": "YYYYMMDD，默认最近数据就绪交易日"}])
def market_limit(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("limit_list_d", {"td": td},
                       lambda: pro.limit_list_d(trade_date=td), requested, td, reason)


@register("market_lianban", G_MKT, "涨停最强/连板板块统计",
          params=[{"name": "date", "type": "string", "required": False}])
def market_lianban(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("limit_cpt_list", {"td": td},
                       lambda: pro.limit_cpt_list(trade_date=td), requested, td, reason)


@register("market_stk_limit", G_MKT, "每日个股涨跌停价",
          params=[{"name": "date", "type": "string", "required": False}])
def market_stk_limit(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("stk_limit", {"td": td},
                       lambda: pro.stk_limit(trade_date=td), requested, td, reason)


@register("market_index_dailybasic", G_MKT, "大盘每日指标（PE/PB/换手/总市值）",
          params=[{"name": "date", "type": "string", "required": False}])
def market_index_dailybasic(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("index_dailybasic", {"td": td},
                       lambda: pro.index_dailybasic(trade_date=td), requested, td, reason)


# ================= money =================
@register("money_flow", G_MONEY, "个股资金流向（东财）",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "date", "type": "string", "required": False}])
def money_flow(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("moneyflow_dc", {"c": p["code"], "td": td},
                       lambda: pro.moneyflow_dc(ts_code=p["code"], trade_date=td),
                       requested, td, reason)


@register("money_flow_ind", G_MONEY, "行业板块资金流向（东财）",
          params=[{"name": "date", "type": "string", "required": False}])
def money_flow_ind(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("moneyflow_ind_dc", {"td": td},
                       lambda: pro.moneyflow_ind_dc(trade_date=td), requested, td, reason)


@register("money_hsgt", G_MONEY, "北向资金全天净流入",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hsgt(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("moneyflow_hsgt", {"td": td},
                       lambda: pro.moneyflow_hsgt(trade_date=td), requested, td, reason)


@register("money_hsgt_top10", G_MONEY, "沪深股通十大成交股",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hsgt_top10(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("hsgt_top10", {"td": td},
                       lambda: pro.hsgt_top10(trade_date=td), requested, td, reason)


@register("money_toplist", G_MONEY, "龙虎榜每日明细（上榜个股）",
          params=[{"name": "date", "type": "string", "required": False}])
def money_toplist(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("top_list", {"td": td},
                       lambda: pro.top_list(trade_date=td), requested, td, reason)


@register("money_topinst", G_MONEY, "龙虎榜机构/营业部席位明细",
          params=[{"name": "date", "type": "string", "required": False}])
def money_topinst(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("top_inst", {"td": td},
                       lambda: pro.top_inst(trade_date=td), requested, td, reason)


@register("money_hm_list", G_MONEY, "游资名录", params=[])
def money_hm_list(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("hm_list", {}, lambda: pro.hm_list())


@register("money_hm_detail", G_MONEY, "游资每日交易明细",
          params=[{"name": "date", "type": "string", "required": False}])
def money_hm_detail(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("hm_detail", {"td": td},
                       lambda: pro.hm_detail(trade_date=td), requested, td, reason)


# ================= fundamental =================
@register("fundamental_daily_basic", G_FUND, "个股每日指标（PE/PB/换手/市值），PE仅作风险背景",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "date", "type": "string", "required": False}])
def fundamental_daily_basic(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("daily_basic", {"c": p["code"], "td": td},
                       lambda: pro.daily_basic(ts_code=p["code"], trade_date=td),
                       requested, td, reason)


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
    # tushare forecast 要求 period/ts_code/ann_date 至少一个；都缺省时默认取最近交易日的公告，
    # 避免无参调用直接报「ann_date和ts_code至少输入一个参数」。
    if not kw:
        kw["ann_date"] = common.last_trade_date()
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
# 说明：当前 tushare token 档位无 news/anns_d/cctv_news 接口权限（返回 402），
# 属「资讯类」服务，按工程降级策略统一由 agent 从各财经平台多源获取（≥2 来源交叉验证），
# 故此处不再注册 news_flash / news_filter / news_anns / news_cctv 等数据服务接口。
# 详见 agent/skills/data-service/SKILL.md「资讯类外部获取」与 doc/INTERFACE_AVAILABILITY.md。


# ================= overseas 外盘 =================
# overseas_us（us_daily）当前 token 档位无数据返回，属资讯/外盘类，改由 agent 外部多源获取，
# 故不再注册；港股 hk_daily 可用，保留。
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
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("kpl_list", {"td": td},
                       lambda: pro.kpl_list(trade_date=td), requested, td, reason)


# hot_kpl_concept（kpl_concept）当前 token 返回「请指定正确的接口名」不可用，已移除；
# 题材/概念强度改用 hot_kpl_list + hot_dc/hot_ths + 涨停连板聚合。


# ================= sector 板块/行业 =================
@register("sector_dc", G_SEC, "板块行情排名（东财）",
          params=[{"name": "date", "type": "string", "required": False}])
def sector_dc(p: dict) -> dict:
    pro = common.get_pro()
    requested, td, reason = _resolve_eod_date(p)
    return _eod_cached("dc_index", {"td": td},
                       lambda: pro.dc_index(trade_date=td), requested, td, reason)


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
                   historical=True, expected_end=p["end"])


@register("sector_ths_daily", G_SEC, "同花顺板块/概念指数日线",
          params=[{"name": "code", "type": "string", "required": True},
                  {"name": "start", "type": "string", "required": True},
                  {"name": "end", "type": "string", "required": True}])
def sector_ths_daily(p: dict) -> dict:
    pro = common.get_pro()
    return _cached("ths_daily", {"c": p["code"], "s": p["start"], "e": p["end"]},
                   lambda: pro.ths_daily(ts_code=p["code"], start_date=p["start"], end_date=p["end"]),
                   historical=True, expected_end=p["end"])


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

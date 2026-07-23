"""选股登记、看板与混合期限回测工具（DB 持久化 + 成熟样本固化）。

- log_selection：登记标的到 DB（selections 表），按 (日期,代码,category) **幂等去重**。
  category=auto(自动选股,用于调参) / manual(用户触发正式选股) /
  watch(用户关注) / holding(用户持仓)。所有正式选股保存选股价、热点/事件、短线地位和量化因子快照。
- selection_dashboard：按日期/热点/类别查询选股，并补最近交易日行情与选股后涨跌。
- selection_backtest：统计选出后 1/2/3 个交易日、7/30 个自然日目标后的首个交易日收益，
  计算胜率及相对沪深300超额，按 category、auto 的 driver/分数分桶产出调参依据。
  **成功样本按计算版本固化到 selection_forward_returns_v2；未成熟或失败样本后续重试**，
  避免反复覆盖已成功收益，同时允许长期期限随时间增量成熟。
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import audit_log
import common
import db
import factor_contract
import factor_config
import selection_tags
from registry import ParamError, register

try:
    import tushare as ts
except ImportError:
    ts = None  # type: ignore

HORIZON_SPECS = (
    {"key": "1t", "storage": 1, "days": 1, "kind": "trading", "label": "1个交易日"},
    {"key": "2t", "storage": 2, "days": 2, "kind": "trading", "label": "2个交易日"},
    {"key": "3t", "storage": 3, "days": 3, "kind": "trading", "label": "3个交易日"},
    {"key": "7c", "storage": 7, "days": 7, "kind": "calendar", "label": "7个自然日"},
    {"key": "30c", "storage": 30, "days": 30, "kind": "calendar", "label": "30个自然日"},
)
BENCHMARK = "000300.SH"
RETURN_CALC_VERSION = "forward-returns-v3-mixed-horizons"
MIN_OPTIMIZATION_SAMPLES = 50
MIN_OOS_SAMPLES = 10
VALID_CATEGORIES = {"auto", "manual", "watch", "holding"}


def _to_date(s: str):
    s = str(s).replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


def _selection_time(value: Any, date_value: Any = None) -> tuple[str, datetime]:
    """规范化选股日期和上海时间；显式日期与时间必须属于同一天。"""
    zone = ZoneInfo(common.TZ)
    if value:
        text_value = str(value).strip().replace("Z", "+00:00")
        try:
            selected_at = datetime.fromisoformat(text_value)
        except ValueError as exc:
            raise ParamError("selected_at 必须是 ISO 时间，如 2026-07-16 10:30:00") from exc
        if selected_at.tzinfo is not None:
            selected_at = selected_at.astimezone(zone).replace(tzinfo=None)
    else:
        now = datetime.now(zone).replace(tzinfo=None)
        selected_day = str(date_value or common.today_str()).replace("-", "")
        selected_at = (now if selected_day == now.strftime("%Y%m%d")
                       else datetime.strptime(selected_day + " 15:00:00", "%Y%m%d %H:%M:%S"))

    selected_date = str(date_value or selected_at.strftime("%Y%m%d")).replace("-", "")
    try:
        _to_date(selected_date)
    except ValueError as exc:
        raise ParamError("date 必须是有效 YYYYMMDD") from exc
    if selected_at.strftime("%Y%m%d") != selected_date:
        raise ParamError("selected_at 与 date 必须属于同一天")
    return selected_date, selected_at


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _market_snapshot(codes: list[str]) -> dict[str, Any]:
    """批量获取实时价格，并以最近完成日行情兜底；价格、涨幅和涨跌停价必须同日。"""
    normalized = list(dict.fromkeys(str(code).strip().upper() for code in codes if code))
    refreshed_at = common.now_str()
    if not normalized:
        return {"refreshed_at": refreshed_at, "quote_date": None,
                "quote_date_min": None, "quote_date_max": None,
                "mixed_quote_dates": False, "quotes": {}, "errors": []}

    def normalize_trade_date(value: Any) -> Optional[str]:
        text = str(value or "").strip().replace("-", "")
        if len(text) != 8 or not text.isdigit():
            return None
        try:
            _to_date(text)
        except ValueError:
            return None
        return text

    errors: list[str] = []
    realtime: dict[str, dict[str, Any]] = {}
    if ts is not None:
        for offset in range(0, len(normalized), 100):
            chunk = normalized[offset:offset + 100]
            try:
                frame = ts.realtime_quote(ts_code=",".join(chunk))
                for raw in frame.to_dict(orient="records") if frame is not None else []:
                    code = str(raw.get("TS_CODE") or raw.get("ts_code") or "").upper()
                    if code:
                        realtime[code] = raw
            except Exception as exc:
                errors.append(f"实时行情失败：{type(exc).__name__}: {exc}"[:300])
    else:
        errors.append("实时行情失败：tushare 未安装")

    pro = None
    try:
        pro = common.get_pro()
    except Exception as exc:
        errors.append(f"行情客户端失败：{type(exc).__name__}: {exc}"[:300])

    completed_date: Optional[str] = None
    daily_map: dict[str, dict[str, Any]] = {}
    basic_map: dict[str, dict[str, Any]] = {}
    try:
        completed_date = normalize_trade_date(common.market_clock()["last_data_ready_date"])
        if pro is not None and completed_date:
            daily = common.cached_call(
                "selection_dashboard_daily", {"trade_date": completed_date},
                lambda: pro.daily(trade_date=completed_date), historical=True,
                trade_date=completed_date, expected_end=completed_date)
            daily_map = {str(row.get("ts_code") or "").upper(): row
                         for row in daily.get("rows", []) if row.get("ts_code")}
    except Exception as exc:
        errors.append(f"最近收盘行情失败：{type(exc).__name__}: {exc}"[:300])
    try:
        if pro is not None and completed_date:
            basics = common.cached_call(
                "selection_dashboard_basic", {"trade_date": completed_date},
                lambda: pro.daily_basic(
                    trade_date=completed_date,
                    fields="ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio"),
                historical=True, trade_date=completed_date, expected_end=completed_date)
            basic_map = {str(row.get("ts_code") or "").upper(): row
                         for row in basics.get("rows", []) if row.get("ts_code")}
    except Exception as exc:
        errors.append(f"最近换手数据失败：{type(exc).__name__}: {exc}"[:300])

    today = common.today_str()
    valid_realtime_dates: dict[str, str] = {}
    for code, row in realtime.items():
        trade_date = normalize_trade_date(row.get("DATE"))
        if (trade_date is None or trade_date > today
                or (completed_date is not None and trade_date < completed_date)):
            errors.append(f"{code} 实时行情日期无效或陈旧，已回退最近收盘")
            continue
        valid_realtime_dates[code] = trade_date

    limit_dates = set(valid_realtime_dates.values())
    if completed_date:
        limit_dates.add(completed_date)
    limit_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    if pro is not None:
        for trade_date in sorted(limit_dates):
            try:
                limits = common.cached_call(
                    "selection_dashboard_limits", {"trade_date": trade_date},
                    lambda trade_date=trade_date: pro.stk_limit(trade_date=trade_date),
                    historical=True, trade_date=trade_date, expected_end=trade_date)
                limit_by_date[trade_date] = {
                    str(row.get("ts_code") or "").upper(): row
                    for row in limits.get("rows", []) if row.get("ts_code")}
            except Exception as exc:
                errors.append(f"{trade_date}涨跌停价失败：{type(exc).__name__}: {exc}"[:300])

    quotes: dict[str, dict[str, Any]] = {}
    quote_dates: list[str] = []
    for code in normalized:
        rt = realtime.get(code, {})
        daily = daily_map.get(code, {})
        basic = basic_map.get(code, {})
        realtime_date = valid_realtime_dates.get(code)
        realtime_price = _finite_float(rt.get("PRICE"))
        use_realtime = realtime_date is not None and realtime_price is not None and realtime_price > 0
        if use_realtime:
            price = realtime_price
            pre_close = _finite_float(rt.get("PRE_CLOSE"))
            trade_date = realtime_date
            quote_time = " ".join(part for part in (
                str(rt.get("DATE") or "").strip(), str(rt.get("TIME") or "").strip()) if part)
            source = "realtime_quote"
        else:
            price = _finite_float(daily.get("close"))
            pre_close = _finite_float(daily.get("pre_close"))
            trade_date = normalize_trade_date(daily.get("trade_date")) or completed_date
            quote_time = trade_date or ""
            source = "tushare daily close" if price is not None else "unavailable"
        if trade_date:
            quote_dates.append(trade_date)
        pct_chg = ((price / pre_close - 1) * 100
                   if price is not None and pre_close not in (None, 0) else None)
        limit_row = limit_by_date.get(trade_date or "", {}).get(code, {})
        up_limit = _finite_float(limit_row.get("up_limit"))
        down_limit = _finite_float(limit_row.get("down_limit"))
        auto_tags: list[str] = []
        if price is not None and up_limit is not None and abs(price - up_limit) <= 0.0051:
            auto_tags.append("涨停")
        if price is not None and down_limit is not None and abs(price - down_limit) <= 0.0051:
            auto_tags.append("跌停")
        quotes[code] = {
            "latest_price": price,
            "latest_chg_pct": round(pct_chg, 4) if pct_chg is not None else None,
            "latest_quote_time": quote_time or None,
            "latest_trade_date": trade_date,
            "quote_source": source,
            "turnover_rate": _finite_float(basic.get("turnover_rate")),
            "turnover_trade_date": completed_date,
            "amount": _finite_float(daily.get("amount")),
            "amount_trade_date": completed_date,
            "market_tags": auto_tags,
        }
    unique_dates = sorted(set(quote_dates))
    return {"refreshed_at": refreshed_at,
            "quote_date": unique_dates[-1] if unique_dates else completed_date,
            "quote_date_min": unique_dates[0] if unique_dates else completed_date,
            "quote_date_max": unique_dates[-1] if unique_dates else completed_date,
            "mixed_quote_dates": len(unique_dates) > 1,
            "quotes": quotes, "errors": list(dict.fromkeys(errors))}


def _record_tags(extra: dict[str, Any], driver: str = "") -> list[str]:
    """读取新标签；旧记录从热点、短线地位和驱动字段兼容合成。"""
    raw_tags = extra.get("tags")
    try:
        tags = selection_tags.normalize_tags(raw_tags) if isinstance(raw_tags, list) else []
    except ValueError:
        tags = []
    for value in (extra.get("market_role"), extra.get("hotspot"), driver):
        tag = str(value or "").strip()
        if tag and tag not in {"未分类", "未标注", "非主线"} and tag not in tags:
            tags.append(tag)
    return tags[:24]


def _default_selection_range() -> tuple[Any, Any]:
    """默认展示目标交易日及其之前三个交易日，共四个交易日。

    注意口径差异（有意为之）：此处末日用 last_trade_date（含当日，盘中登记的候选
    当天即可见），而 _market_snapshot 的行情兜底使用 last_data_ready_date（默认 18:00
    安全线，避免拿到当天未完整发布的日终数据）。两者分别服务于展示日期与行情日期，
    实际行情日由 latest_trade_date / quote_source / mixed_quote_dates 暴露。
    """
    end_text = common.last_trade_date()
    start_window = (datetime.strptime(end_text, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    calendar = common.get_pro().trade_cal(
        exchange="SSE", start_date=start_window, end_date=end_text, is_open="1")
    open_days = sorted(calendar[calendar["is_open"] == 1]["cal_date"].astype(str).tolist())
    if not open_days:
        raise RuntimeError("无法获取默认选股看板交易日范围")
    return _to_date(open_days[-4] if len(open_days) >= 4 else open_days[0]), _to_date(open_days[-1])


@register("selection_tag_catalog", "review",
          "选股标签合集：返回固定标签及面向 Agent 的标签说明；板块、题材、事件标签可按规范自行补充",
          params=[], returns="selection_tag_version / rows[{tag,description}]")
def selection_tag_catalog(p: dict) -> dict:
    return {"source": "selection_tag_catalog", "fetched_at": common.now_str(),
            "selection_tag_version": selection_tags.TAG_VERSION,
            "rows": list(selection_tags.CATALOG)}


# ---------------- 登记 ----------------
def _capture_selection_price(code: str, selected_date: str) -> dict[str, Any]:
    """仅抓选股交易日当天收盘价；无数据时留空，禁止回退到未来日期。"""
    try:
        pro = common.get_pro()
        payload = common.cached_call(
            "selection_entry_quote", {"code": code, "trade_date": selected_date},
            lambda: pro.daily(ts_code=code, trade_date=selected_date),
            historical=True, trade_date=selected_date, expected_end=selected_date)
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


def _valid_price(value: Any) -> Optional[float]:
    price = _finite_float(value)
    return price if price is not None and price > 0 else None


def _ensure_selection_price(record: dict[str, Any], trigger: str) -> tuple[dict[str, Any], bool]:
    """缺失时按选股日原始收盘价补齐并落库；失败时保持为空。"""
    extra = dict(record.get("extra") or {})
    if _valid_price(extra.get("selected_price")) is not None:
        return record, False
    selected_date = str(record.get("sel_date") or "").replace("-", "")
    snapshot = _capture_selection_price(str(record.get("code") or ""), selected_date)
    price = _valid_price(snapshot.get("selected_price"))
    if price is None:
        return record, False
    patched = db.patch_selection_price_if_missing(
        int(record["id"]), price, selected_date,
        str(snapshot.get("price_source") or "tushare daily close"),
    )
    updated = dict(patched.get("record") or record)
    if patched.get("updated"):
        audit_log.append("selection", {
            "event": "price_backfilled", "trigger": trigger,
            "selection_id": record.get("id"), "date": selected_date,
            "code": record.get("code"), "name": record.get("name"),
            "category": record.get("category"), "selected_price": price,
            "price_source": snapshot.get("price_source"),
        })
    return updated, bool(patched.get("updated"))


def refresh_selection_quotes(items: list[dict[str, Any]]) -> dict[str, Any]:
    """仅刷新调用方当前列表的行情，并重试补齐这些记录的缺失选股价。"""
    selection_ids = []
    for item in items[:1000]:
        try:
            selection_ids.append(int(item.get("id")))
        except (TypeError, ValueError):
            continue
    records = db.fetch_selections_by_ids(selection_ids)
    refreshed_records = []
    backfilled = 0
    for record in records:
        updated, changed = _ensure_selection_price(record, "quote_refresh")
        refreshed_records.append(updated)
        backfilled += int(changed)
    market = _market_snapshot([str(record.get("code") or "") for record in refreshed_records])
    selected_prices = {}
    for record in refreshed_records:
        extra = record.get("extra") or {}
        selected_prices[str(record.get("id"))] = {
            "selected_price": _valid_price(extra.get("selected_price")),
            "selected_price_date": extra.get("price_trade_date"),
            "selected_price_source": extra.get("price_source"),
            "price_backfilled_at": extra.get("price_backfilled_at"),
        }
    return {**market, "selected_prices": selected_prices,
            "backfilled_prices": backfilled, "record_count": len(refreshed_records)}


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
          "规范化上传正式选股/历史观察快照（日期+代码+category 幂等）。保存核心事件、精炼理由、标签、选股时间、行情与完整因子契约；"
          "auto 用于调参，manual/watch/holding 仅隔离回测",
          params=[{"name": "code", "type": "string", "required": True, "desc": "tushare 完整股票代码"},
                  {"name": "name", "type": "string", "required": False, "default": ""},
                  {"name": "selected_at", "type": "string", "required": False,
                   "desc": "选股时间，ISO 格式；省略时由服务端按上海时间补充"},
                  {"name": "date", "type": "string", "required": False,
                   "desc": "兼容字段 YYYYMMDD；与 selected_at 必须同日"},
                  {"name": "score", "type": "float", "required": False, "default": 0.0,
                   "desc": "兼容字段；正式选股评分由 screening_run_id 对应运行提供"},
                  {"name": "score_raw", "type": "float", "required": False,
                   "desc": "兼容字段；正式选股以筛选运行快照为准"},
                  {"name": "score_percentile", "type": "float", "required": False,
                   "desc": "兼容字段；正式选股以筛选运行0~1分位为准"},
                  {"name": "screening_run_id", "type": "string", "required": False,
                   "desc": "screen_quant/screen_trend 返回的运行ID；auto/manual正式选股必填"},
                  {"name": "driver", "type": "string", "required": False, "default": "未标注",
                   "desc": "主导驱动：涨价/逻辑/预期/情绪"},
                  {"name": "core_event", "type": "string", "required": False,
                   "desc": "核心事件或催化；Agent 侧精炼为可核验短句"},
                  {"name": "reason", "type": "string", "required": True,
                   "desc": "精炼入选理由：实际受益、量化信号、风险证伪，不重复堆砌事件"},
                  {"name": "tags", "type": "array", "required": False, "strict": True,
                   "desc": "标签字符串数组；固定标签优先从 selection_tag_catalog 选，板块/题材/事件标签可自行编排"},
                  {"name": "category", "type": "string", "required": False, "default": "auto",
                   "desc": "auto|manual|watch|holding；用户触发正式选股使用 manual"},
                  {"name": "selected_price", "type": "float", "required": False,
                   "desc": "选股时价格；省略则服务端抓选股日收盘价"},
                  {"name": "hotspot", "type": "string", "required": False, "default": "",
                   "desc": "兼容的主板块/题材；未传时取首个 Agent 自编排标签作为聚合题材"},
                  {"name": "event", "type": "string", "required": False, "default": "",
                   "desc": "兼容字段；新调用使用 core_event"},
                  {"name": "market_role", "type": "string", "required": False, "default": "",
                   "desc": "兼容字段：核心/分支/补涨/非主线；新调用同时写入 tags"},
                  {"name": "factors", "type": "object", "required": False,
                   "desc": "兼容字段；正式选股因子以筛选运行和服务端契约快照为准"},
                  {"name": "extra", "type": "object", "required": False}],
          returns="登记结果，含服务端标签版本、规范标签、最新价及涨停/跌停标签")
def log_selection(p: dict) -> dict:
    cat = str(p.get("category", "auto")).strip()
    if cat not in VALID_CATEGORIES:
        return {"logged": False, "reason": f"category 须为 {sorted(VALID_CATEGORIES)}"}
    code = str(p.get("code") or "").strip().upper()
    if not code:
        return {"logged": False, "reason": "code 必填"}
    try:
        selected_date, selected_at = _selection_time(p.get("selected_at"), p.get("date"))
    except ParamError as exc:
        return {"logged": False, "reason": str(exc)}
    reason = str(p.get("reason") or "").strip()
    if not reason:
        return {"logged": False, "reason": "reason 必填且须由 Agent 精炼"}
    if len(reason) > 4000:
        return {"logged": False, "reason": "reason 最长 4000 个字符"}
    if p.get("extra") is not None and not isinstance(p.get("extra"), dict):
        return {"logged": False, "reason": "extra 必须是对象"}
    extra = dict(p.get("extra") or {})
    core_event = str(p.get("core_event") or p.get("event") or extra.get("core_event")
                     or extra.get("event") or "").strip()
    if cat in {"auto", "manual"} and not core_event:
        return {"logged": False, "reason": "正式选股必须填写精炼后的 core_event"}
    if len(core_event) > 1000:
        return {"logged": False, "reason": "core_event 最长 1000 个字符"}
    try:
        uploaded_tags = selection_tags.normalize_tags(
            p.get("tags") if "tags" in p else extra.get("tags"))
    except ValueError as exc:
        return {"logged": False, "reason": str(exc)}
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
        if (score_percentile is not None
                and (not math.isfinite(score_percentile) or not 0 <= score_percentile <= 1)):
            raise ValueError
    except (TypeError, ValueError):
        return {"logged": False, "reason": "score_percentile 必须是0~1之间的有限数值"}

    score_raw_value = (run_candidate.get("score_raw") if run_candidate
                       else p.get("score_raw", extra.get("score_raw")))
    score_raw = _finite_float(score_raw_value) if score_raw_value is not None else None
    if score_raw_value is not None and score_raw is None:
        return {"logged": False, "reason": "score_raw 必须是有限数值"}
    compatibility_score = 0.0
    if score_percentile is None and p.get("score") not in (None, ""):
        parsed_score = _finite_float(p.get("score"))
        if parsed_score is None:
            return {"logged": False, "reason": "score 必须是有限数值"}
        compatibility_score = parsed_score

    if cat == "auto" or p.get("selected_price") is None:
        price_snapshot = _capture_selection_price(code, selected_date)
    else:
        try:
            supplied_price = float(p["selected_price"])
            if not math.isfinite(supplied_price) or supplied_price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return {"logged": False, "reason": "selected_price 必须是大于0的有限数值"}
        price_snapshot = {"selected_price": supplied_price,
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

    market_snapshot = _market_snapshot([code])
    latest_quote = market_snapshot["quotes"].get(code, {})
    hotspot = str(p.get("hotspot") or extra.get("hotspot") or "").strip()
    market_role = str(p.get("market_role") or extra.get("market_role") or "").strip()
    driver = str(p.get("driver") or "未标注").strip() or "未标注"
    tags = [tag for tag in uploaded_tags if tag not in selection_tags.AUTO_MARKET_TAGS]
    for tag in (market_role, hotspot, driver):
        if tag and tag not in {"未分类", "未标注", "非主线"} and tag not in tags:
            tags.append(tag)
    market_tags = list(latest_quote.get("market_tags") or [])
    tags = tags[:max(0, 24 - len(market_tags))] + [tag for tag in market_tags if tag not in tags]
    primary_theme = selection_tags.primary_theme(tags, hotspot)

    extra.update(price_snapshot)
    extra.update({
        "selected_at": selected_at.strftime("%Y-%m-%d %H:%M:%S"),
        "selection_tag_version": selection_tags.TAG_VERSION,
        "tags": tags,
        "primary_theme": primary_theme,
        "hotspot": hotspot,
        "core_event": core_event,
        "event": core_event,
        "market_role": market_role,
        "latest_price_at_upload": latest_quote.get("latest_price"),
        "latest_quote_time_at_upload": latest_quote.get("latest_quote_time"),
        "latest_quote_source_at_upload": latest_quote.get("quote_source"),
        "market_tags_at_upload": market_tags,
        "market_quote_errors_at_upload": market_snapshot.get("errors") or [],
        "factors": factor_snapshot,
        "factor_source": factor_source,
        "factor_contract": factor_contract.base_contract("stock"),
        "factor_metadata": factor_metadata,
        "screening_run_id": run_id or None,
        "screening_function": screening_run.get("function_name") if screening_run else None,
        "screening_contract": screening_run.get("contract") if screening_run else None,
        "score_raw": score_raw,
        "screening_rank": run_candidate.get("rank") if run_candidate else None,
        "score_percentile": score_percentile,
        "trigger": "scheduled" if cat == "auto" else "user",
    })
    if not factor_snapshot:
        extra["factor_error"] = factor_source
    stored_score = score_percentile if score_percentile is not None else compatibility_score
    rec = {
        "sel_date": _to_date(selected_date), "code": code, "name": p.get("name", ""),
        "score": stored_score, "driver": driver,
        "reason": reason, "category": cat, "extra": extra,
        "logged_at": selected_at,
    }
    write = db.upsert_selection(rec)
    inserted = bool(write.get("inserted", True))
    record = dict(write.get("record") or rec)
    if record.get("id") is not None:
        record, _ = _ensure_selection_price(record, "log_selection")
    for key in ("sel_date", "logged_at"):
        if record.get(key) is not None:
            record[key] = str(record[key])
    stored_extra = record.get("extra") or extra
    current_quote = {
        "latest_price": latest_quote.get("latest_price"),
        "latest_chg_pct": latest_quote.get("latest_chg_pct"),
        "latest_quote_time": latest_quote.get("latest_quote_time"),
        "latest_trade_date": latest_quote.get("latest_trade_date"),
        "quote_source": latest_quote.get("quote_source"),
        "market_tags": market_tags,
    }
    return {"logged": True, "inserted": inserted, "duplicate": not inserted,
            "immutable": cat in {"auto", "manual"},
            "selection_tag_version": selection_tags.TAG_VERSION,
            "tags": stored_extra.get("tags") or [],
            # 兼容旧调用方保留顶层行情字段；current_quote 明确这是本次请求刷新值，而非原记录快照。
            "latest_price": current_quote["latest_price"],
            "latest_quote_time": current_quote["latest_quote_time"],
            "market_tags": market_tags, "current_quote": current_quote,
            "quote_errors": market_snapshot.get("errors") or [],
            "record": record}


# ---------------- 前向收益 ----------------
def _forward_returns(pro, code: str, sel_day: str) -> dict[str, dict[str, Any]]:
    """按混合期限精确对齐个股和基准；自然日期限取目标日后首个 SSE 交易日。"""
    selected_date = datetime.strptime(sel_day, "%Y%m%d")
    calendar_end = (selected_date + timedelta(days=100)).strftime("%Y%m%d")
    try:
        cal = pro.trade_cal(exchange="SSE", start_date=sel_day, end_date=calendar_end)
        dates = sorted(cal[cal["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist())
    except Exception as exc:
        return {spec["key"]: {"status": "failed",
                "error": f"交易日历失败：{type(exc).__name__}: {exc}"[:300]}
                for spec in HORIZON_SPECS}
    if not dates or dates[0] != sel_day:
        return {spec["key"]: {"status": "failed", "error": "选股日期不是交易日"}
                for spec in HORIZON_SPECS}

    exit_dates: dict[str, Optional[str]] = {}
    for spec in HORIZON_SPECS:
        if spec["kind"] == "trading":
            index = int(spec["days"])
            exit_dates[spec["key"]] = dates[index] if len(dates) > index else None
        else:
            target = (selected_date + timedelta(days=int(spec["days"]))).strftime("%Y%m%d")
            exit_dates[spec["key"]] = next((day for day in dates if day >= target), None)

    last_available = str(common.market_clock()["last_data_ready_date"])
    results = {
        spec["key"]: {
            "status": "not_matured", "entry_trade_date": sel_day,
            "exit_trade_date": exit_dates.get(spec["key"]),
        }
        for spec in HORIZON_SPECS
    }
    mature = {key: day for key, day in exit_dates.items()
              if day is not None and day <= last_available}
    if not mature:
        return results

    end = max(mature.values())
    try:
        stock_payload = common.cached_call(
            "selection_forward_qfq",
            {"code": code, "start_date": sel_day, "end_date": end},
            lambda: ts.pro_bar(ts_code=code, adj="qfq", start_date=sel_day, end_date=end),
            historical=True, data_status="final", expected_end=end,
        )
        stock_df = pd.DataFrame(stock_payload.get("rows", []))
    except Exception as exc:
        for key in mature:
            results[key].update(status="failed",
                                error=f"前复权行情失败：{type(exc).__name__}: {exc}"[:300])
        return results
    if stock_df.empty:
        for key in mature:
            results[key].update(status="failed", error="前复权行情为空")
        return results
    stock_prices = {str(row["trade_date"]): float(row["close"])
                    for row in stock_df.to_dict(orient="records") if row.get("close") is not None}
    try:
        bench_payload = common.cached_call(
            "selection_forward_benchmark",
            {"code": BENCHMARK, "start_date": sel_day, "end_date": end},
            lambda: pro.index_daily(ts_code=BENCHMARK, start_date=sel_day, end_date=end),
            historical=True, data_status="final", expected_end=end,
        )
        bench_df = pd.DataFrame(bench_payload.get("rows", []))
        bench_prices = {str(row["trade_date"]): float(row["close"])
                        for row in bench_df.to_dict(orient="records") if row.get("close") is not None}
    except Exception:
        bench_prices = {}

    entry = stock_prices.get(sel_day)
    bench_entry = bench_prices.get(sel_day)
    for key, exit_day in mature.items():
        exit_price = stock_prices.get(exit_day)
        if entry is None or exit_price is None:
            results[key] = {
                "status": "failed", "entry_trade_date": sel_day,
                "exit_trade_date": exit_day,
                "error": "个股在统一入场或退出交易日无前复权价格（可能停牌）",
            }
            continue
        ret = (exit_price / entry - 1) * 100
        bench_exit = bench_prices.get(exit_day)
        benchmark_ret = ((bench_exit / bench_entry - 1) * 100
                         if bench_entry and bench_exit is not None else None)
        results[key] = {
            "status": "success", "ret_pct": round(ret, 6),
            "excess_pct": round(ret - benchmark_ret, 6) if benchmark_ret is not None else None,
            "entry_trade_date": sel_day, "entry_price": entry,
            "exit_trade_date": exit_day, "exit_price": exit_price,
            "benchmark_entry_price": bench_entry, "benchmark_exit_price": bench_exit,
            "error": None,
        }
    return results


def _tuning_hints(by_driver: dict[str, dict[str, list[float]]]) -> list[str]:
    hints: list[str] = []
    horizon_key = "30c"
    avg = {driver: sum(values[horizon_key]) / len(values[horizon_key])
           for driver, values in by_driver.items() if values.get(horizon_key)}
    if not avg:
        return ["样本不足，暂无法给出调参建议（需更多已满 30 个自然日的自动选股样本）"]
    best = max(avg, key=avg.get)
    worst = min(avg, key=avg.get)
    hints.append(f"30自然日超额最优驱动：{best}（+{avg[best]:.2f}pct），可维持/提高其在选股中的权重")
    if avg[worst] < 0:
        hints.append(f"30自然日超额为负驱动：{worst}（{avg[worst]:.2f}pct），建议降低权重或提高入选门槛（尤其情绪类）")
    return hints


@register("selection_backtest", "review",
          "按统一交易日和前复权口径回测1/3/7/30日收益；自动保存可审计快照。"
          "只有筛选来源、因子契约、样本量和时序样本外表现均合格时才开放调参。",
          params=[{"name": "save_snapshot", "type": "bool", "required": False, "default": True,
                   "desc": "默认保存本次聚合、样本哈希和优化门禁"}],
          returns="收益统计、optimization_gate、snapshot_id、tuning_hints、逐周期收益/超额与最新行情明细")
def selection_backtest(p: dict) -> dict:
    pro = common.get_pro()
    sels = db.fetch_selections()
    current_contract = factor_contract.base_contract("stock")

    cat_returns: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    cat_excess: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_driver: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    auto_by_bucket: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    details: list[dict[str, Any]] = []
    controlled_30d: list[dict[str, Any]] = []
    computed_calls = 0
    backfilled_prices = 0

    for raw_selection in sels:
        selection, price_changed = _ensure_selection_price(raw_selection, "selection_backtest")
        backfilled_prices += int(price_changed)
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
        pending_specs = [spec for spec in HORIZON_SPECS
                         if cached.get(spec["storage"], {}).get("status") != "success"]
        if pending_specs:
            calculated = _forward_returns(pro, selection["code"], sel_day)
            computed_calls += 1
            for spec in pending_specs:
                item = calculated[spec["key"]]
                cached[spec["storage"]] = db.save_return_v2(
                    sid, spec["storage"], RETURN_CALC_VERSION, **item)

        returns: dict[str, float] = {}
        excess_returns: dict[str, float] = {}
        return_status: dict[str, str] = {}
        return_dates: dict[str, Optional[str]] = {}
        return_errors: dict[str, Optional[str]] = {}
        for spec in HORIZON_SPECS:
            key = spec["key"]
            item = cached.get(spec["storage"]) or {}
            return_status[key] = item.get("status", "missing")
            return_dates[key] = item.get("exit_trade_date")
            return_errors[key] = item.get("error")
            if item.get("status") != "success" or item.get("ret_pct") is None:
                continue
            ret = float(item["ret_pct"])
            excess = float(item["excess_pct"]) if item.get("excess_pct") is not None else None
            returns[key] = ret
            if excess is not None:
                excess_returns[key] = excess
            cat_returns[category][key].append(ret)
            if excess is not None:
                cat_excess[category][key].append(excess)
            if controlled:
                if excess is not None:
                    auto_by_driver[driver][key].append(excess)
                auto_by_bucket[bucket][key].append(ret)
                if key == "30c" and excess is not None:
                    controlled_30d.append({"id": sid, "date": sel_day, "driver": driver,
                                           "excess": excess, "return": ret})
        details.append({
            "id": sid, "date": str(selection["sel_date"]), "code": selection["code"],
            "name": selection.get("name", ""), "category": category, "driver": driver,
            "score": _finite_float(selection.get("score")),
            "selected_price": _valid_price(extra.get("selected_price")),
            "score_percentile": percentile, "bucket": bucket, "controlled_auto": controlled,
            "returns_pct": returns, "excess_pct": excess_returns,
            "return_status": return_status, "return_errors": return_errors,
            "return_exit_dates": return_dates, "return_calc_version": RETURN_CALC_VERSION,
        })

    market = _market_snapshot([str(row.get("code") or "") for row in details])
    quote_map = market.get("quotes") or {}
    for row in details:
        quote = quote_map.get(str(row.get("code") or "").upper(), {})
        latest_price = _valid_price(quote.get("latest_price"))
        selected_price = _valid_price(row.get("selected_price"))
        row.update({
            "latest_price": latest_price,
            "latest_chg_pct": _finite_float(quote.get("latest_chg_pct")),
            "latest_quote_time": quote.get("latest_quote_time"),
            "latest_trade_date": quote.get("latest_trade_date"),
            "quote_source": quote.get("quote_source"),
            "since_selection_pct": round((latest_price / selected_price - 1) * 100, 4)
            if latest_price is not None and selected_price is not None else None,
        })

    def summarize(values_by_horizon: dict[str, list[float]]) -> dict[str, Any]:
        output = {}
        for spec in HORIZON_SPECS:
            key = spec["key"]
            values = values_by_horizon.get(key, [])
            if values:
                output[key] = {
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
        reasons.append(f"当前契约30自然日成熟受控样本 {sample_count}，至少需要 {MIN_OPTIMIZATION_SAMPLES}")
    if distinct_dates < 10:
        reasons.append(f"样本仅覆盖 {distinct_dates} 个选股日，至少需要10个独立日期")
    if len(oos) < MIN_OOS_SAMPLES:
        reasons.append(f"时序样本外样本 {len(oos)}，至少需要 {MIN_OOS_SAMPLES}")
    if oos_avg is not None and oos_avg <= 0:
        reasons.append("时序样本外30自然日平均超额不为正")
    if oos_win is not None and oos_win <= 0.5:
        reasons.append("时序样本外30自然日超额胜率不高于50%")
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
        "horizons": [dict(spec) for spec in HORIZON_SPECS],
        "factor_contract": factor_config.model_contract("stock"),
        "total_selections": len(sels), "recomputed_samples": computed_calls,
        "backfilled_prices": backfilled_prices,
        "by_category_return": {key: summarize(value) for key, value in cat_returns.items()},
        "by_category_excess": {key: summarize(value) for key, value in cat_excess.items()},
        "auto_by_driver_excess": {key: summarize(value) for key, value in auto_by_driver.items()},
        "auto_by_bucket_return": {key: summarize(value) for key, value in auto_by_bucket.items()},
        "optimization_gate": gate, "sample_hash": sample_hash,
        "tuning_hints": tuning_hints, "details": details,
        "detail_total": len(details),
        "quote_refreshed_at": market.get("refreshed_at"),
        "quote_trade_date": market.get("quote_date"),
        "quote_errors": market.get("errors") or [],
        "note": "1/2/3日按交易日，7/30日按自然日目标后的首个交易日；仅受控auto样本可进入优化门禁。",
    }
    if p.get("save_snapshot", True):
        snapshot_payload = {key: value for key, value in result.items() if key != "details"}
        snapshot_payload["sample_ids"] = sample_identity
        result["snapshot_id"] = db.save_snapshot("selection", snapshot_payload)
    else:
        result["snapshot_id"] = None
    return result


@register("selection_dashboard", "review",
          "规范化选股看板：默认展示最近目标交易日及之前三个交易日；按日期/题材/类别查询，"
          "刷新实时行情、涨跌停标签并保留评分、因子契约与历史回测字段；"
          "管理员查看 watch/holding（或不限类别）时实时合并当前自选 portfolio_items，"
          "标注 live_portfolio 与「当前自选」标签，不受日期范围限制、不写入 selections、不参与回测",
          params=[{"name": "date_from", "type": "string", "required": False, "desc": "起始日期 YYYYMMDD；起止均省略时自动取最近四个交易日"},
                  {"name": "date_to", "type": "string", "required": False, "desc": "结束日期 YYYYMMDD"},
                  {"name": "hotspot", "type": "string", "required": False, "desc": "题材、板块、事件或标签关键词"},
                  {"name": "category", "type": "string", "required": False,
                   "desc": "auto|manual|watch|holding；省略为当前权限下全部，watch/holding 仅管理员"},
                  {"name": "limit", "type": "int", "required": False, "default": 200}],
          returns="rows / hotspots / selection_tag_version / refreshed_at / quote_errors / default_date_range")
def selection_dashboard(p: dict) -> dict:
    category = str(p.get("category") or "").strip()
    if category and category not in VALID_CATEGORIES:
        raise ParamError(f"category 须为 {sorted(VALID_CATEGORIES)}")
    try:
        default_applied = not p.get("date_from") and not p.get("date_to")
        if default_applied:
            date_from, date_to = _default_selection_range()
        else:
            date_from = _to_date(p["date_from"]) if p.get("date_from") else None
            date_to = _to_date(p["date_to"]) if p.get("date_to") else None
    except ValueError as exc:
        raise ParamError("date_from/date_to 必须是有效 YYYYMMDD") from exc
    if date_from and date_to and date_from > date_to:
        raise ParamError("date_from 不能晚于 date_to")

    records = db.fetch_selections(date_from, date_to, category or None)
    keyword = str(p.get("hotspot") or "").strip().casefold()
    if keyword:
        records = [row for row in records if keyword in " ".join([
            str((row.get("extra") or {}).get("hotspot", "")),
            str((row.get("extra") or {}).get("core_event", "")),
            str((row.get("extra") or {}).get("event", "")),
            str(row.get("reason", "")),
            " ".join(_record_tags(row.get("extra") or {}, str(row.get("driver") or ""))),
        ]).casefold()]
    try:
        limit = min(max(int(p.get("limit", 200)), 1), 1000)
    except (TypeError, ValueError) as exc:
        raise ParamError("limit 必须是 1 到 1000 的整数") from exc
    records = records[:limit]
    refreshed_records = []
    backfilled_prices = 0
    for record in records:
        updated, changed = _ensure_selection_price(record, "selection_dashboard")
        refreshed_records.append(updated)
        backfilled_prices += int(changed)
    records = refreshed_records

    # 方案 A：管理员查看关注/持仓（或不限类别）时，实时合并当前自选（portfolio_items），
    # 标注为「当前自选」与历史快照区分；这些行只用于看板展示，不写入 selections、不参与回测。
    live_portfolio_items: list[dict[str, Any]] = []
    if db.can_read_sensitive_selections():
        wanted_types = {"watch", "holding"} if not category else (
            {category} if category in {"watch", "holding"} else set())
        if wanted_types:
            try:
                portfolio = db.fetch_portfolio_items()
            except Exception:
                portfolio = {"rows": []}
            for item in (portfolio.get("rows") or []):
                if str(item.get("type") or "") not in wanted_types:
                    continue
                if keyword:
                    haystack = " ".join([
                        str(item.get("name") or ""), str(item.get("code") or ""),
                        str(item.get("note") or ""), "当前自选",
                        "持仓" if item.get("type") == "holding" else "关注",
                    ]).casefold()
                    if keyword not in haystack:
                        continue
                live_portfolio_items.append(item)

    snapshot_codes = [str(record.get("code") or "") for record in records]
    snapshot_codes += [str(item.get("code") or "") for item in live_portfolio_items]
    market = _market_snapshot(snapshot_codes)
    quote_map = market.get("quotes") or {}
    rows: list[dict[str, Any]] = []
    theme_counts: dict[str, int] = defaultdict(int)
    for record in records:
        extra = record.get("extra") or {}
        code = str(record.get("code", "")).upper()
        quote = quote_map.get(code, {})
        selected_price = _valid_price(extra.get("selected_price"))
        current_price = _valid_price(quote.get("latest_price"))
        since_return = (round((current_price / selected_price - 1) * 100, 2)
                        if selected_price is not None and current_price is not None else None)

        tags = [tag for tag in _record_tags(extra, str(record.get("driver") or ""))
                if tag not in selection_tags.AUTO_MARKET_TAGS]
        # 与 log_selection 一致：为服务端补充的涨停/跌停标签预留位置，避免用户标签占满 24 位后被截掉。
        market_tags = [tag for tag in (quote.get("market_tags") or []) if tag not in tags]
        tags = tags[:max(0, 24 - len(market_tags))] + market_tags
        legacy_hotspot = str(extra.get("hotspot") or "").strip()
        primary_theme = str(extra.get("primary_theme") or "").strip()
        if not primary_theme or primary_theme == "未分类":
            primary_theme = selection_tags.primary_theme(tags, legacy_hotspot)
        theme_counts[primary_theme] += 1

        rows.append({
            "id": record.get("id"), "date": str(record.get("sel_date")),
            "selected_at": extra.get("selected_at") or (
                record.get("logged_at").strftime("%Y-%m-%d %H:%M:%S")
                if record.get("logged_at") else None),
            "logged_at": record.get("logged_at").strftime("%Y-%m-%d %H:%M:%S")
            if record.get("logged_at") else None,
            "code": code, "name": record.get("name", ""),
            "category": record.get("category", ""), "score": float(record.get("score") or 0),
            "score_raw": extra.get("score_raw"),
            "score_percentile": extra.get("score_percentile"),
            "screening_rank": extra.get("screening_rank"),
            "driver": record.get("driver", ""),
            "tags": tags, "primary_theme": primary_theme,
            "hotspot": legacy_hotspot or primary_theme,
            "core_event": extra.get("core_event") or extra.get("event", ""),
            "event": extra.get("core_event") or extra.get("event", ""),
            "market_role": extra.get("market_role", ""),
            "reason": record.get("reason", ""),
            "selected_price": selected_price,
            "selected_price_date": extra.get("price_trade_date"),
            "selected_price_source": extra.get("price_source"),
            "price_backfilled_at": extra.get("price_backfilled_at"),
            "latest_price": current_price,
            "latest_chg_pct": quote.get("latest_chg_pct"),
            "latest_quote_time": quote.get("latest_quote_time"),
            "quote_source": quote.get("quote_source"),
            "market_tags": quote.get("market_tags") or [],
            "since_selection_pct": since_return,
            "turnover_rate": quote.get("turnover_rate"),
            "turnover_trade_date": quote.get("turnover_trade_date"),
            "amount": quote.get("amount"),
            "amount_trade_date": quote.get("amount_trade_date"),
            "factors": extra.get("factors") or {},
            "factor_contract": extra.get("factor_contract") or {},
            "factor_metadata": extra.get("factor_metadata") or {},
            "screening_run_id": extra.get("screening_run_id"),
            "screening_function": extra.get("screening_function"),
            "factor_error": extra.get("factor_error", ""),
            "trigger": extra.get("trigger", ""),
        })
    # 追加「当前自选」实时行：始终展示，不受日期范围限制；持仓以真实成本作为“选股后”基准。
    live_theme = "当前自选"
    live_date = str(market.get("quote_date") or common.now_str()[:10].replace("-", ""))
    for item in live_portfolio_items:
        code = str(item.get("code") or "").upper()
        quote = quote_map.get(code, {})
        is_holding = str(item.get("type")) == "holding"
        cost_price = _valid_price(item.get("cost_price")) if is_holding else None
        current_price = _valid_price(quote.get("latest_price"))
        since_return = (round((current_price / cost_price - 1) * 100, 2)
                        if cost_price is not None and current_price is not None else None)
        role_tag = "持仓" if is_holding else "关注"
        tags = [live_theme, role_tag]
        market_tags = [tag for tag in (quote.get("market_tags") or []) if tag not in tags]
        tags = tags[:max(0, 24 - len(market_tags))] + market_tags
        theme_counts[live_theme] += 1
        rows.append({
            "id": f"pf-{code}", "live_portfolio": True,
            "date": live_date,
            "selected_at": str(item.get("updated_at") or "") or None,
            "logged_at": str(item.get("updated_at") or "") or None,
            "code": code, "name": str(item.get("name") or ""),
            "category": str(item.get("type") or ""), "score": 0.0,
            "score_raw": None, "score_percentile": None, "screening_rank": None,
            "driver": "自选", "tags": tags, "primary_theme": live_theme,
            "hotspot": live_theme, "core_event": "", "event": "",
            "market_role": role_tag,
            "reason": str(item.get("note") or ("持仓成本 " + str(item.get("cost_price")) if is_holding else "")),
            "selected_price": cost_price,
            "selected_price_date": None,
            "selected_price_source": "portfolio_cost" if is_holding else None,
            "price_backfilled_at": None,
            "latest_price": current_price,
            "latest_chg_pct": quote.get("latest_chg_pct"),
            "latest_quote_time": quote.get("latest_quote_time"),
            "quote_source": quote.get("quote_source"),
            "market_tags": quote.get("market_tags") or [],
            "since_selection_pct": since_return,
            "turnover_rate": quote.get("turnover_rate"),
            "turnover_trade_date": quote.get("turnover_trade_date"),
            "amount": quote.get("amount"),
            "amount_trade_date": quote.get("amount_trade_date"),
            "factors": {}, "factor_contract": {}, "factor_metadata": {},
            "screening_run_id": None, "screening_function": None,
            "factor_error": "", "trigger": "portfolio_live",
            "lots": item.get("lots"), "shares": item.get("shares"),
        })

    hotspots = [{"name": name, "count": count} for name, count in sorted(
        theme_counts.items(), key=lambda item: (-item[1], item[0]))]
    return {"source": "selection_dashboard", "fetched_at": common.now_str(),
            "refreshed_at": market.get("refreshed_at"),
            "quote_trade_date": market.get("quote_date"),
            "quote_trade_date_min": market.get("quote_date_min"),
            "quote_trade_date_max": market.get("quote_date_max"),
            "mixed_quote_dates": bool(market.get("mixed_quote_dates")),
            "quote_errors": market.get("errors") or [],
            "selection_tag_version": selection_tags.TAG_VERSION,
            "date_from": str(date_from) if date_from else None,
            "date_to": str(date_to) if date_to else None,
            "default_date_range": default_applied,
            "group_by": "date" if keyword else "theme",
            "total": len(rows), "hotspots": hotspots, "rows": rows,
            "backfilled_prices": backfilled_prices,
            "note": "优先使用实时行情，失败时回退最近完成交易日收盘；缺失项保持为空，不做推断"}


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(selection_backtest({}), ensure_ascii=False, indent=2))

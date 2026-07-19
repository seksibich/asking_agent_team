"""服务端确定性量化盯盘引擎。

交易日连续竞价时按配置频率扫描；不调用 Agent、不写日终因子表，
盘中指标来自质量合格的实时快照序列，样本不足时明确标记不可用。
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

import common
import db
import notifications
import observability
from registry import ParamError, register

_CONFIG_KEY = "quant_watch_config"
_CONFIG_HISTORY_KEY = "quant_watch"
_ACTIVE_PHASES = {"morning", "afternoon"}
_VALID_BOARDS = {"main", "star", "gem"}
_OWNER = uuid.uuid4().hex
_STOP = threading.Event()
_WAKE = threading.Event()
_SCAN_LOCK = threading.Lock()
_CONDITION = threading.Condition()
_THREAD: Optional[threading.Thread] = None
_SEQUENCE = 0
_LATEST: dict[str, Any] = {}
# 最短30秒频率、最长30分钟窗口需要至少61个点；保留80个同阶段快照。
_SNAPSHOTS: deque[dict[str, Any]] = deque(maxlen=80)
_INDEX_SNAPSHOTS: deque[dict[str, Any]] = deque(maxlen=80)
_FILTER_CACHE: dict[str, Any] = {}
_FACTOR_CACHE: dict[str, Any] = {}
_SECTOR_CACHE: dict[str, Any] = {}
_LAST_CLEAN_DATE = ""
_STATUS_CACHE_TTL = 0.5
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _invalidate_status_cache() -> None:
    """清空短时状态缓存；调用方须先完成对应数据库提交。"""
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()


def _signal_status_change() -> None:
    """使状态缓存失效并唤醒唯一 WebSocket 广播任务。"""
    global _SEQUENCE
    _invalidate_status_cache()
    with _CONDITION:
        _SEQUENCE += 1
        _CONDITION.notify_all()
_CONFIG_ERROR: Optional[str] = None


def _env_interval() -> int:
    try:
        value = int(os.getenv("QUANT_WATCH_INTERVAL_SECONDS", "60"))
    except ValueError:
        value = 60
    return min(600, max(30, value))


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": _env_interval(),
    "universe_mode": "market",
    "boards": ["main", "gem"],
    "industries": [],
    "themes": [],
    "include_portfolio": True,
    "include_recent_selections": True,
    "selection_trade_days": 3,
    "window_minutes": 5,
    "qualified_score": 72.0,
    "max_candidates": 8,
    "priority_alert_pct": 1.5,
    "notify_enabled": False,
    "notify_channels": [],
}


def _terms(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else str(value).replace("，", ",").split(",")
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    if len(result) > 10 or any(len(item) > 40 for item in result):
        raise ParamError(f"{field} 最多 10 项且每项不超过 40 个字符")
    return result


def _normalize_config(value: Any) -> dict[str, Any]:
    raw = dict(value or {}) if isinstance(value, dict) else {}
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ParamError(f"未知盯盘配置项：{','.join(unknown)}")
    config = {**DEFAULT_CONFIG, **raw}
    for field in ("enabled", "include_portfolio", "include_recent_selections", "notify_enabled"):
        if not isinstance(config[field], bool):
            raise ParamError(f"{field} 必须是布尔值")
    try:
        config["interval_seconds"] = int(config["interval_seconds"])
        config["selection_trade_days"] = int(config["selection_trade_days"])
        config["window_minutes"] = int(config["window_minutes"])
        config["max_candidates"] = int(config["max_candidates"])
        config["qualified_score"] = float(config["qualified_score"])
        config["priority_alert_pct"] = float(config["priority_alert_pct"])
    except (TypeError, ValueError) as exc:
        raise ParamError("盯盘数值配置格式错误") from exc
    if not 30 <= config["interval_seconds"] <= 600:
        raise ParamError("interval_seconds 必须在 30 到 600 秒之间")
    if not 1 <= config["selection_trade_days"] <= 10:
        raise ParamError("selection_trade_days 必须在 1 到 10 之间")
    if not 1 <= config["window_minutes"] <= 30:
        raise ParamError("window_minutes 必须在 1 到 30 之间")
    if not 1 <= config["max_candidates"] <= 20:
        raise ParamError("max_candidates 必须在 1 到 20 之间")
    if not 0 <= config["qualified_score"] <= 100:
        raise ParamError("qualified_score 必须在 0 到 100 之间")
    if not 0.1 <= config["priority_alert_pct"] <= 20:
        raise ParamError("priority_alert_pct 必须在 0.1 到 20 之间")
    mode = str(config["universe_mode"]).strip().lower()
    if mode not in {"market", "priority_only"}:
        raise ParamError("universe_mode 仅支持 market 或 priority_only")
    config["universe_mode"] = mode
    boards = config["boards"]
    if not isinstance(boards, list) or not boards:
        raise ParamError("boards 必须是非空数组")
    boards = list(dict.fromkeys(str(item).strip().lower() for item in boards))
    invalid = sorted(set(boards) - _VALID_BOARDS)
    if invalid:
        raise ParamError(f"boards 仅支持 main、star、gem；非法值：{','.join(invalid)}")
    config["boards"] = boards
    config["industries"] = _terms(config.get("industries"), "industries")
    config["themes"] = _terms(config.get("themes"), "themes")
    # 产品合同：关注、持仓和最近三个交易日 auto/manual 选股始终必盯，不允许配置关闭。
    config["include_portfolio"] = True
    config["include_recent_selections"] = True
    config["selection_trade_days"] = 3
    channels = config.get("notify_channels")
    if not isinstance(channels, list):
        raise ParamError("notify_channels 必须是数组")
    channels = list(dict.fromkeys(str(item).strip().lower() for item in channels if str(item).strip()))
    invalid_channels = sorted(set(channels) - {"feishu", "wecom"})
    if invalid_channels:
        raise ParamError(f"不支持的通知渠道：{','.join(invalid_channels)}")
    config["notify_channels"] = channels
    return config


def get_config() -> dict[str, Any]:
    global _CONFIG_ERROR
    try:
        config = _normalize_config(db.get_config(_CONFIG_KEY))
        _CONFIG_ERROR = None
        return config
    except Exception as exc:
        _CONFIG_ERROR = f"{type(exc).__name__}: {exc}"[:500]
        return dict(DEFAULT_CONFIG)


def set_config(value: dict[str, Any], actor: str = "web-admin", reason: str = "") -> dict[str, Any]:
    current = get_config()
    config = _normalize_config({**current, **dict(value or {})})
    changed = config != current
    version = None
    if changed:
        db.set_config(_CONFIG_KEY, config)
        version = db.record_config_version(
            _CONFIG_HISTORY_KEY, config, actor, reason or "更新量化盯盘设置")
        _FILTER_CACHE.clear()
        _signal_status_change()
        _WAKE.set()
    return {"config": config, "changed": changed, "config_version": version,
            "notification_channels": notifications.available_channels()}


def _board_of(code: str) -> str:
    raw = str(code or "").strip().upper()
    number, _, exchange = raw.partition(".")
    if exchange == "BJ":
        return "bj"
    if number.startswith(("688", "689")):
        return "star"
    if number.startswith(("300", "301")):
        return "gem"
    if exchange in {"SH", "SZ"}:
        return "main"
    return "unknown"


def _recent_trade_date_range(days: int) -> tuple[date, date]:
    today = common.today_str()
    start = (datetime.strptime(today, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    frame = common.get_pro().trade_cal(exchange="SSE", start_date=start, end_date=today)
    open_days = sorted(frame[frame["is_open"].astype(int) == 1]["cal_date"].astype(str).tolist())
    selected = open_days[-days:] if open_days else [today]
    return datetime.strptime(selected[0], "%Y%m%d").date(), datetime.strptime(selected[-1], "%Y%m%d").date()


def _priority_stocks(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    priority: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if config["include_portfolio"]:
        try:
            for row in db.fetch_portfolio_items().get("rows") or []:
                code = str(row.get("code") or "").upper()
                if code:
                    priority[code] = {
                        "name": str(row.get("name") or ""),
                        "sources": ["持仓" if row.get("type") == "holding" else "关注"],
                        "portfolio_type": row.get("type"),
                        "cost_price": row.get("cost_price"),
                    }
        except Exception as exc:
            errors.append(f"关注持仓读取失败：{type(exc).__name__}: {exc}"[:240])
    if config["include_recent_selections"]:
        try:
            start, end = _recent_trade_date_range(3)
            records = []
            for category in ("auto", "manual"):
                records.extend(db.fetch_selections(start, end, category))
            for row in records:
                code = str(row.get("code") or "").upper()
                if not code:
                    continue
                item = priority.setdefault(code, {
                    "name": str(row.get("name") or ""), "sources": [],
                    "portfolio_type": None, "cost_price": None,
                })
                source = f"近{config['selection_trade_days']}日选股"
                if source not in item["sources"]:
                    item["sources"].append(source)
        except Exception as exc:
            errors.append(f"近期选股读取失败：{type(exc).__name__}: {exc}"[:240])
    return priority, errors


def _filter_codes(terms: list[str]) -> tuple[Optional[set[str]], Optional[str]]:
    if not terms:
        return None, None
    key = "|".join(item.casefold() for item in terms)
    cached = _FILTER_CACHE.get(key)
    if cached and time.monotonic() - cached["at"] < 1800:
        return set(cached["codes"]), cached.get("error")
    try:
        import screen_trend
        codes = set(screen_trend._industry_members(common.get_pro(), terms))
        error = None if codes else "板块/题材条件没有匹配到成分股"
    except Exception as exc:
        codes = set()
        error = f"板块/题材成分解析失败：{type(exc).__name__}: {exc}"[:300]
    _FILTER_CACHE[key] = {"at": time.monotonic(), "codes": sorted(codes), "error": error}
    return codes, error


def _prepare_quotes(snapshot: dict[str, Any], config: dict[str, Any],
                    priority: dict[str, dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    frame = pd.DataFrame(snapshot.get("rows") or [])
    required = {"TS_CODE", "NAME", "PRICE", "PCT_CHANGE", "OPEN", "HIGH", "LOW", "CLOSE", "AMOUNT"}
    if frame.empty or not required.issubset(frame.columns):
        raise RuntimeError("全市场快照为空或字段不完整")
    frame["TS_CODE"] = frame["TS_CODE"].fillna("").astype(str).str.upper().str.strip()
    for column in required - {"TS_CODE", "NAME"}:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        frame[column] = numeric.where(np.isfinite(numeric), np.nan)
    frame = frame[frame["PRICE"].gt(0) & frame["TS_CODE"].ne("")].drop_duplicates("TS_CODE")
    frame["BOARD"] = frame["TS_CODE"].map(_board_of)
    market = frame[frame["BOARD"].isin(set(config["boards"]))].copy()
    terms = config["industries"] + config["themes"]
    filter_codes, filter_error = _filter_codes(terms)
    if filter_codes is not None:
        market = market[market["TS_CODE"].isin(filter_codes)]
    if config["universe_mode"] == "priority_only":
        selected_codes = set(priority)
    else:
        selected_codes = set(market["TS_CODE"]) | set(priority)
    selected = frame[frame["TS_CODE"].isin(selected_codes)].copy()
    selected["IS_PRIORITY"] = selected["TS_CODE"].isin(priority)
    returned_codes = set(frame["TS_CODE"])
    missing_priority = sorted(set(priority) - returned_codes)
    return selected, frame, {
        "market_pool_count": len(market),
        "priority_count": len(priority),
        "expected_priority_codes": sorted(priority),
        "missing_priority_codes": missing_priority,
        "priority_complete": not missing_priority,
        "effective_count": len(selected),
        "filter_terms": terms,
        "filter_error": filter_error,
    }


def _daily_factor_scores() -> tuple[dict[str, float], Optional[str], Optional[str]]:
    """读取最近完整日因子并计算横截面分位；不现场补算。"""
    try:
        import factor_config
        import factor_contract
        import factors
        contract = factor_config.model_contract("stock")
        sector_contract = factor_config.model_contract("sector")
        dependencies = factor_contract.stock_data_dependencies(sector_contract)
        dependency_hash = factor_contract.fingerprint(dependencies)
        ready_date = str(common.market_clock()["last_data_ready_date"])
        usable_dates = db.usable_factor_date_counts(
            contract["factor_version"], contract["schema_hash"], dependency_hash, limit=30)
        trade_date = next((str(row["trade_date"]) for row in usable_dates
                           if str(row["trade_date"]) <= ready_date), None)
        if not trade_date:
            return {}, None, f"数据就绪日 {ready_date} 之前没有合格的完整日个股因子"
        cache_key = f"{trade_date}:{contract['schema_hash']}:{contract['weight_version']}"
        cached = _FACTOR_CACHE.get(cache_key)
        if cached:
            return dict(cached), trade_date, None
        records: list[dict[str, Any]] = []
        for row in db.fetch_daily_factors(trade_date):
            if (row.get("schema_hash") != contract["schema_hash"]
                    or row.get("dependency_hash") != dependency_hash):
                continue
            payload = dict(row.get("factors") or {})
            valid, _ = factor_contract.validate_payload("stock", payload)
            if not valid:
                continue
            payload.pop("_meta", None)
            payload["code"] = row["code"]
            records.append(payload)
        table = pd.DataFrame(records)
        if table.empty:
            return {}, trade_date, "完整日因子记录为空"
        table = factors.composite_score(table, factor_config.effective_weights("stock"), strict=True)
        scores = dict(zip(table["code"], (table["score_percentile"] * 100).round(3)))
        _FACTOR_CACHE.clear()
        _FACTOR_CACHE[cache_key] = scores
        return scores, trade_date, None
    except Exception as exc:
        return {}, None, f"完整日因子读取失败：{type(exc).__name__}: {exc}"[:300]


def _snapshot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["TS_CODE", "PRICE", "AMOUNT", "OPEN", "HIGH", "LOW", "PCT_CHANGE"]
    return frame[columns].drop_duplicates("TS_CODE").set_index("TS_CODE")


def _window_base(now: datetime, minutes: int, interval_seconds: int,
                 phase: str, source: str) -> tuple[Optional[pd.DataFrame], dict[str, Any]]:
    """只在同一连续竞价阶段、同一行情源内寻找接近目标窗口的基准。"""
    target_seconds = minutes * 60
    candidates = []
    for item in _SNAPSHOTS:
        age = (now - item["at"]).total_seconds()
        if (age > 0 and item["phase"] == phase and item["source"] == source
                and item.get("interval_seconds") == interval_seconds):
            candidates.append((abs(age - target_seconds), age, item))
    if not candidates:
        return None, {"status": "warming", "reason": "同交易阶段暂无历史快照"}
    _, actual_seconds, selected = min(candidates, key=lambda value: value[0])
    min_age = max(20.0, target_seconds * 0.80)
    max_age = target_seconds + max(90.0, interval_seconds * 1.5)
    if not min_age <= actual_seconds <= max_age:
        return None, {
            "status": "warming", "reason": "历史快照未落入目标窗口容差",
            "actual_window_seconds": round(actual_seconds, 1),
        }
    return selected["frame"], {
        "status": "available", "actual_window_seconds": round(actual_seconds, 1),
        "base_quote_time": selected["quote_time"], "base_source": selected["source"],
    }


def _rank_score(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    numeric = numeric.where(np.isfinite(numeric), np.nan)
    return numeric.rank(method="average", pct=True).mul(100)


def _technical_metrics(code: str, phase: str, source: str,
                       interval_seconds: int) -> dict[str, Any]:
    """仅使用同交易阶段、同行情源、同采样频率的成功快照计算分时代理。"""
    prices: list[float] = []
    for item in _SNAPSHOTS:
        frame = item["frame"]
        if (item["phase"] == phase and item["source"] == source
                and item.get("interval_seconds") == interval_seconds and code in frame.index):
            value = frame.at[code, "PRICE"]
            if pd.notna(value) and math.isfinite(float(value)) and float(value) > 0:
                prices.append(float(value))
    result: dict[str, Any] = {
        "sample_count": len(prices), "macd": None, "macd_score": None,
        "kdj_k": None, "kdj_d": None, "kdj_j": None, "kdj_score": None,
        "support_proxy": None, "support_score": None,
    }
    if len(prices) >= 13:
        values = pd.Series(prices, dtype=float)
        fast = values.ewm(span=6, adjust=False).mean()
        slow = values.ewm(span=13, adjust=False).mean()
        diff = fast - slow
        dea = diff.ewm(span=5, adjust=False).mean()
        hist = float((diff - dea).iloc[-1])
        prev_hist = float((diff - dea).iloc[-2])
        result["macd"] = round(hist, 6)
        result["macd_score"] = float(np.clip(50 + (18 if hist > 0 else -18)
                                                      + (12 if hist > prev_hist else -8), 0, 100))
    if len(prices) >= 9:
        values = pd.Series(prices, dtype=float)
        lows = values.rolling(9).min()
        highs = values.rolling(9).max()
        rsv = ((values - lows) / (highs - lows).replace(0, np.nan) * 100).fillna(50)
        k_series = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d_series = k_series.ewm(alpha=1 / 3, adjust=False).mean()
        k_value = float(k_series.iloc[-1])
        d_value = float(d_series.iloc[-1])
        j_value = 3 * k_value - 2 * d_value
        rising = k_value >= float(k_series.iloc[-2]) and k_value >= d_value
        result.update({
            "kdj_k": round(k_value, 2), "kdj_d": round(d_value, 2),
            "kdj_j": round(j_value, 2),
            "kdj_score": float(np.clip(50 + (18 if rising else -10)
                                           + (8 if 20 <= k_value <= 85 else -5), 0, 100)),
        })
        recent = values.tail(min(12, len(values)))
        low, high, current = float(recent.min()), float(recent.max()), float(recent.iloc[-1])
        recovery = (current - low) / (high - low) if high > low else 0.5
        slope = current / float(recent.iloc[0]) - 1 if recent.iloc[0] else 0.0
        support = float(np.clip(recovery * 70 + (20 if slope >= 0 else 5), 0, 100))
        result["support_proxy"] = round(recovery, 4)
        result["support_score"] = round(support, 2)
    return result


def _full_sw_members() -> tuple[dict[str, list[dict[str, str]]], dict[str, Any]]:
    """优先读取一行含 L1/L2/L3 的全量映射；失败时仅回退可靠的 L1。"""
    today = common.today_str()
    cached = _SECTOR_CACHE.get(today)
    if cached:
        return cached["members"], cached["quality"]
    pro = common.get_pro()
    errors: list[str] = []
    for endpoint in ("sw_member", "index_member_all"):
        try:
            payload = common.cached_call(
                f"quant_watch_{endpoint}", {"date": today},
                lambda endpoint=endpoint: getattr(pro, endpoint)(), use_cache=True)
            frame = pd.DataFrame(payload.get("rows") or [])
            code_col = next((name for name in ("ts_code", "con_code", "code") if name in frame.columns), None)
            level_columns = [(level, f"{level.lower()}_code", f"{level.lower()}_name")
                             for level in ("L1", "L2", "L3")]
            if not code_col or len(frame) < 3000 or not any(code in frame.columns for _, code, _ in level_columns):
                raise RuntimeError("返回未达到全量分级映射门槛")
            members: dict[str, list[dict[str, str]]] = {}
            levels: set[str] = set()
            for row in frame.to_dict(orient="records"):
                code = str(row.get(code_col) or "").upper()
                if not code:
                    continue
                items = []
                for level, level_code, level_name in level_columns:
                    sector_code = str(row.get(level_code) or "").strip()
                    if sector_code:
                        levels.add(level)
                        items.append({"level": level, "code": sector_code,
                                      "name": str(row.get(level_name) or sector_code).strip()})
                if items:
                    members[code] = items
            if len(members) < 3000:
                raise RuntimeError("有效股票分级映射不足 3000")
            quality = {"status": "available", "source": endpoint,
                       "levels": sorted(levels), "stock_count": len(members), "errors": errors}
            _SECTOR_CACHE.clear()
            _SECTOR_CACHE[today] = {"members": members, "quality": quality}
            return members, quality
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}"[:220])
    members: dict[str, list[dict[str, str]]] = {}
    try:
        import screen_sector
        sectors = screen_sector._sw_l1_industries(pro)
        for sector in sectors:
            codes = screen_sector._active_sector_members(pro, sector["code"], today)
            item = {"level": "L1", "code": str(sector["code"]), "name": str(sector["name"])}
            for code in codes:
                members.setdefault(code, []).append(item)
        quality = {
            "status": "partial" if members else "unavailable",
            "source": "index_classify+index_member", "levels": ["L1"] if members else [],
            "stock_count": len(members), "errors": errors,
            "reason": "当前数据源未返回可靠的申万全分级映射，仅使用申万一级；二三级不参与评分",
        }
    except Exception as exc:
        errors.append(f"L1 fallback: {type(exc).__name__}: {exc}"[:220])
        quality = {"status": "unavailable", "source": None, "levels": [],
                   "stock_count": 0, "errors": errors,
                   "reason": "申万行业成分映射不可用，行业项不参与本轮个股评分"}
    _SECTOR_CACHE.clear()
    _SECTOR_CACHE[today] = {"members": members, "quality": quality}
    return members, quality


def _sector_metrics(frame: pd.DataFrame, memberships: dict[str, list[dict[str, str]]],
                    index_window: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """计算行业窗口指标；任何非有限值都不得进入排名或 JSON。"""
    if frame.empty or not memberships:
        return [], {}
    buckets: dict[tuple[str, str, str], list[int]] = {}
    codes = frame["TS_CODE"].tolist()
    for position, code in enumerate(codes):
        for item in memberships.get(code, []):
            key = (item["level"], item["code"], item["name"])
            buckets.setdefault(key, []).append(position)
    rows: list[dict[str, Any]] = []
    for (level, sector_code, name), positions in buckets.items():
        part = frame.iloc[positions].copy()
        speed = pd.to_numeric(part["SPEED_PCT"], errors="coerce")
        window_amount = pd.to_numeric(part["WINDOW_AMOUNT"], errors="coerce")
        total_amount = pd.to_numeric(part["AMOUNT"], errors="coerce")
        valid_mask = np.isfinite(speed)
        valid = part.loc[valid_mask].copy()
        if len(part) < 5 or len(valid) < 3:
            continue
        valid_speed = speed.loc[valid_mask].astype(float)
        valid_weights = window_amount.loc[valid_mask].where(
            np.isfinite(window_amount.loc[valid_mask]), 0).clip(lower=0).astype(float)
        speed_value = (float(np.average(valid_speed, weights=valid_weights))
                       if float(valid_weights.sum()) > 0 else float(valid_speed.mean()))
        window_total = float(window_amount.where(np.isfinite(window_amount), 0).clip(lower=0).sum())
        amount_total = float(total_amount.where(np.isfinite(total_amount), 0).clip(lower=0).sum())
        if not all(math.isfinite(value) for value in (speed_value, window_total, amount_total)):
            continue
        rows.append({
            "level": level, "code": sector_code, "name": name,
            "speed_pct": round(speed_value, 4),
            "window_amount": round(window_total, 2),
            "total_amount": round(amount_total, 2),
            "member_count": len(part), "sample_count": len(valid),
        })
    if not rows:
        return [], {}
    result = pd.DataFrame(rows)
    result["SPEED_SCORE"] = result.groupby("level")["speed_pct"].rank(pct=True).mul(100)
    result["AMOUNT_SCORE"] = result.groupby("level")["window_amount"].rank(pct=True).mul(100)
    result["score"] = result["SPEED_SCORE"] * 0.62 + result["AMOUNT_SCORE"] * 0.38
    speeds = pd.to_numeric(frame["SPEED_PCT"], errors="coerce")
    finite_speeds = speeds[np.isfinite(speeds)]
    market_speed = float(finite_speeds.mean()) if len(finite_speeds) else None
    result["market_sync"] = (np.where(
        np.sign(result["speed_pct"]) == np.sign(market_speed), "同步", "背离")
        if market_speed is not None and math.isfinite(market_speed) else None)
    finite_index = [float(value) for value in index_window.values()
                    if _finite_number(value) is not None]
    if finite_index:
        index_speed = float(np.mean(finite_index))
        result["index_sync"] = np.where(
            np.sign(result["speed_pct"]) == np.sign(index_speed), "同步", "背离")
    else:
        result["index_sync"] = None
    public = _json_safe(result.sort_values(
        ["level", "score"], ascending=[True, False]).round(4).to_dict(orient="records"))
    score_by_stock: dict[str, float] = {}
    sector_score_map = {
        (row["level"], row["code"]): float(row["score"])
        for row in public if _finite_number(row.get("score")) is not None
    }
    level_weights = {"L1": 0.25, "L2": 0.35, "L3": 0.40}
    for code, items in memberships.items():
        values = [(sector_score_map.get((item["level"], item["code"])), level_weights[item["level"]])
                  for item in items if item["level"] in level_weights]
        values = [(value, weight) for value, weight in values
                  if value is not None and math.isfinite(float(value))]
        if values:
            score_by_stock[code] = sum(value * weight for value, weight in values) / sum(
                weight for _, weight in values)
    return public, score_by_stock


def _index_prices(now: datetime, window_minutes: int, interval_seconds: int,
                  phase: str, scan_id: str) -> tuple[dict[str, Any], dict[str, float]]:
    names = {"000001.SH": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指"}
    current: dict[str, float] = {}
    errors: list[str] = []
    try:
        import tushare as ts
        frame = ts.realtime_quote(ts_code=",".join(names))
        for row in (frame.to_dict(orient="records") if frame is not None else []):
            code = str(row.get("TS_CODE") or row.get("ts_code") or "").upper()
            price = pd.to_numeric(row.get("PRICE") or row.get("price"), errors="coerce")
            if code in names and pd.notna(price) and math.isfinite(float(price)) and float(price) > 0:
                current[code] = float(price)
    except Exception as exc:
        errors.append(f"指数实时行情失败：{type(exc).__name__}: {exc}"[:240])
    target_seconds = window_minutes * 60
    candidates = []
    for item in _INDEX_SNAPSHOTS:
        age = (now - item["at"]).total_seconds()
        if (age > 0 and item["phase"] == phase
                and item.get("interval_seconds") == interval_seconds):
            candidates.append((abs(age - target_seconds), age, item))
    base: dict[str, float] = {}
    actual_window = None
    if candidates:
        _, age, item = min(candidates, key=lambda value: value[0])
        if max(20.0, target_seconds * 0.80) <= age <= target_seconds + max(90.0, interval_seconds * 1.5):
            base = item["prices"]
            actual_window = round(age, 1)
    if current:
        _INDEX_SNAPSHOTS.append({
            "at": now, "phase": phase, "interval_seconds": interval_seconds,
            "scan_id": scan_id, "prices": current,
        })
    speeds = {code: (price / base[code] - 1) * 100 for code, price in current.items()
              if code in base and base[code] > 0}
    rows = [{"code": code, "name": names[code], "price": round(price, 3),
             "window_speed_pct": round(speeds[code], 4) if code in speeds else None}
            for code, price in current.items()]
    return {
        "status": "available" if current else "unavailable",
        "window_status": "available" if speeds else "warming",
        "rows": rows, "errors": errors, "window_minutes": window_minutes,
        "actual_window_seconds": actual_window,
    }, speeds


def _finite_number(value: Any, digits: Optional[int] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _json_safe(value: Any) -> Any:
    """递归移除 NaN/Infinity 和 numpy 标量，保证 MySQL JSON 与标准 JSON 可接受。"""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _public_stock(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "code", "name", "board", "price", "pct_change", "speed_pct",
        "window_amount", "total_amount", "score", "score_coverage",
        "daily_factor_score", "daily_k_score", "macd", "macd_score",
        "kdj_k", "kdj_d", "kdj_j", "kdj_score", "support_proxy",
        "support_score", "sector_score", "is_priority", "priority_sources",
        "breakout", "breakdown", "large_order_status", "reason",
    )
    return {key: row.get(key) for key in keys}


def _score_stocks(frame: pd.DataFrame, priority: dict[str, dict[str, Any]],
                  factor_scores: dict[str, float], sector_scores: dict[str, float],
                  config: dict[str, Any], phase: str,
                  source: str) -> list[dict[str, Any]]:
    """按实际可用指标重新归一评分；缺失窗口或形态不以中性值计入最终分。"""
    if frame.empty:
        return []
    frame = frame.copy()
    frame["SPEED_SCORE"] = _rank_score(frame["SPEED_PCT"])
    frame["WINDOW_AMOUNT_SCORE"] = _rank_score(frame["WINDOW_AMOUNT"])
    frame["TOTAL_AMOUNT_SCORE"] = _rank_score(frame["AMOUNT"])
    frame["DAILY_FACTOR_SCORE"] = frame["TS_CODE"].map(factor_scores)
    frame["SECTOR_SCORE"] = frame["TS_CODE"].map(sector_scores)
    numeric_columns = ["PRICE", "OPEN", "HIGH", "LOW", "PCT_CHANGE"]
    valid_ohlc = pd.Series(True, index=frame.index)
    for column in numeric_columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        valid_ohlc &= np.isfinite(values)
    valid_ohlc &= (frame["PRICE"] > 0) & (frame["OPEN"] > 0)
    valid_ohlc &= (frame["HIGH"] >= frame[["PRICE", "OPEN"]].max(axis=1))
    valid_ohlc &= (frame["LOW"] <= frame[["PRICE", "OPEN"]].min(axis=1))
    valid_ohlc &= frame["HIGH"] >= frame["LOW"]
    frame["DAILY_K_SCORE"] = np.nan
    price_range = (frame.loc[valid_ohlc, "HIGH"] - frame.loc[valid_ohlc, "LOW"]).replace(0, np.nan)
    range_position = ((frame.loc[valid_ohlc, "PRICE"] - frame.loc[valid_ohlc, "LOW"])
                      / price_range).clip(0, 1)
    body_pct = ((frame.loc[valid_ohlc, "PRICE"]
                 / frame.loc[valid_ohlc, "OPEN"].replace(0, np.nan) - 1) * 100)
    frame.loc[valid_ohlc, "DAILY_K_SCORE"] = (
        50 + range_position.sub(0.5).mul(35) + body_pct.clip(-5, 5).mul(4)
    ).clip(0, 100)
    frame["BREAKOUT"] = valid_ohlc & (frame["PRICE"] >= frame["HIGH"] * 0.999) & frame["PCT_CHANGE"].gt(1)
    frame["BREAKDOWN"] = valid_ohlc & (frame["PRICE"] <= frame["LOW"] * 1.001) & frame["PCT_CHANGE"].lt(-1)
    # 预选只用于控制分时指标计算量，允许暂时以 50 作为排序中性值；最终评分绝不填补缺失。
    preliminary = (
        frame["SPEED_SCORE"].fillna(50) * 0.32
        + frame["WINDOW_AMOUNT_SCORE"].fillna(50) * 0.27
        + frame["TOTAL_AMOUNT_SCORE"].fillna(50) * 0.10
        + frame["DAILY_K_SCORE"].fillna(50) * 0.13
        + frame["DAILY_FACTOR_SCORE"].fillna(50) * 0.18
    )
    detail_codes = set(frame.assign(PRELIMINARY=preliminary).nlargest(250, "PRELIMINARY")["TS_CODE"])
    detail_codes.update(priority)
    weights = {
        "daily_factor_score": 0.20, "daily_k_score": 0.10,
        "speed_score": 0.15, "window_amount_score": 0.15,
        "total_amount_score": 0.08, "macd_score": 0.08,
        "kdj_score": 0.05, "support_score": 0.07, "sector_score": 0.12,
    }
    labels = {
        "daily_factor_score": "完整日因子", "daily_k_score": "当日K线",
        "speed_score": "窗口涨速", "window_amount_score": "窗口成交额",
        "total_amount_score": "当日成交额", "macd_score": "分时MACD",
        "kdj_score": "分时KDJ", "support_score": "承接代理",
        "sector_score": "行业轮动",
    }
    records: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        code = str(raw["TS_CODE"])
        technical = (_technical_metrics(
            code, phase, source, config["interval_seconds"])
            if code in detail_codes else {})
        components = {
            "daily_factor_score": raw.get("DAILY_FACTOR_SCORE"),
            "daily_k_score": raw.get("DAILY_K_SCORE"),
            "speed_score": raw.get("SPEED_SCORE"),
            "window_amount_score": raw.get("WINDOW_AMOUNT_SCORE"),
            "total_amount_score": raw.get("TOTAL_AMOUNT_SCORE"),
            "macd_score": technical.get("macd_score"),
            "kdj_score": technical.get("kdj_score"),
            "support_score": technical.get("support_score"),
            "sector_score": raw.get("SECTOR_SCORE"),
        }
        available = [(float(value), weights[name]) for name, value in components.items()
                     if _finite_number(value) is not None]
        available_weight = sum(weight for _, weight in available)
        score = (sum(value * weight for value, weight in available) / available_weight
                 if available_weight else None)
        top_components = sorted(
            ((name, float(value)) for name, value in components.items()
             if _finite_number(value) is not None), key=lambda item: item[1], reverse=True)[:3]
        priority_item = priority.get(code) or {}
        record = {
            "code": code,
            "name": str(raw.get("NAME") or priority_item.get("name") or ""),
            "board": raw.get("BOARD"),
            "price": _finite_number(raw.get("PRICE"), 3),
            "pct_change": _finite_number(raw.get("PCT_CHANGE"), 3),
            "speed_pct": _finite_number(raw.get("SPEED_PCT"), 4),
            "window_amount": _finite_number(raw.get("WINDOW_AMOUNT"), 2),
            "total_amount": _finite_number(raw.get("AMOUNT"), 2),
            "score": _finite_number(score, 2),
            "score_coverage": round(available_weight, 3),
            "daily_factor_score": _finite_number(raw.get("DAILY_FACTOR_SCORE"), 2),
            "daily_k_score": _finite_number(raw.get("DAILY_K_SCORE"), 2),
            "sector_score": _finite_number(raw.get("SECTOR_SCORE"), 2),
            "is_priority": code in priority,
            "priority_sources": list(priority_item.get("sources") or []),
            "breakout": bool(raw.get("BREAKOUT")),
            "breakdown": bool(raw.get("BREAKDOWN")),
            "large_order_status": "unavailable",
            "reason": "、".join(labels[name] for name, value in top_components if value >= 55)
                      or "当前有效指标未形成明显共振",
            **technical,
        }
        records.append(_json_safe(record))
    return records


def _add_window_metrics(frame: pd.DataFrame, base: Optional[pd.DataFrame]) -> pd.DataFrame:
    result = frame.copy()
    if base is None or base.empty:
        result["SPEED_PCT"] = np.nan
        result["WINDOW_AMOUNT"] = np.nan
        return result
    aligned = base.reindex(result["TS_CODE"])
    base_price = pd.to_numeric(aligned["PRICE"], errors="coerce").to_numpy()
    base_amount = pd.to_numeric(aligned["AMOUNT"], errors="coerce").to_numpy()
    current_price = result["PRICE"].to_numpy(dtype=float)
    current_amount = result["AMOUNT"].to_numpy(dtype=float)
    result["SPEED_PCT"] = np.where(base_price > 0, (current_price / base_price - 1) * 100, np.nan)
    result["WINDOW_AMOUNT"] = np.where(
        np.isfinite(base_amount), np.maximum(current_amount - base_amount, 0), np.nan)
    return result


def _sector_rotation(rows: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
    previous_payload = (_LATEST.get("payload") or {}) if _LATEST else {}
    previous_rows = (previous_payload.get("sectors") or []) if (
        str(previous_payload.get("trade_date") or "") == str(trade_date)) else []
    previous_rank: dict[tuple[str, str], int] = {}
    for level in ("L1", "L2", "L3"):
        ordered = sorted((row for row in previous_rows if row.get("level") == level
                          and _finite_number(row.get("score")) is not None),
                         key=lambda row: float(row["score"]), reverse=True)
        previous_rank.update({(level, row["code"]): rank for rank, row in enumerate(ordered, 1)})
    top_by_level: dict[str, list[dict[str, Any]]] = {}
    movers: list[dict[str, Any]] = []
    for level in ("L1", "L2", "L3"):
        ordered = sorted((row for row in rows if row.get("level") == level
                          and _finite_number(row.get("score")) is not None),
                         key=lambda row: float(row["score"]), reverse=True)
        top_by_level[level] = ordered[:8]
        for rank, row in enumerate(ordered[:20], 1):
            old = previous_rank.get((level, row["code"]))
            if old is not None and old - rank >= 5:
                movers.append({"level": level, "code": row["code"], "name": row["name"],
                               "rank": rank, "previous_rank": old, "rank_change": old - rank})
    return {
        "top_by_level": top_by_level,
        "fast_risers": sorted(movers, key=lambda row: row["rank_change"], reverse=True)[:12],
        "divergent_sectors": [row for row in rows if row.get("index_sync") == "背离"][:20],
    }


def _top(records: list[dict[str, Any]], key: str, count: int,
         reverse: bool = True, predicate=None) -> list[dict[str, Any]]:
    rows = [row for row in records if _finite_number(row.get(key)) is not None
            and (predicate(row) if predicate else True)]
    return [_public_stock(row) for row in sorted(
        rows, key=lambda row: float(row[key]), reverse=reverse)[:count]]


def _notify(payload: dict[str, Any], config: dict[str, Any],
            owner_id: str, fence_token: int) -> dict[str, Any]:
    """按渠道幂等发送优先异动；失败或认领超时最多重试三次。"""
    channel_state = notifications.available_channels()
    if not config["notify_enabled"] or not config["notify_channels"]:
        return {"enabled": False, "sent": 0, "channels": channel_state}
    if not payload.get("universe", {}).get("priority_complete"):
        return {
            "enabled": True, "sent": 0, "channels": channel_state,
            "reason": "优先标的覆盖不完整，本轮禁止发送通知",
        }
    if not db.quant_watch_scan_owned(owner_id, fence_token):
        raise RuntimeError("通知前扫描租约已失效")
    alerts = payload.get("priority_alerts") or []
    claimed_by_channel: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for channel in config["notify_channels"]:
        claimed: list[tuple[str, str, dict[str, Any]]] = []
        for row in alerts[:10]:
            event = "突破" if row.get("breakout") else "下破" if row.get("breakdown") else "涨速异动"
            event_key = f"{payload['trade_date']}:{row['code']}:{event}:{channel}"
            result = db.claim_quant_watch_notification_event(
                event_key, payload["trade_date"], owner_id, fence_token)
            if result.get("claimed"):
                claimed.append((event_key, event, row))
        if claimed:
            claimed_by_channel[channel] = claimed
    if not claimed_by_channel:
        return {
            "enabled": True, "sent": 0, "channels": channel_state,
            "reason": "本轮通知事件已处理或已达重试上限",
        }
    results: dict[str, Any] = {}
    success_events = 0
    attempted_events = 0
    for channel, claimed in claimed_by_channel.items():
        lines = [f"量化盯盘 {payload['scanned_at']}（仅为异动提示，不构成交易建议）"]
        for _, event, row in claimed:
            lines.append(
                f"{event}｜{row['name']} {row['code']}｜涨速 {row.get('speed_pct')}%｜评分 {row.get('score')}")
        try:
            channel_result = _json_safe(
                notifications.send_text([channel], "\n".join(lines)).get(channel) or {
                    "ok": False, "error": "通知渠道未返回结果",
                })
        except Exception as exc:
            channel_result = {"ok": False, "error": f"通知处理失败：{type(exc).__name__}"}
        final_status = "success" if channel_result.get("ok") else "failed"
        attempted_events += len(claimed)
        if final_status == "success":
            success_events += len(claimed)
        results[channel] = channel_result
        event_result = {"channel": channel, "result": channel_result}
        for event_key, _, _ in claimed:
            if not db.save_quant_watch_notification_result(
                    event_key, owner_id, fence_token, final_status, event_result):
                raise RuntimeError("通知结果写入前扫描租约已失效")
    outcomes = [bool(item.get("ok")) for item in results.values()]
    return {
        "enabled": True,
        "status": "success" if all(outcomes) else "partial" if any(outcomes) else "failed",
        "sent": success_events, "attempted": attempted_events,
        "results": results, "channels": channel_state,
    }


def _rollback_scan_samples(scan_id: str) -> None:
    """扫描未形成有效聚合消息时，移除本轮股票与指数内存样本。"""
    if not scan_id:
        return
    stock_items = [item for item in _SNAPSHOTS if item.get("scan_id") != scan_id]
    index_items = [item for item in _INDEX_SNAPSHOTS if item.get("scan_id") != scan_id]
    _SNAPSHOTS.clear()
    _SNAPSHOTS.extend(stock_items)
    _INDEX_SNAPSHOTS.clear()
    _INDEX_SNAPSHOTS.extend(index_items)


def scan_once(config: Optional[dict[str, Any]] = None, manual: bool = False) -> dict[str, Any]:
    """经数据库原子认领执行一轮扫描；自动与手动入口共用同一 fencing 门禁。"""
    if not _SCAN_LOCK.acquire(blocking=False):
        message = "当前实例已有扫描正在执行，请稍后重试"
        if manual:
            raise ParamError(message)
        return {"source": "quant_watch", "claimed": False,
                "reason": "local_scan_active", "message": message}
    owner_claimed = False
    fence_token = -1
    scan_id = ""
    samples_committed = False
    try:
        resolved = _normalize_config(config or get_config())
        if _CONFIG_ERROR:
            raise ParamError(f"量化盯盘配置不可用：{_CONFIG_ERROR}")
    except Exception:
        _SCAN_LOCK.release()
        raise
    lease_seconds = max(300, resolved["interval_seconds"] * 4)
    try:
        clock = common.market_clock()
        phase = str(clock.get("phase") or "")
        if phase not in _ACTIVE_PHASES:
            raise ParamError(f"当前市场阶段 {phase} 不执行量化盯盘扫描")
        claim = db.claim_quant_watch_scan(
            _OWNER, lease_seconds, resolved["interval_seconds"], manual=manual)
        if not claim.get("claimed"):
            if manual:
                raise ParamError(str(claim.get("message") or "本轮扫描未获得执行权"))
            return {"source": "quant_watch", **claim}
        owner_claimed = True
        fence_token = int(claim["fence_token"])

        def renew_or_fail(stage: str) -> None:
            if not db.renew_quant_watch_lease(_OWNER, fence_token, lease_seconds):
                raise RuntimeError(f"{stage}前扫描租约已失效")

        now = common.shanghai_now()
        import market_data
        snapshot = market_data.fetch_realtime_market_snapshot()
        renew_or_fail("处理实时快照")
        coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
        if (snapshot.get("degraded") is True or coverage.get("validation_passed") is not True
                or snapshot.get("quote_date") != common.today_str()):
            errors = snapshot.get("errors") or ["实时快照质量门禁未通过"]
            raise RuntimeError("；".join(str(item) for item in errors[:3]))
        quote_source = str(snapshot.get("source") or "unknown")
        quote_time = str(snapshot.get("as_of") or snapshot.get("quote_time") or "")
        if quote_time and any(
                item.get("phase") == phase and item.get("source") == quote_source
                and item.get("quote_time") == quote_time for item in _SNAPSHOTS):
            db.update_quant_watch_state(
                _OWNER, fence_token, status="waiting", trade_date=common.today_str(),
                last_error=None, phase=phase)
            return {
                "source": "quant_watch", "claimed": True, "status": "skipped",
                "reason": "quote_not_updated",
                "message": f"实时行情尚未更新，本轮跳过：{quote_time}",
                "trade_date": common.today_str(), "market_phase": phase,
            }

        scan_id = uuid.uuid4().hex
        priority, priority_errors = _priority_stocks(resolved)
        selected, full_market, universe = _prepare_quotes(snapshot, resolved, priority)
        universe["priority_complete"] = bool(
            not priority_errors and not universe.get("missing_priority_codes"))
        scan_status = "success" if universe["priority_complete"] else "degraded"
        if selected.empty:
            raise RuntimeError("当前配置的有效扫描股票池为空")
        base_frame, window_meta = _window_base(
            now, resolved["window_minutes"], resolved["interval_seconds"],
            phase, quote_source)
        full_market = _add_window_metrics(full_market, base_frame)
        selected = _add_window_metrics(selected, base_frame)
        current_snapshot = _snapshot_frame(full_market)
        _SNAPSHOTS.append({
            "at": now, "phase": phase, "source": quote_source,
            "quote_time": quote_time, "interval_seconds": resolved["interval_seconds"],
            "scan_id": scan_id, "frame": current_snapshot,
        })
        index_status, index_speeds = _index_prices(
            now, resolved["window_minutes"], resolved["interval_seconds"], phase, scan_id)
        factor_scores, factor_date, factor_error = _daily_factor_scores()
        renew_or_fail("计算行业轮动")
        memberships, sector_quality = _full_sw_members()
        sector_rows, sector_scores = _sector_metrics(full_market, memberships, index_speeds)
        renew_or_fail("完成行业轮动")
        records = _score_stocks(
            selected, priority, factor_scores, sector_scores, resolved, phase, quote_source)
        reliable_window = window_meta.get("status") == "available"
        qualified = [row for row in records
                     if reliable_window
                     and _finite_number(row.get("score")) is not None
                     and float(row["score"]) >= resolved["qualified_score"]
                     and float(row.get("score_coverage") or 0) >= 0.60
                     and _finite_number(row.get("speed_pct")) is not None
                     and _finite_number(row.get("window_amount")) is not None]
        qualified.sort(key=lambda row: float(row["score"]), reverse=True)
        alerts = [row for row in records if row["is_priority"] and (
            row["breakout"] or row["breakdown"]
            or (_finite_number(row.get("speed_pct")) is not None
                and abs(float(row["speed_pct"])) >= resolved["priority_alert_pct"]))]
        alerts.sort(key=lambda row: (
            abs(float(row["speed_pct"])) if _finite_number(row.get("speed_pct")) is not None else 0,
            float(row["score"]) if _finite_number(row.get("score")) is not None else 0,
        ), reverse=True)
        speed_values = pd.Series([
            float(row["speed_pct"]) for row in records
            if _finite_number(row.get("speed_pct")) is not None
        ], dtype=float)
        up_cut = float(speed_values.quantile(0.99)) if len(speed_values) >= 100 else 1.0
        down_cut = float(speed_values.quantile(0.01)) if len(speed_values) >= 100 else -1.0
        anomalies = [row for row in records
                     if _finite_number(row.get("speed_pct")) is not None
                     and (float(row["speed_pct"]) >= max(up_cut, 0.8)
                          or float(row["speed_pct"]) <= min(down_cut, -0.8))]
        trade_date = common.today_str()
        phase_samples = sum(
            1 for item in _SNAPSHOTS
            if (item.get("phase") == phase and item.get("source") == quote_source
                and item.get("interval_seconds") == resolved["interval_seconds"])
        )
        payload = {
            "scan_id": scan_id, "trade_date": trade_date, "status": scan_status,
            "scanned_at": common.now_str(), "market_phase": phase,
            "manual": bool(manual), "window_minutes": resolved["window_minutes"],
            "universe": universe,
            "quality": {
                "quote_source": quote_source, "quote_date": snapshot.get("quote_date"),
                "quote_as_of": quote_time, "coverage": coverage,
                "window": window_meta,
                "priority_errors": priority_errors,
                "factor_trade_date": factor_date, "factor_error": factor_error,
                "sector_membership": sector_quality,
                "large_order": {"status": "unavailable",
                                "reason": "当前实时源不含逐笔委托/成交，禁止用窗口成交额冒充大单"},
                "minute_indicators": {
                    "status": "warming" if phase_samples < 13 else "available",
                    "sample_count": phase_samples,
                    "sample_interval_seconds": resolved["interval_seconds"],
                    "note": "MACD 使用 6/13/5 快参数；KDJ 与承接为按配置频率采样的盘中快照代理",
                },
            },
            "index_sync": index_status,
            "market_summary": {
                "scanned_count": len(records), "qualified_count": len(qualified),
                "priority_alert_count": len(alerts),
                "avg_speed_pct": _finite_number(speed_values.mean(), 4) if len(speed_values) else None,
            },
            "qualified": [_public_stock(row) for row in qualified[:resolved["max_candidates"]]],
            "priority_alerts": [_public_stock(row) for row in alerts],
            "top_total_amount": _top(records, "total_amount", 20),
            "top_window_amount": _top(records, "window_amount", 10),
            "top_speed": _top(records, "speed_pct", 10),
            "breakouts": [_public_stock(row) for row in records if row["breakout"]][:30],
            "breakdowns": [_public_stock(row) for row in records if row["breakdown"]][:30],
            "anomalies": [_public_stock(row) for row in sorted(
                anomalies, key=lambda row: abs(float(row["speed_pct"])), reverse=True)[:30]],
            "sectors": sector_rows,
            "sector_rotation": _sector_rotation(sector_rows, trade_date),
        }
        payload = _json_safe(payload)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        renew_or_fail("发送通知")
        payload["notification"] = _notify(
            payload, resolved, _OWNER, fence_token)
        payload = _json_safe(payload)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if not db.quant_watch_scan_owned(_OWNER, fence_token):
            raise RuntimeError("聚合消息落库前扫描租约已失效")
        saved = db.save_quant_watch_message({
            "message_id": scan_id, "trade_date": trade_date,
            "scanned_at": now.replace(tzinfo=None), "phase": phase,
            "status": scan_status, "payload": payload,
        }, _OWNER, fence_token)
        if not saved:
            raise RuntimeError("聚合消息落库前扫描租约已失效")
        if not db.update_quant_watch_state(
                _OWNER, fence_token, status=scan_status, trade_date=trade_date,
                last_scan_at=now.replace(tzinfo=None), last_error=None,
                last_message_id=scan_id, phase=phase):
            raise RuntimeError("更新扫描状态前租约已失效")
        samples_committed = True
        if not db.quant_watch_scan_owned(_OWNER, fence_token):
            raise RuntimeError("发布 WebSocket 消息前扫描租约已失效")
        _publish(payload)
        return payload
    except Exception as exc:
        if not samples_committed:
            _rollback_scan_samples(scan_id)
        if owner_claimed and fence_token >= 0:
            try:
                db.update_quant_watch_state(
                    _OWNER, fence_token, status="error", trade_date=common.today_str(),
                    last_error=f"{type(exc).__name__}: {exc}"[:500])
            except Exception:
                pass
        raise
    finally:
        if owner_claimed and fence_token >= 0:
            try:
                db.release_quant_watch_lease(_OWNER, fence_token)
            except Exception:
                pass
        _SCAN_LOCK.release()


def _publish(payload: dict[str, Any]) -> None:
    global _LATEST, _SEQUENCE
    _invalidate_status_cache()
    with _CONDITION:
        _LATEST = {"payload": payload}
        _SEQUENCE += 1
        _CONDITION.notify_all()


def _clean_for_trade_day(clock: dict[str, Any]) -> None:
    global _LAST_CLEAN_DATE, _LATEST, _SEQUENCE
    today = common.today_str()
    now = common.shanghai_now()
    if (clock.get("is_trading_day")
            and now.time().replace(tzinfo=None) >= datetime.strptime("09:00", "%H:%M").time()
            and _LAST_CLEAN_DATE != today):
        db.clear_quant_watch_before(today)
        _SNAPSHOTS.clear()
        _INDEX_SNAPSHOTS.clear()
        _invalidate_status_cache()
        with _CONDITION:
            _LATEST = {}
            _SEQUENCE += 1
            _CONDITION.notify_all()
        _LAST_CLEAN_DATE = today


def _state_with_scheduler(config: dict[str, Any]) -> dict[str, Any]:
    raw = db.get_quant_watch_state() or {
        "status": "waiting", "trade_date": common.today_str(), "phase": None,
        "last_scan_at": None, "last_error": None,
    }
    try:
        clock = common.market_clock()
        market_phase = clock.get("phase")
        continuous = market_phase in _ACTIVE_PHASES
    except Exception:
        market_phase = None
        continuous = False
    raw_status = str(raw.get("status") or "")
    if raw_status == "error" or raw.get("last_error"):
        last_scan_status = "error"
    elif raw_status == "degraded":
        last_scan_status = "degraded"
    elif raw_status == "success" or raw.get("last_scan_at"):
        last_scan_status = "success"
    else:
        last_scan_status = "never"
    thread_ready = bool(_THREAD and _THREAD.is_alive())
    if _CONFIG_ERROR:
        scheduler_status = "degraded"
    elif not config["enabled"]:
        scheduler_status = "disabled"
    elif not thread_ready:
        scheduler_status = "unavailable"
    elif continuous:
        scheduler_status = "running"
    else:
        scheduler_status = "waiting"
    raw.update({
        "status": scheduler_status,
        "scheduler_status": scheduler_status,
        "market_phase": market_phase,
        "current_phase": market_phase,
        "is_continuous_trading": continuous,
        "last_scan_status": last_scan_status,
        "config_error": _CONFIG_ERROR,
    })
    return raw


def status(limit: int = 60) -> dict[str, Any]:
    """返回量化盯盘状态；同一条数的并发读取在半秒内复用不可变快照。"""
    effective_limit = max(1, min(int(limit), 300))
    now_mono = time.monotonic()
    with _STATUS_CACHE_LOCK:
        cached = _STATUS_CACHE.get(effective_limit)
        if cached and now_mono - cached[0] < _STATUS_CACHE_TTL:
            return copy.deepcopy(cached[1])
        config = get_config()
        today = common.today_str()
        messages = db.fetch_quant_watch_messages(today, effective_limit)
        latest = messages[0]["payload"] if messages else None
        snapshot = {
            "source": "quant_watch", "fetched_at": common.now_str(),
            "trade_date": today, "config": config,
            "state": _state_with_scheduler(config),
            "latest": latest, "messages": messages,
            "notification_channels": notifications.available_channels(),
            "retention": "接口仅返回当天聚合消息；下一交易日 09:00 清理旧消息",
        }
        _STATUS_CACHE[effective_limit] = (time.monotonic(), snapshot)
        return copy.deepcopy(snapshot)


def wait_for_update(last_sequence: int, timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    with _CONDITION:
        if _SEQUENCE <= last_sequence:
            _CONDITION.wait(timeout=max(1.0, min(float(timeout), 30.0)))
        sequence = _SEQUENCE
    return sequence, status()


def wake_update_waiters() -> None:
    """唤醒广播等待线程，用于最后连接断开和服务优雅停止。"""
    with _CONDITION:
        _CONDITION.notify_all()


def _loop() -> None:
    last_error = ""
    last_error_at = 0.0
    while not _STOP.is_set():
        config = get_config()
        wait_seconds = 5.0
        started = time.perf_counter()
        try:
            clock = common.market_clock()
            _clean_for_trade_day(clock)
            if config["enabled"] and clock.get("phase") in _ACTIVE_PHASES:
                result = scan_once(config, manual=False)
                if result.get("claimed") is not False:
                    observability.record_quant_watch(
                        str(result.get("status") or "success"),
                        (time.perf_counter() - started) * 1000,
                        manual=False, payload=result)
            last_error = ""
        except Exception as exc:
            # 时钟或依赖持续异常时最多每五分钟记录一次；错误变化立即记录。
            error = f"{type(exc).__name__}: {exc}"[:500]
            now_mono = time.monotonic()
            if error != last_error or now_mono - last_error_at >= 300:
                observability.record_quant_watch(
                    "error", (time.perf_counter() - started) * 1000,
                    manual=False, error=error)
                last_error = error
                last_error_at = now_mono
        _WAKE.wait(wait_seconds)
        _WAKE.clear()


def start() -> None:
    """启动服务端扫描线程；数据库租约确保多进程仅一个实例执行扫描。"""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    _WAKE.clear()
    _THREAD = threading.Thread(target=_loop, name="quant-watch", daemon=True)
    _THREAD.start()


def stop() -> None:
    """请求线程停止；正在执行的扫描由自身 finally 按 fencing token 释放租约。"""
    _STOP.set()
    _WAKE.set()
    wake_update_waiters()
    thread = _THREAD
    if thread and thread.is_alive():
        thread.join(timeout=5)


@register("quant_watch_status", "watch",
          "量化盯盘当天状态与聚合消息；仅返回当日数据，下一交易日09:00清理",
          params=[{"name": "limit", "type": "int", "required": False, "default": 60}],
          returns="config / state / latest / messages")
def quant_watch_status(p: dict[str, Any]) -> dict[str, Any]:
    try:
        limit = int(p.get("limit", 60))
    except (TypeError, ValueError) as exc:
        raise ParamError("limit 必须是整数") from exc
    return status(limit)


@register("quant_watch_get_config", "watch", "读取量化盯盘设置与通知渠道配置状态",
          params=[], returns="config / notification_channels")
def quant_watch_get_config(p: dict[str, Any]) -> dict[str, Any]:
    return {"source": "quant_watch", "fetched_at": common.now_str(),
            "config": get_config(),
            "notification_channels": notifications.available_channels()}


@register("quant_watch_set_config", "watch", "保存并版本化量化盯盘设置",
          params=[{"name": "config", "type": "object", "required": True},
                  {"name": "reason", "type": "string", "required": False}],
          returns="config / changed / config_version")
def quant_watch_set_config(p: dict[str, Any]) -> dict[str, Any]:
    config = p.get("config")
    if not isinstance(config, dict):
        raise ParamError("config 必须是对象")
    result = set_config(config, actor="web-admin", reason=str(p.get("reason") or ""))
    result.update({"source": "quant_watch", "fetched_at": common.now_str()})
    return result


@register("quant_watch_scan_once", "watch", "管理员在连续竞价时手动执行一轮量化盯盘诊断",
          params=[], returns="单轮扫描聚合结果")
def quant_watch_scan_once(p: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = scan_once(get_config(), manual=True)
        observability.record_quant_watch(
            str(result.get("status") or "success"),
            (time.perf_counter() - started) * 1000,
            manual=True, payload=result)
        return result
    except Exception as exc:
        observability.record_quant_watch(
            "error", (time.perf_counter() - started) * 1000,
            manual=True, error=f"{type(exc).__name__}: {exc}")
        raise


def health_summary() -> dict[str, Any]:
    """供 /health 使用的轻量统一状态，不读取当日消息正文。"""
    config = get_config()
    state = _state_with_scheduler(config)
    return {
        "enabled": config["enabled"],
        "interval_seconds": config["interval_seconds"],
        "status": state["scheduler_status"],
        "scheduler_status": state["scheduler_status"],
        "market_phase": state["market_phase"],
        "last_scan_status": state["last_scan_status"],
        "trade_date": state.get("trade_date"),
        "last_scan_at": state.get("last_scan_at"),
        "next_scan_at": state.get("next_scan_at"),
        "last_error": state.get("last_error") or state.get("config_error"),
        "config_error": state.get("config_error"),
    }

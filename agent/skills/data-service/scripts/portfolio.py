"""管理员自选管理：股票模糊搜索、当前关注/持仓获取与版本化上传。"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import common
import db
from registry import ParamError, register

_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_NUMERIC_QUERY_RE = re.compile(r"^\d{1,6}$")
_MAX_COST_PRICE = Decimal("99999999999999.9999")
_MAX_LOTS = 2_147_483_647


def _stock_universe() -> list[dict[str, Any]]:
    """读取上市股票基础信息，搜索与上传校验共用当日缓存。"""
    pro = common.get_pro()
    payload = common.cached_call(
        "portfolio_stock_universe", {"date": common.today_str()},
        lambda: pro.stock_basic(
            exchange="", list_status="L",
            fields="ts_code,name,industry,market,list_date"),
    )
    return list(payload.get("rows") or [])


def _normalize_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not _CODE_RE.fullmatch(code):
        raise ParamError("code 必须是完整股票代码，如 600519.SH")
    return code


@register("portfolio_stock_search", "portfolio",
          "管理员自选股票搜索：按股票名称或代码片段模糊匹配，必须先从结果中选择再添加",
          params=[{"name": "query", "type": "string", "required": True,
                   "desc": "股票名称或代码片段"},
                  {"name": "limit", "type": "int", "required": False, "default": 12}],
          returns="query / rows[{code,name,industry,market}] / total_matches")
def portfolio_stock_search(p: dict) -> dict:
    query = str(p.get("query") or "").strip()
    if not query:
        raise ParamError("query 必填")
    if len(query) > 64:
        raise ParamError("query 最长 64 个字符")
    try:
        limit = min(max(int(p.get("limit", 12)), 1), 30)
    except (TypeError, ValueError):
        raise ParamError("limit 必须是 1 到 30 的整数")

    folded = query.casefold()
    digits = query if _NUMERIC_QUERY_RE.fullmatch(query) else ""
    matches: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    for row in _stock_universe():
        code = str(row.get("ts_code") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        code_folded = code.casefold()
        code_digits = code.split(".", 1)[0]
        name_folded = name.casefold()

        if folded in {name_folded, code_folded, code_digits}:
            rank = 0
        elif digits and code_digits.startswith(digits):
            rank = 1
        elif name_folded.startswith(folded):
            rank = 2
        elif folded in name_folded:
            rank = 3
        elif folded in code_folded or (digits and digits in code_digits):
            rank = 4
        else:
            continue

        matches.append(((rank, len(name), code), {
            "code": code, "name": name,
            "industry": str(row.get("industry") or ""),
            "market": str(row.get("market") or ""),
        }))
    matches.sort(key=lambda item: item[0])
    rows = [item[1] for item in matches[:limit]]
    return {"source": "portfolio_stock_search", "fetched_at": common.now_str(),
            "query": query, "total_matches": len(matches), "rows": rows}


def _normalize_upload_item(raw: Any, valid_stocks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ParamError("items 每项必须是对象")
    code = _normalize_code(raw.get("code"))
    deleted = raw.get("deleted", False)
    if not isinstance(deleted, bool):
        raise ParamError(f"{code} 的 deleted 必须是布尔值")
    if deleted:
        return {"code": code, "deleted": True}

    stock = valid_stocks.get(code)
    if not stock:
        raise ParamError(f"{code} 不在当前上市股票池；新增前请先搜索并从结果中选择")
    item_type = str(raw.get("type") or raw.get("item_type") or "").strip().lower()
    if item_type not in {"watch", "holding"}:
        raise ParamError(f"{code} 的 type 必须为 watch 或 holding")
    note = str(raw.get("note") or "").strip()
    if len(note) > 2000:
        raise ParamError(f"{code} 的备注最长 2000 个字符")

    cost_price = None
    lots = None
    if item_type == "holding":
        cost_raw = raw.get("cost_price")
        lots_raw = raw.get("lots")
        if isinstance(cost_raw, bool) or isinstance(lots_raw, bool):
            raise ParamError(f"{code} 设为持仓时必须填写大于0的持仓成本和整数手数")
        try:
            cost_value = Decimal(str(cost_raw))
            lots_value = Decimal(str(lots_raw))
            if (not cost_value.is_finite() or cost_value <= 0
                    or cost_value > _MAX_COST_PRICE):
                raise ValueError
            if (not lots_value.is_finite() or lots_value <= 0
                    or lots_value != lots_value.to_integral_value()
                    or lots_value > _MAX_LOTS):
                raise ValueError
            cost_price = cost_value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            lots = int(lots_value)
        except (InvalidOperation, TypeError, ValueError):
            raise ParamError(f"{code} 设为持仓时必须填写大于0的持仓成本和整数手数")

    return {
        "code": code,
        "name": str(stock.get("name") or "").strip(),
        "type": item_type,
        "cost_price": cost_price,
        "lots": lots,
        "note": note,
    }


@register("portfolio_get", "portfolio",
          "管理员获取当前关注与持仓；按股票代码唯一，返回独立 portfolio_version",
          params=[], returns="portfolio_version / holding_count / watch_count / rows")
def portfolio_get(p: dict) -> dict:
    result = db.fetch_portfolio_items()
    result.update({"source": "portfolio_db", "fetched_at": common.now_str()})
    return result


@register("portfolio_upload", "portfolio",
          "管理员批量上传关注与持仓；同批次同代码最后一项生效，服务端按代码 upsert 最新状态，实际变化才升级版本",
          params=[{"name": "items", "type": "array", "required": True,
                   "desc": "[{code,type:watch|holding,cost_price,lots,note}]；deleted=true 表示移除"},
                  {"name": "source", "type": "string", "required": False, "default": "agent"}],
          returns="portfolio_version / changed / inserted / updated / deleted / unchanged / rows")
def portfolio_upload(p: dict) -> dict:
    raw_items = p.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ParamError("items 必须是非空数组")
    if len(raw_items) > 500:
        raise ParamError("单次最多上传 500 项")
    source = str(p.get("source") or "agent").strip()[:32] or "agent"

    # 先按规范化代码保留最后一项，再只校验最终状态，确保批次语义可预测。
    deduped_raw: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ParamError("items 每项必须是对象")
        code = _normalize_code(raw.get("code"))
        deduped_raw[code] = {**raw, "code": code}

    needs_validation = any(item.get("deleted") is not True for item in deduped_raw.values())
    universe = _stock_universe() if needs_validation else []
    valid_stocks = {str(row.get("ts_code") or "").strip().upper(): row for row in universe}
    items = [_normalize_upload_item(item, valid_stocks) for item in deduped_raw.values()]
    result = db.apply_portfolio_upload(items, source)
    result.update({
        "source": "portfolio_db",
        "fetched_at": common.now_str(),
        "received_count": len(raw_items),
        "deduplicated_count": len(items),
    })
    return result

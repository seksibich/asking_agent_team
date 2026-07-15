"""统一因子契约：分离公式结构版本、完整成分和权重版本。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import factors

MODEL_VERSIONS = {
    "stock": factors.STOCK_FACTOR_VERSION,
    "sector": "sector-factors-v1",
    "trend": "trend-factors-v1",
    "sentiment": "sentiment-factors-v1",
}

MODEL_WEIGHTS = {
    "stock": factors.STOCK_FACTOR_WEIGHTS,
    "sector": factors.SECTOR_FACTOR_WEIGHTS,
    "trend": factors.TREND_FACTOR_WEIGHTS,
    "sentiment": factors.SENTIMENT_FACTOR_WEIGHTS,
}

# 公式修订号进入 schema_hash；公式口径变化时必须同时提升对应修订号与 MODEL_VERSIONS。
FORMULA_REVISIONS = {
    "stock": {
        "mom_12_1": "1", "trend_ma": "1", "high_52w": "1", "reversal_1m": "1",
        "low_turnover": "1", "low_ivol": "1", "vol_confirm": "1", "industry_strength": "1",
        "mom_6_1": "1", "max_lottery": "1", "downside_vol": "1", "amihud_illiq": "1",
        "small_size": "1", "value_bm": "1", "earnings_yield": "1",
    },
    "sector": {name: "1" for name in factors.SECTOR_FACTOR_WEIGHTS},
    "trend": {name: "1" for name in factors.TREND_FACTOR_WEIGHTS},
    "sentiment": {name: "1" for name in factors.SENTIMENT_FACTOR_WEIGHTS},
}

def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def base_contract(model: str) -> dict[str, Any]:
    """返回不含可变权重的公式契约；所有成分均保留，包括默认权重为 0 的候选因子。"""
    if model not in MODEL_WEIGHTS:
        raise ValueError(f"未知因子模型：{model}")
    components = list(MODEL_WEIGHTS[model])
    revisions = FORMULA_REVISIONS[model]
    if set(components) != set(revisions):
        raise RuntimeError(f"{model} 因子定义与公式修订不一致")
    definition = {
        "components": [{"name": name, "formula_revision": revisions[name],
                        "direction": "higher_is_better"} for name in components],
        "score_method": "cross_sectional_zscore_weighted_sum",
        "auxiliary_fields": ["price", "_meta"],
    }
    identity = {"model": model, "factor_version": MODEL_VERSIONS[model],
                "components": components, "definition": definition}
    return {**identity, "schema_hash": _hash(identity)}


def weighted_contract(model: str, weights: dict[str, float],
                      weight_version: str | None = None) -> dict[str, Any]:
    """绑定完整权重快照；权重版本变化不改变公式 schema_hash。"""
    base = base_contract(model)
    if set(weights) != set(base["components"]):
        raise ValueError(f"{model} 权重成分与因子契约不一致")
    normalized = {name: float(weights[name]) for name in base["components"]}
    if any(not math.isfinite(value) or value < 0 for value in normalized.values()):
        raise ValueError("因子权重必须是非负有限数")
    return {
        **base,
        "weights": normalized,
        "weight_version": weight_version or "default",
        "weight_hash": _hash(normalized),
        "active_components": [name for name, value in normalized.items() if value != 0],
    }


def validate_payload(model: str, payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """严格校验原始因子快照包含契约全部成分；辅助字段不参与成分判断。"""
    components = base_contract(model)["components"]
    missing = [name for name in components if name not in payload]
    invalid = []
    for name in components:
        if name not in payload:
            continue
        try:
            if not math.isfinite(float(payload[name])):
                invalid.append(name)
        except (TypeError, ValueError):
            invalid.append(name)
    return not missing and not invalid, missing + invalid


def fingerprint(value: Any) -> str:
    """为因子依赖、股票池等任意规范 JSON 生成稳定指纹。"""
    return _hash(value)


def dependency_summary(contract: dict[str, Any]) -> dict[str, Any]:
    """提取会改变因子值的上游公式与权重版本。"""
    return {key: contract.get(key) for key in (
        "model", "factor_version", "schema_hash", "weight_version", "weight_hash")}


def stock_data_dependencies(sector_contract: dict[str, Any]) -> dict[str, Any]:
    """返回个股因子预计算的完整上游依赖口径，供写入与消费端统一计算指纹。"""
    return {
        "sector_scoring": dependency_summary(sector_contract),
        "sector_membership": {
            "source": "tushare.index_member",
            "classification": "申万一级行业",
            "as_of_rule": "in_date<=trade_date<out_date",
            "revision": "1",
        },
        "stock_universe": {
            "source": "tushare.stock_basic:L,D,P",
            "as_of_rule": "list_date<=trade_date<delist_date;exclude_ST_and_delisting",
            "revision": "1",
        },
    }

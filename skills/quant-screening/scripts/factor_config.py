"""量化因子/指标权重配置（可配置 + 校验）。

- 四个模型的权重可运行时配置：
  - stock     : 个股量化选股（screen_quant）
  - sector    : 选板块/板块轮动（screen_sector）
  - trend     : 趋势选股（screen_trend）
  - sentiment : 情绪温度指标（sentiment_temperature / market_timing）
- get_factor_config：返回各模型的规范因子列表、当前生效权重、来源（default/override）。
- set_factor_weights：提交某模型的【全部】因子权重；校验规则：
    · 必须恰好覆盖该模型的全部规范因子（缺失/多余/名称差异均报错并给出 expected_factors）
    · 权重之和必须约等于 1.0（容差 0.01）
  校验失败返回 {applied: false, ...} 并指引 Agent 先 get_factor_config 同步因子列表。
- 覆盖持久化到 DATA_DIR/factor_weights.json，选股脚本运行时读取。
"""
from __future__ import annotations

import json
from typing import Any

import common
import db
import factors
from registry import register

CONFIG_KEY = "factor_weights"   # config_kv 中的键
TOL = 0.01

# 规范因子集（默认权重）
DEFAULTS: dict[str, dict[str, float]] = {
    "stock": factors.STOCK_FACTOR_WEIGHTS,
    "sector": factors.SECTOR_FACTOR_WEIGHTS,
    "trend": factors.TREND_FACTOR_WEIGHTS,
    "sentiment": factors.SENTIMENT_FACTOR_WEIGHTS,
}


def canonical_factors(model: str) -> list[str]:
    """某模型的规范因子名（有序）。"""
    return list(DEFAULTS[model].keys())


def _load_overrides() -> dict[str, Any]:
    try:
        return db.get_config(CONFIG_KEY) or {}
    except Exception:
        return {}


def _save_overrides(data: dict[str, Any]) -> None:
    db.set_config(CONFIG_KEY, data)  # 落库


def effective_weights(model: str) -> dict[str, float]:
    """生效权重：若有 override 且因子集匹配则用 override，否则用默认。"""
    if model not in DEFAULTS:
        return {}
    ov = _load_overrides().get(model, {}).get("weights")
    if ov and set(ov.keys()) == set(DEFAULTS[model].keys()):
        return dict(ov)
    return dict(DEFAULTS[model])


@register("get_factor_config", "screening",
          "获取因子/指标权重配置：各模型(stock/sector/trend/sentiment)的规范因子列表、当前生效权重与来源",
          params=[{"name": "model", "type": "string", "required": False,
                   "desc": "stock|sector|trend|sentiment；省略返回全部"}],
          returns="models -> {canonical_factors, weights, source}")
def get_factor_config(p: dict) -> dict:
    ov = _load_overrides()
    models = [p["model"]] if p.get("model") else list(DEFAULTS.keys())
    out: dict[str, Any] = {}
    for m in models:
        if m not in DEFAULTS:
            continue
        has_ov = bool(ov.get(m, {}).get("weights")) and \
            set(ov[m]["weights"].keys()) == set(DEFAULTS[m].keys())
        out[m] = {
            "canonical_factors": canonical_factors(m),
            "weights": effective_weights(m),
            "source": "override" if has_ov else "default",
            "updated_at": ov.get(m, {}).get("updated_at") if has_ov else None,
        }
    return {"source": "factor_config", "fetched_at": common.now_str(), "models": out}


@register("set_factor_weights", "screening",
          "更新某模型的全部因子/指标权重（必须传全部且权重和=1）。缺失/多余/差异/和≠1 时返回错误并指引",
          params=[{"name": "model", "type": "string", "required": True, "desc": "stock|sector|trend|sentiment"},
                  {"name": "weights", "type": "object", "required": True,
                   "desc": "全部规范因子的权重字典，权重和须=1.0"}],
          returns="applied 是否成功；失败含 expected_factors/missing/unexpected/hint")
def set_factor_weights(p: dict) -> dict:
    model = p["model"]
    weights = p["weights"] or {}
    if model not in DEFAULTS:
        return {"applied": False, "error": f"unknown model: {model}",
                "valid_models": list(DEFAULTS.keys())}

    expected = set(DEFAULTS[model].keys())
    got = set(weights.keys())
    missing = sorted(expected - got)
    unexpected = sorted(got - expected)
    if missing or unexpected:
        return {
            "applied": False,
            "error": "因子列表不匹配：必须提交该模型的全部规范因子且不能有多余因子",
            "model": model,
            "expected_factors": canonical_factors(model),
            "missing": missing,
            "unexpected": unexpected,
            "hint": "先调用 get_factor_config 获取最新规范因子列表，补齐/去除后重试",
        }

    try:
        total = sum(float(v) for v in weights.values())
    except (TypeError, ValueError):
        return {"applied": False, "error": "权重值必须为数值", "model": model}
    if abs(total - 1.0) > TOL:
        return {"applied": False, "error": f"权重之和必须为 1.0（当前 {round(total, 4)}）",
                "model": model, "expected_factors": canonical_factors(model)}

    ov = _load_overrides()
    ov[model] = {"weights": {k: round(float(v), 6) for k, v in weights.items()},
                 "updated_at": common.now_str()}
    _save_overrides(ov)
    return {"applied": True, "model": model, "weights": ov[model]["weights"],
            "note": "已保存，后续 screen_quant/screen_sector/screen_trend 将使用新权重"}


if __name__ == "__main__":
    print(json.dumps(get_factor_config({}), ensure_ascii=False, indent=2))

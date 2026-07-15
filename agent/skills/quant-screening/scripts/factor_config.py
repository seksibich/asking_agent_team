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
            "version_id": ov.get(m, {}).get("version_id") if has_ov else None,
            "actor": ov.get(m, {}).get("actor") if has_ov else None,
        }
    return {"source": "factor_config", "fetched_at": common.now_str(), "models": out}


@register("set_factor_weights", "screening",
          "更新某模型的全部因子/指标权重（必须传全部且权重和=1）。缺失/多余/差异/和≠1 时返回错误并指引。"
          "每次成功修改都会留痕为一个类 commit 的 version_id（署名 actor），可用 get_config_history/get_config_version 定位",
          params=[{"name": "model", "type": "string", "required": True, "desc": "stock|sector|trend|sentiment"},
                  {"name": "weights", "type": "object", "required": True,
                   "desc": "全部规范因子的权重字典，权重和须=1.0"},
                  {"name": "actor", "type": "string", "required": False, "default": "agent",
                   "desc": "修改者身份署名，如 回测分析师/main-orchestrator/user"},
                  {"name": "reason", "type": "string", "required": False, "default": "",
                   "desc": "修改原因（回测证据/背离说明等），建议附回测 version 或关键指标"}],
          returns="applied 是否成功 + version_id（留痕版本）；失败含 expected_factors/missing/unexpected/hint")
def set_factor_weights(p: dict) -> dict:
    model = p["model"]
    weights = p["weights"] or {}
    actor = p.get("actor") or "agent"
    reason = p.get("reason") or ""
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

    norm_weights = {k: round(float(v), 6) for k, v in weights.items()}
    # 留痕为一个版本（类 commit），署名 actor
    ver = db.record_config_version(f"factor_weights:{model}", norm_weights, actor, reason)
    ov = _load_overrides()
    ov[model] = {"weights": norm_weights, "updated_at": common.now_str(),
                 "version_id": ver["version_id"], "actor": actor, "reason": reason,
                 "parent_version": ver.get("parent_version")}
    _save_overrides(ov)
    return {"applied": True, "model": model, "weights": norm_weights,
            "version_id": ver["version_id"], "parent_version": ver.get("parent_version"),
            "actor": actor, "reason": reason,
            "note": "已保存并留痕；后续 screen_quant/screen_sector/screen_trend 将使用新权重。"
                    "可用 get_config_history / get_config_version 定位或 restore_config_version 回滚"}


@register("get_config_history", "screening",
          "查询配置变更留痕（因子/情绪权重、归一窗口等）：按 version_id 倒序返回，含 actor/reason/payload/parent。"
          "可按 config_key 过滤（如 factor_weights:stock / factor_weights:sentiment / sentiment_window）",
          params=[{"name": "config_key", "type": "string", "required": False,
                   "desc": "过滤键：factor_weights:<model> 或 sentiment_window；省略返回全部"},
                  {"name": "model", "type": "string", "required": False,
                   "desc": "便捷参数：等价于 config_key=factor_weights:<model>"},
                  {"name": "limit", "type": "int", "required": False, "default": 50}],
          returns="versions 列表（version_id/config_key/actor/reason/payload/parent_version/created_at）")
def get_config_history(p: dict) -> dict:
    key = p.get("config_key")
    if not key and p.get("model"):
        key = f"factor_weights:{p['model']}"
    versions = db.list_config_versions(key, int(p.get("limit", 50)))
    return {"source": "config_history", "fetched_at": common.now_str(),
            "config_key": key, "versions": versions}


@register("get_config_version", "screening",
          "按 version_id 定位某次配置变更的完整快照（含当时的全部权重 payload、actor、reason）",
          params=[{"name": "version_id", "type": "string", "required": True, "desc": "类 commit 的版本号"}],
          returns="该版本的 config_key/actor/reason/payload/parent_version/created_at；不存在返回 found=false")
def get_config_version(p: dict) -> dict:
    v = db.get_config_version(p["version_id"])
    if not v:
        return {"source": "config_version", "fetched_at": common.now_str(),
                "found": False, "version_id": p["version_id"]}
    return {"source": "config_version", "fetched_at": common.now_str(), "found": True, **v}


@register("restore_config_version", "screening",
          "回滚到某个历史版本的配置：读取该 version_id 的 payload 并重新生效（仅支持 factor_weights:<model>）。"
          "回滚本身也会留痕为一个新版本（parent 指向被回滚版本），署名 actor",
          params=[{"name": "version_id", "type": "string", "required": True, "desc": "要回滚到的历史版本号"},
                  {"name": "actor", "type": "string", "required": False, "default": "agent"},
                  {"name": "reason", "type": "string", "required": False, "default": ""}],
          returns="restored 是否成功 + 新 version_id")
def restore_config_version(p: dict) -> dict:
    v = db.get_config_version(p["version_id"])
    if not v:
        return {"restored": False, "error": f"version 不存在: {p['version_id']}"}
    key = v.get("config_key", "")
    if not key.startswith("factor_weights:"):
        return {"restored": False, "error": f"暂只支持回滚 factor_weights:<model>，该版本 config_key={key}"}
    model = key.split(":", 1)[1]
    payload = v.get("payload") or {}
    reason = p.get("reason") or f"restore from {p['version_id']}"
    return set_factor_weights({"model": model, "weights": payload,
                               "actor": p.get("actor") or "agent", "reason": reason})


if __name__ == "__main__":
    print(json.dumps(get_factor_config({}), ensure_ascii=False, indent=2))

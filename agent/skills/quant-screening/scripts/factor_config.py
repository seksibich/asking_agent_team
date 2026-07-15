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
import math
from typing import Any

import common
import db
import factor_contract
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


def current_weight_version(model: str) -> str | None:
    """返回当前覆盖权重版本；默认权重没有数据库版本，返回 None。"""
    entry = _load_overrides().get(model, {})
    weights = entry.get("weights") or {}
    return entry.get("version_id") if set(weights) == set(DEFAULTS.get(model, {})) else None


def model_contract(model: str) -> dict[str, Any]:
    """返回公式结构与当前完整权重的可复现契约，包括默认权重为 0 的因子。"""
    return factor_contract.weighted_contract(
        model, effective_weights(model), current_weight_version(model) or "default")


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
        contract = model_contract(m)
        out[m] = {
            "canonical_factors": canonical_factors(m),
            "weights": effective_weights(m),
            "source": "override" if has_ov else "default",
            "updated_at": ov.get(m, {}).get("updated_at") if has_ov else None,
            "version_id": ov.get(m, {}).get("version_id") if has_ov else None,
            "actor": ov.get(m, {}).get("actor") if has_ov else None,
            "factor_version": contract["factor_version"],
            "schema_hash": contract["schema_hash"],
            "weight_version": contract["weight_version"],
            "active_components": contract["active_components"],
            "contract": contract,
        }
    return {"source": "factor_config", "fetched_at": common.now_str(), "models": out}


@register("set_factor_weights", "screening",
          "原子更新某模型全部因子权重。服务端校验完整契约、非负有限值、权重和、并发父版本；"
          "Agent 自动调参还必须绑定达到样本外门槛的回测快照并遵守小步与零权重锁定。",
          params=[{"name": "model", "type": "string", "required": True, "desc": "stock|sector|trend|sentiment"},
                  {"name": "weights", "type": "object", "required": True,
                   "desc": "全部规范因子的权重字典，含权重为0的候选因子"},
                  {"name": "actor", "type": "string", "required": False, "default": "agent"},
                  {"name": "reason", "type": "string", "required": False, "default": ""},
                  {"name": "expected_parent_version", "type": "string", "required": False,
                   "desc": "get_factor_config 返回的当前 weight_version；用于防并发覆盖"},
                  {"name": "backtest_snapshot_id", "type": "int", "required": False,
                   "desc": "Agent 自动调参必须绑定的合格回测快照"}],
          returns="applied/version_id；拒绝时返回契约、并发或优化门禁原因")
def set_factor_weights(p: dict) -> dict:
    model = p["model"]
    weights = p.get("weights") or {}
    actor = str(p.get("actor") or "agent")
    reason = str(p.get("reason") or "")
    if model not in DEFAULTS:
        return {"applied": False, "error": f"unknown model: {model}",
                "valid_models": list(DEFAULTS)}

    expected = set(DEFAULTS[model])
    got = set(weights)
    missing, unexpected = sorted(expected - got), sorted(got - expected)
    if missing or unexpected:
        return {"applied": False, "error": "必须提交契约中的全部因子，含权重为0的候选因子",
                "model": model, "expected_factors": canonical_factors(model),
                "missing": missing, "unexpected": unexpected}
    try:
        normalized = {name: round(float(weights[name]), 6) for name in canonical_factors(model)}
    except (TypeError, ValueError):
        return {"applied": False, "error": "权重值必须为数值", "model": model}
    if any(not math.isfinite(value) or value < 0 for value in normalized.values()):
        return {"applied": False, "error": "权重必须是非负有限数", "model": model}
    total = sum(normalized.values())
    if abs(total - 1.0) > TOL:
        return {"applied": False, "error": f"权重之和必须为1.0（当前 {round(total, 6)}）",
                "model": model}
    if max(normalized.values(), default=0) > 0.40:
        return {"applied": False, "error": "单因子权重不得超过0.40", "model": model}

    manual = bool(p.get("manual_override")) or actor.casefold() in {
        "user", "用户", "admin", "管理员"
    }
    snapshot_id = p.get("backtest_snapshot_id")
    current = effective_weights(model)
    if not manual:
        if "expected_parent_version" not in p:
            return {"applied": False, "error": "Agent 调参必须提交 expected_parent_version 防止并发覆盖"}
        changed_too_much = [name for name in normalized
                            if abs(normalized[name] - float(current[name])) > 0.030001]
        activated = [name for name in normalized if float(current[name]) == 0 and normalized[name] != 0]
        if changed_too_much:
            return {"applied": False, "error": "Agent 单次每个因子调整不得超过0.03",
                    "factors": changed_too_much}
        if activated:
            return {"applied": False, "error": "Agent 不得自动启用当前权重为0的候选因子",
                    "factors": activated}
        snapshot = db.get_snapshot(int(snapshot_id)) if snapshot_id is not None else None
        gate = (snapshot or {}).get("payload", {}).get("optimization_gate", {})
        if not snapshot or snapshot.get("kind") != "selection" or not gate.get("eligible"):
            return {"applied": False, "error": "Agent 调参必须绑定通过样本量与样本外验证的选股回测快照"}
        if gate.get("schema_hash") != factor_contract.base_contract(model)["schema_hash"]:
            return {"applied": False, "error": "回测快照因子契约与当前模型不一致"}
        if model == "stock":
            dependency_hash = factor_contract.fingerprint(
                factor_contract.stock_data_dependencies(model_contract("sector")))
            if gate.get("dependency_hash") != dependency_hash:
                return {"applied": False, "error": "回测快照的行业公式或权重依赖已过期"}

    contract = factor_contract.weighted_contract(
        model, normalized, p.get("expected_parent_version") or current_weight_version(model) or "default")
    published = db.publish_factor_weights(
        model=model, weights=normalized, actor=actor, reason=reason,
        expected_parent_version=p.get("expected_parent_version"),
        entry_metadata={"schema_hash": contract["schema_hash"],
                        "factor_version": contract["factor_version"],
                        "backtest_snapshot_id": snapshot_id})
    if not published.get("applied"):
        published["error"] = "配置已被其他请求更新，请刷新 get_factor_config 后重试"
        return published
    return {**published, "actor": actor, "reason": reason,
            "contract": factor_contract.weighted_contract(
                model, normalized, published["version_id"]),
            "note": "权重与版本已在同一事务原子发布；后续筛选使用新版本。"}


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
                               "actor": p.get("actor") or "agent", "reason": reason,
                               "expected_parent_version": current_weight_version(model),
                               "manual_override": True})


if __name__ == "__main__":
    print(json.dumps(get_factor_config({}), ensure_ascii=False, indent=2))

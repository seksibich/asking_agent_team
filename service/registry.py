"""功能注册表与版本机制。

- 所有对外数据/分析功能通过 @register 装饰器注册。
- /functions 返回全部功能索引；data_version 由索引内容哈希自动生成，
  任何功能的新增/描述/参数变化都会改变版本号（无需手工维护版本）。
- SCHEMA_VERSION 仅在发生「破坏性变更」（如调用协议改变）时手工 +1。

扩展方式：在 skills/<skill>/scripts/ 下新增模块，用 @register 声明功能即可，
服务启动时 loader 自动发现，/functions 自动更新，data_version 自动变化。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable, Optional

SCHEMA_VERSION = 1  # 破坏性协议变更时手工 +1

# name -> {name, group, description, params, returns, fn}
_REGISTRY: dict[str, dict[str, Any]] = {}


class ParamError(Exception):
    """参数校验错误。"""


def register(name: str, group: str, description: str,
             params: Optional[list[dict[str, Any]]] = None,
             returns: str = "") -> Callable:
    """注册一个功能。

    params: [{"name","type","required","default","desc"}]
    被装饰函数签名统一为 fn(p: dict) -> Any，返回数据负载。
    """
    def deco(fn: Callable[[dict[str, Any]], Any]) -> Callable:
        if name in _REGISTRY:
            raise RuntimeError(f"duplicate function name: {name}")
        _REGISTRY[name] = {
            "name": name,
            "group": group,
            "description": description,
            "params": params or [],
            "returns": returns,
            "fn": fn,
        }
        return fn
    return deco


def _index_payload() -> list[dict[str, Any]]:
    """不含函数对象的功能索引（用于 /functions 与版本哈希）。"""
    out = []
    for name in sorted(_REGISTRY):
        item = _REGISTRY[name]
        out.append({
            "name": item["name"],
            "group": item["group"],
            "description": item["description"],
            "params": item["params"],
            "returns": item["returns"],
        })
    return out


def data_version() -> str:
    """基于功能索引内容的自动版本号。索引变化则版本变化。"""
    idx = _index_payload()
    raw = json.dumps(idx, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"v{SCHEMA_VERSION}.{digest}"


def functions_index() -> dict[str, Any]:
    """/functions 返回体。"""
    idx = _index_payload()
    groups: dict[str, list[str]] = {}
    for it in idx:
        groups.setdefault(it["group"], []).append(it["name"])
    return {
        "data_version": data_version(),
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(idx),
        "groups": groups,
        "functions": idx,
    }


def _validate(meta: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """按元数据校验并填充默认值。"""
    result: dict[str, Any] = {}
    for spec in meta["params"]:
        pname = spec["name"]
        if pname in params and params[pname] is not None:
            result[pname] = params[pname]
        elif spec.get("required"):
            raise ParamError(f"missing required param: {pname}")
        elif "default" in spec:
            result[pname] = spec["default"]
    return result


def call(name: str, params: Optional[dict[str, Any]] = None) -> Any:
    """按功能名分发调用。"""
    if name not in _REGISTRY:
        raise ParamError(f"unknown function: {name}")
    meta = _REGISTRY[name]
    clean = _validate(meta, params or {})
    return meta["fn"](clean)


def has(name: str) -> bool:
    return name in _REGISTRY


def names() -> list[str]:
    return sorted(_REGISTRY)

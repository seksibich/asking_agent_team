"""选股标签契约：固定标签说明、版本和上传规范化。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_TAG_ITEMS = (
    ("龙头", "题材或产业链中辨识度、带动性最强的领涨标的；前端最高优先高亮。"),
    ("核心", "当前主线中逻辑、资金或事件地位居前的核心标的；前端重点高亮。"),
    ("补涨", "同题材核心已启动后，位置较低且存在跟随修复预期的标的。"),
    ("趋势", "以中短期趋势延续、量价结构稳定为主要特征。"),
    ("连板", "当前处于连续涨停晋级路径，情绪敏感且波动风险高。"),
    ("反包", "分歧或断板后重新转强并覆盖前一日弱势走势。"),
    ("弹性", "相对同题材对事件和资金变化更敏感，潜在波动更大。"),
    ("低位", "在本轮题材或中期区间中位置相对较低，不等同于低风险。"),
    ("高位", "在本轮题材或中期区间中位置较高，需重点检查兑现与退潮风险。"),
    ("逻辑", "主要依据产业链传导、供需或景气逻辑，而非单纯情绪。"),
    ("情绪", "主要受市场热度、涨停生态或资金博弈驱动。"),
    ("预期", "主要交易尚未完全兑现的政策、产品、业绩或事件预期。"),
    ("业绩", "有公告或结构化财务数据支持的业绩变化线索。"),
    ("订单", "有可核验订单、中标或合同信息驱动。"),
    ("涨价", "产品或商品价格变化构成主要驱动，结论需双来源核验。"),
    ("政策", "政策发布、征求意见或执行窗口构成主要催化。"),
    ("事件驱动", "具体可定位事件构成短期主要催化。"),
    ("景气", "行业需求、产能利用或供需格局处于上行阶段。"),
    ("反转", "基本面、供需或市场预期从弱势阶段出现方向性改善。"),
    ("放量", "成交量较近期基线显著放大，需结合价格方向判断。"),
    ("突破", "价格或量价结构突破可核验的关键区间。"),
    ("涨停", "服务端按最新可核验价格与当日涨停价自动补充。"),
    ("跌停", "服务端按最新可核验价格与当日跌停价自动补充。"),
)

CATALOG = tuple({"tag": tag, "description": description} for tag, description in _TAG_ITEMS)
FIXED_TAGS = frozenset(tag for tag, _ in _TAG_ITEMS)
AUTO_MARKET_TAGS = frozenset({"涨停", "跌停"})
TAG_VERSION = "selection-tags-v1." + hashlib.sha256(
    json.dumps(CATALOG, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()[:8]


def normalize_tags(value: Any) -> list[str]:
    """规范化调用方标签；允许固定标签及 Agent 自编排的板块、题材、事件标签。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tags 必须是字符串数组")
    tags: list[str] = []
    for item in value:
        tag = str(item or "").strip()
        if not tag:
            continue
        if len(tag) > 32:
            raise ValueError("单个标签最长 32 个字符")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 24:
        raise ValueError("每只股票最多 24 个标签")
    return tags


def primary_theme(tags: list[str], fallback: str = "") -> str:
    """以热点字段优先，否则取首个非固定标签作为题材聚合键。"""
    if fallback and fallback != "未分类":
        return fallback
    return next((tag for tag in tags if tag not in FIXED_TAGS), "未分类")

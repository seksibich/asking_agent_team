"""涨价链扫描（分析重心第一优先）。

思路：
1. 新闻侧：检索含"涨价/提价/调价/上调/涨停报价"等关键词的财经快讯，作为涨价线索来源之一
2. 价格侧：抓取一批大宗商品/期货主力合约日线，计算近 5 日涨幅，识别价格上行品种
两路信号交叉，输出结构化涨价线索，供 Agent 二次交叉验证后写入观察池。

注意：本脚本只做"信号发现"，不下结论。是否成立由 Agent 结合公告等交叉验证。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
from registry import register

# 涨价关键词（新闻侧）
DEFAULT_KEYWORDS = ["涨价", "提价", "调价", "上调", "报价上涨", "供不应求", "限产", "停产", "涨停报价"]

# 关注的商品/期货主力（价格侧）。可用环境变量 PRICE_HIKE_FUTURES 覆盖（逗号分隔期货代码）。
COMMODITY_FUTURES = [c for c in os.getenv("PRICE_HIKE_FUTURES", "").split(",") if c.strip()]


def _news_signals(keywords: list[str]) -> list[dict[str, Any]]:
    """新闻侧涨价线索。"""
    pro = common.get_pro()
    day = common.today_str()
    start = f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00"
    end = f"{day[:4]}-{day[4:6]}-{day[6:]} 23:59:59"
    try:
        df = pro.news(src="sina", start_date=start, end_date=end)
    except Exception:
        return []
    if df.empty or "content" not in df.columns:
        return []
    pattern = "|".join(keywords)
    hit = df[df["content"].astype(str).str.contains(pattern, na=False)]
    signals = []
    for _, row in hit.head(50).iterrows():
        signals.append({
            "type": "news",
            "time": str(row.get("datetime", "")),
            "content": str(row.get("content", ""))[:200],
            "matched": [k for k in keywords if k in str(row.get("content", ""))],
        })
    return signals


def _price_signals() -> list[dict[str, Any]]:
    """价格侧：计算商品/期货近 5 日涨幅（需相应 tushare 权限）。

    默认清单为占位，用户按自身权限替换为 fut_daily/期货代码或现货价格接口。
    """
    pro = common.get_pro()
    signals: list[dict[str, Any]] = []
    end = common.last_trade_date()
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d")
    for code in COMMODITY_FUTURES:
        code = code.strip()
        if not code:
            continue
        try:
            df = pro.fut_daily(ts_code=code, start_date=start, end_date=end)
        except Exception:
            continue
        if df is None or df.empty or len(df) < 2:
            continue
        df = df.sort_values("trade_date")
        latest = float(df.iloc[-1]["close"])
        base = float(df.iloc[max(0, len(df) - 6)]["close"])
        chg = (latest - base) / base * 100 if base else 0.0
        if chg > 0:
            signals.append({
                "type": "price",
                "code": code,
                "latest": latest,
                "chg_5d_pct": round(chg, 2),
            })
    return signals


# 建议 Agent 自主补充取数的外部渠道（脚本不代抓，仅提示 Agent 去核验）
EXTERNAL_SOURCE_HINTS = [
    "生意社(100ppi)", "百川盈孚", "上海有色网 SMM", "卓创资讯", "中国化工网",
    "Mysteel/钢联", "各行业协会官网",
    "期货行情：大商所/郑商所/上期所主力合约",
    "交易所公告/提价函", "投资者关系互动平台",
]


def scan(keywords: Optional[list[str]] = None) -> dict[str, Any]:
    """涨价链扫描主入口，返回新闻+价格双路信号 + 外部取数提示。

    本脚本只做线索发现；权威价格数据由 Agent 自主到外部行业平台/期货行情获取，
    并 ≥2 来源交叉验证后方可作为涨价结论。
    """
    kws = keywords or DEFAULT_KEYWORDS
    result = {
        "source": "price_hike_scan",
        "fetched_at": common.now_str(),
        "keywords": kws,
        "news_signals": _news_signals(kws),
        "price_signals": _price_signals(),
        "external_source_hints": EXTERNAL_SOURCE_HINTS,
        "note": ("信号仅供线索发现。Agent 应自主到外部行业披露平台/期货行情/公司公告"
                 "补充权威价格数据并 ≥2 来源交叉验证，方可作为涨价结论；以预期驱动，不依赖过往业绩"),
    }
    return result


@register("price_hike_scan", "price_hike",
          "涨价链扫描：新闻侧关键词线索 + 期货价格信号 + 外部取数渠道提示（涨价为第一重心）",
          params=[{"name": "keywords", "type": "array", "required": False,
                   "desc": "自定义涨价关键词，默认涨价/提价/调价等"}],
          returns="news_signals / price_signals / external_source_hints")
def price_hike_scan(p: dict) -> dict:
    return scan(p.get("keywords"))


if __name__ == "__main__":
    import json
    print(json.dumps(scan(), ensure_ascii=False, indent=2))

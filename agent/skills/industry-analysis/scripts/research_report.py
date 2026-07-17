"""投研报告数据包生成。

为某个主题/标的聚合投研所需数据（涨价信号、板块行情、个股行情与基本面、相关新闻），
返回结构化数据包。Agent 拿到后按 industry-analysis 技能撰写报告，
输出到 投研/yyyyMMdd-xx研究报告/。本脚本只备数据，不写结论。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

import common
import price_hike
from registry import register


def _stock_snapshot(pro, code: str, end: str) -> dict[str, Any]:
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=60)).strftime("%Y%m%d")
    snap: dict[str, Any] = {"code": code}
    try:
        d = pro.daily(ts_code=code, start_date=start, end_date=end)
        if not d.empty:
            d = d.sort_values("trade_date")
            snap["last_close"] = float(d.iloc[-1]["close"])
            snap["chg_20d_pct"] = round(
                (float(d.iloc[-1]["close"]) - float(d.iloc[-21]["close"])) / float(d.iloc[-21]["close"]) * 100, 2
            ) if len(d) > 21 else None
    except Exception:
        pass
    try:
        b = pro.daily_basic(ts_code=code, trade_date=end,
                            fields="ts_code,pe,pb,total_mv,turnover_rate")
        if not b.empty:
            snap["basic"] = b.to_dict(orient="records")[0]
    except Exception:
        pass
    try:
        f = pro.forecast(ts_code=code)
        if f is not None and not f.empty:
            snap["forecast"] = f.head(3).to_dict(orient="records")
    except Exception:
        pass
    return snap


def build(theme: str, codes: Optional[list[str]] = None) -> dict[str, Any]:
    """投研数据包主入口。"""
    pro = common.get_pro()
    end = common.last_data_ready_date()

    package: dict[str, Any] = {
        "source": "research/build",
        "fetched_at": common.now_str(),
        "theme": theme,
        "trade_date": end,
    }

    # 涨价信号（第一优先）
    package["price_hike"] = price_hike.scan([theme, "涨价", "提价", "调价", "上调"])

    # 主题相关新闻
    try:
        day = common.today_str()
        news = pro.news(src="sina", start_date=f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00",
                        end_date=f"{day[:4]}-{day[4:6]}-{day[6:]} 23:59:59")
        if not news.empty and "content" in news.columns:
            news = news[news["content"].astype(str).str.contains(theme, na=False)]
            package["news"] = news.head(30).to_dict(orient="records")
        else:
            package["news"] = []
    except Exception:
        package["news"] = []

    # 个股快照
    if codes:
        package["stocks"] = [_stock_snapshot(pro, c, end) for c in codes]

    package["valuation_note"] = ("stocks[].basic 中的 pe/pb 仅作过往业绩对应的估值背景，"
                                 "写入报告风险提示，不作为看多依据；forecast 为前瞻业绩预告(预期)")
    package["note"] = ("数据包仅供撰写投研使用。以预期驱动为主(涨价/景气预期)，"
                       "涨价价格数据须由 Agent 自主到外部行业平台/期货行情 ≥2 来源交叉验证；"
                       "业绩仅在披露期作验证。报告输出到 投研/yyyyMMdd-" + theme + "研究报告/")
    return package


@register("research_build", "research",
          "投研数据包：聚合涨价信号+主题新闻+个股快照/预告，供撰写研究报告",
          params=[{"name": "theme", "type": "string", "required": True, "desc": "研究主题/行业/事件"},
                  {"name": "codes", "type": "array", "required": False, "desc": "相关个股代码"}],
          returns="price_hike / news / stocks 数据包")
def research_build(p: dict) -> dict:
    return build(p["theme"], p.get("codes"))


if __name__ == "__main__":
    import json
    theme = sys.argv[1] if len(sys.argv) > 1 else "涨价"
    codes = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    print(json.dumps(build(theme, codes), ensure_ascii=False, indent=2))

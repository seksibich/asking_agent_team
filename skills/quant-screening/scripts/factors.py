"""量化因子库（趋势 + 情绪，回测有效因子）。

设计依据（A股/通用回测经验）：
- 个股层面：12-1 动量偏弱、短期(1个月)呈反转；低特质波动、低换手为正向 alpha。
  => 个股选股用「12-1 动量 + 1个月反转 + 低波动 + 低换手 + 趋势确认」。
- 行业/板块层面：动量为正（板块轮动具延续性），短期动量亦正向。
  => 选板块用「板块 12-1 / 20 日 / 1个月动量 + 低波动 + 量能确认」。

每个因子已按「值越大越好（预期收益越高）」对齐方向，故合成时权重全为正。
本库只产出技术面趋势/情绪因子，涨价/景气等基本面由 Agent 按 priority-framework 交叉验证叠加。
"""
from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---------------- 个股因子默认权重（sum=1.0） ----------------
# 侧重回测有效的趋势+反转+低波动组合
STOCK_FACTOR_WEIGHTS: dict[str, float] = {
    "mom_12_1": 0.16,        # 12-1 动量（趋势，剔除最近1个月避免短期反转污染）
    "trend_ma": 0.14,        # 均线多头排列强度（趋势）
    "high_52w": 0.09,        # 距52周高点接近度（趋势，52周高点因子）
    "reversal_1m": 0.22,     # 1个月反转（情绪，近月超跌反弹，A股显著）
    "low_turnover": 0.13,    # 低换手（情绪/流动性，高换手未来收益低）
    "low_ivol": 0.20,        # 低波动（特质波动率，低波动异象）
    "vol_confirm": 0.06,     # 量能确认（温和放量）
}

# ---------------- 板块因子默认权重（sum=1.0） ----------------
# 板块层面动量为正，短期动量亦延续
SECTOR_FACTOR_WEIGHTS: dict[str, float] = {
    "sec_mom_12_1": 0.30,    # 板块 12-1 动量（中期趋势）
    "sec_mom_20d": 0.25,     # 板块 20 日动量（近端趋势延续）
    "sec_mom_5d": 0.15,      # 板块 5 日动量（情绪热度延续）
    "sec_vol_confirm": 0.10, # 板块量能确认（放量上行）
    "sec_low_vol": 0.20,     # 板块低波动（稳健趋势优于暴涨暴跌）
}

# ---------------- 趋势选股权重（sum=1.0，侧重趋势因子，弱化反转） ----------------
TREND_FACTOR_WEIGHTS: dict[str, float] = {
    "mom_12_1": 0.28,
    "trend_ma": 0.24,
    "high_52w": 0.18,
    "reversal_1m": 0.05,
    "low_turnover": 0.08,
    "low_ivol": 0.10,
    "vol_confirm": 0.07,
}

# ---------------- 情绪温度指标权重（sum=1.0，0-100 温度合成） ----------------
# 各指标均为"越高越热"，按当天之前 7 日窗口做 min-max 归一后加权
SENTIMENT_FACTOR_WEIGHTS: dict[str, float] = {
    "adv_dec_ratio": 0.25,   # 大盘涨跌家数比（上涨家数占比）
    "limit_updown": 0.20,    # 涨跌停家数（涨停占比，情绪极值）
    "sector_ratio": 0.15,    # 板块涨跌比（上涨板块占比）
    "turnover": 0.15,        # 大盘成交额（量能）
    "index_mom": 0.15,       # 大盘指数动量
    "avg_price_mom": 0.10,   # 平均股价指数动量
}

TRADING_DAYS_YEAR = 252
TRADING_DAYS_MONTH = 21


def _safe(v: float) -> float:
    return 0.0 if (v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))) else float(v)


def _ret(series: pd.Series, lookback: int, skip: int = 0) -> float:
    """区间收益：从 -(lookback+skip) 到 -1-skip 的累计收益率。"""
    n = len(series)
    if n < lookback + skip + 1:
        return 0.0
    end = series.iloc[-1 - skip]
    start = series.iloc[-(lookback + skip) - 1] if (lookback + skip) < n else series.iloc[0]
    return _safe((end - start) / start) if start else 0.0


# ================= 个股因子 =================
def compute_stock_factors(df: pd.DataFrame, turnover_rate: Optional[float] = None) -> Optional[dict[str, Any]]:
    """计算单只个股的趋势/情绪因子原始值。

    df 需含 close, vol 列，按 trade_date 升序。数据不足 60 日返回 None。
    turnover_rate 为最新换手率（%），来自 daily_basic，可选。
    返回值均已对齐为「越大越好」。
    """
    if df is None or df.empty or len(df) < 60:
        return None
    df = df.sort_values("trade_date")
    close = df["close"].astype(float).reset_index(drop=True)
    vol = df["vol"].astype(float).reset_index(drop=True)
    price = float(close.iloc[-1])

    # 12-1 动量：过去 252 日、剔除最近 21 日
    mom_12_1 = _ret(close, TRADING_DAYS_YEAR - TRADING_DAYS_MONTH, skip=TRADING_DAYS_MONTH)
    # 1 个月反转：最近 21 日收益取负（近月跌得多者预期反弹）
    reversal_1m = -_ret(close, TRADING_DAYS_MONTH, skip=0)

    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    # 趋势确认：多头排列 + 相对 ma60 的乖离
    trend_ma = (1.0 if price > ma20 else 0.0) + (1.0 if ma20 > ma60 else 0.0)
    trend_ma += _safe((price - ma60) / ma60) if ma60 else 0.0

    # 距 52 周高点接近度（越接近高点越强，上限 1.0）
    high_252 = float(close.tail(TRADING_DAYS_YEAR).max())
    high_52w = _safe(price / high_252) if high_252 else 0.0

    # 特质波动率近似：过去 60 日日收益标准差，取负（低波动更优）
    daily_ret = close.pct_change().tail(60)
    low_ivol = -_safe(float(daily_ret.std(ddof=0)))

    # 低换手：换手率取负（无数据则 0）
    low_turnover = -_safe(turnover_rate) if turnover_rate is not None else 0.0

    # 量能确认：近 5 日均量 / 前 20 日均量，温和放量为正，极端放量截断
    base_vol = float(vol.tail(25).head(20).mean())
    vr = _safe(vol.tail(5).mean() / base_vol) if base_vol else 1.0
    vol_confirm = min(vr, 3.0)  # 截断防爆量污染

    return {
        "mom_12_1": mom_12_1,
        "trend_ma": trend_ma,
        "high_52w": high_52w,
        "reversal_1m": reversal_1m,
        "low_turnover": low_turnover,
        "low_ivol": low_ivol,
        "vol_confirm": vol_confirm,
        "price": round(price, 2),
    }


# ================= 板块因子 =================
def compute_sector_factors(df: pd.DataFrame) -> Optional[dict[str, Any]]:
    """计算单个板块指数的趋势因子原始值。

    df 需含 close, vol（或 amount）列，按 trade_date 升序。板块层面动量为正。
    """
    if df is None or df.empty or len(df) < 30:
        return None
    df = df.sort_values("trade_date")
    close = df["close"].astype(float).reset_index(drop=True)
    vol_col = "vol" if "vol" in df.columns else ("amount" if "amount" in df.columns else None)

    sec_mom_12_1 = _ret(close, TRADING_DAYS_YEAR - TRADING_DAYS_MONTH, skip=TRADING_DAYS_MONTH) \
        if len(close) >= TRADING_DAYS_YEAR else _ret(close, len(close) - TRADING_DAYS_MONTH - 1, skip=TRADING_DAYS_MONTH)
    sec_mom_20d = _ret(close, 20)
    sec_mom_5d = _ret(close, 5)

    daily_ret = close.pct_change().tail(60)
    sec_low_vol = -_safe(float(daily_ret.std(ddof=0)))

    if vol_col:
        v = df[vol_col].astype(float).reset_index(drop=True)
        base = float(v.tail(25).head(20).mean())
        sec_vol_confirm = min(_safe(v.tail(5).mean() / base), 3.0) if base else 1.0
    else:
        sec_vol_confirm = 1.0

    return {
        "sec_mom_12_1": sec_mom_12_1,
        "sec_mom_20d": sec_mom_20d,
        "sec_mom_5d": sec_mom_5d,
        "sec_vol_confirm": sec_vol_confirm,
        "sec_low_vol": sec_low_vol,
        "last_close": round(float(close.iloc[-1]), 2),
    }


# ================= 合成 =================
def zscore(s: pd.Series) -> pd.Series:
    """横截面标准化。全同值时返回 0。"""
    std = s.std(ddof=0)
    if not std or np.isnan(std):
        return pd.Series([0.0] * len(s), index=s.index)
    return (s - s.mean()) / std


def composite_score(table: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """对因子表做横截面 z-score 后加权合成，新增 z_* 列与 score 列。"""
    for f in weights:
        if f in table.columns:
            table[f"z_{f}"] = zscore(table[f])
        else:
            table[f"z_{f}"] = 0.0
    table["score"] = sum(table[f"z_{f}"] * w for f, w in weights.items())
    return table

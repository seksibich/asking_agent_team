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

# 个股因子公式版本：公式、字段或计算口径变化时递增，旧预计算不会被新选股误用。
STOCK_FACTOR_VERSION = "stock-factors-v2"

# ---------------- 个股因子默认权重（sum=1.0） ----------------
# 侧重回测有效的趋势+反转+低波动组合，并叠加每日持久化的行业强度。
# 下半部分为「候选因子」，默认权重 0（不参与打分、不在选股列表展示）；
# 依据前沿学术论文 / 量化机构常用 / A股回测经验预置，需要时在「权重配置」页调高权重启用。
# 所有因子均已对齐为「值越大越好（预期收益越高）」，故合成权重恒为正。
STOCK_FACTOR_WEIGHTS: dict[str, float] = {
    # —— 默认启用（sum=1.0）——
    "mom_12_1": 0.15,        # 12-1 动量（趋势，剔除最近1个月避免短期反转污染）
    "trend_ma": 0.13,        # 均线多头排列强度（趋势）
    "high_52w": 0.09,        # 距52周高点接近度（趋势，52周高点因子）
    "reversal_1m": 0.18,     # 1个月反转（情绪，近月超跌反弹，A股显著）
    "low_turnover": 0.11,    # 低换手（情绪/流动性，高换手未来收益低）
    "low_ivol": 0.14,        # 低波动（特质波动率，低波动异象）
    "vol_confirm": 0.06,     # 量能确认（温和放量）
    "industry_strength": 0.14,  # 所属申万一级行业每日量化评分分位（板块轮动顺势）
    # —— 候选因子（默认 0，学术/机构常用，按需启用）——
    "mom_6_1": 0.0,          # 6-1 中期动量（Jegadeesh-Titman 1993；中短期趋势延续）
    "max_lottery": 0.0,      # MAX 彩票效应反向（Bali/Cakici/Whitelaw 2011；高博彩性未来收益低）
    "downside_vol": 0.0,     # 下行波动率取负（Ang/Chen/Xing 2006；下行风险溢价）
    "amihud_illiq": 0.0,     # Amihud 非流动性（2002；非流动性溢价，长周期正向，短线慎用）
    "small_size": 0.0,       # 规模因子 −ln(流通市值)（Fama-French SMB 1993；小市值溢价）
    "value_bm": 0.0,         # 账面市值比 B/M=1/PB（Fama-French HML；价值溢价）
    "earnings_yield": 0.0,   # 盈利收益率 E/P=1/PE_TTM（价值/质量；本项目 PE 仅作背景，默认 0）
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
    "mom_12_1": 0.25,
    "trend_ma": 0.21,
    "high_52w": 0.15,
    "reversal_1m": 0.05,
    "low_turnover": 0.07,
    "low_ivol": 0.08,
    "vol_confirm": 0.07,
    "industry_strength": 0.12,
}

# ---------------- 情绪温度指标权重（sum=1.0，0-100 温度合成） ----------------
# 各指标均为"越高越热/越偏多"，按当天之前 7 日窗口做 min-max 归一后加权。
# 振幅方向信号 + 实体长度（大盘指数 / 平均股价指数）均按百分点位口径，低权重参与。
# 注：原 index_kline（大盘K线形态）已由 index_body + index_amp 覆盖（阳阴实体 + 影线方向），故移除。
SENTIMENT_FACTOR_WEIGHTS: dict[str, float] = {
    "adv_dec_ratio": 0.22,   # 大盘涨跌家数比（上涨家数占比）
    "limit_up": 0.12,        # 涨停家数（越多越热，正向）
    "limit_down": 0.06,      # 跌停家数（越多越冷，反向计分）
    "sector_ratio": 0.13,    # 板块涨跌比（上涨板块占比）
    "turnover": 0.12,        # 大盘成交额（量能）
    "index_mom": 0.12,       # 大盘指数动量
    "avg_price_mom": 0.07,   # 平均股价指数动量（全市场平均涨跌幅）
    "index_body": 0.05,      # 大盘指数实体长度（百分点，长阳高分/长阴低分/短实体中性）
    "index_amp": 0.05,       # 大盘指数振幅方向信号（百分点，长下影高分/长上影低分/小振幅中性）
    "avg_price_body": 0.03,  # 平均股价指数实体长度（百分点）
    "avg_price_amp": 0.03,   # 平均股价指数振幅方向信号（百分点）
}

TRADING_DAYS_YEAR = 252
TRADING_DAYS_HALF_YEAR = 126
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
def compute_stock_factors(df: pd.DataFrame, turnover_rate: Optional[float] = None,
                          basic: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """计算单只个股的趋势/情绪 + 候选因子原始值。

    df 需含 close, vol 列（可选含 amount 列用于流动性因子），按 trade_date 升序。
    数据不足 60 日返回 None。
    turnover_rate 为最新换手率（%），来自 daily_basic，可选。
    basic 为该股最新 daily_basic 派生字段（pe_ttm/pb/circ_mv 等），可选；
    缺失时对应候选因子取 0（中性，默认权重 0 不影响打分）。
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

    # ============ 候选因子（默认权重 0，需要时在权重配置启用）============
    rets = close.pct_change()

    # 6-1 中期动量：过去约 126 日、剔除最近 21 日（中短期趋势延续）
    mom_6_1 = _ret(close, TRADING_DAYS_HALF_YEAR - TRADING_DAYS_MONTH, skip=TRADING_DAYS_MONTH)

    # MAX 彩票效应反向：过去 21 日最大单日涨幅，取负（高博彩性个股未来收益偏低）
    last21 = rets.tail(TRADING_DAYS_MONTH).dropna()
    max_lottery = -_safe(float(last21.max())) if len(last21) else 0.0

    # 下行波动率取负：近 60 日仅负收益的标准差（下行风险溢价，低下行波动更优）
    r60 = rets.tail(60).dropna()
    neg = r60[r60 < 0]
    downside_vol = -_safe(float(neg.std(ddof=0))) if len(neg) > 1 else 0.0

    # Amihud 非流动性：近 20 日 mean(|日收益| / 成交额)，越大越不流动（非流动性溢价，正向）
    amihud_illiq = 0.0
    if "amount" in df.columns:
        amt = df["amount"].astype(float).reset_index(drop=True)
        m = min(20, len(amt) - 1)
        if m > 0:
            rr = rets.reset_index(drop=True).tail(m).abs()
            aa = amt.tail(m).replace(0.0, np.nan)
            ratio = (rr / aa).dropna()
            # amount 单位千元，比值极小，放大到可读量级（横截面 zscore 不改排序）
            amihud_illiq = _safe(float(ratio.mean()) * 1e6) if len(ratio) else 0.0

    # daily_basic 派生（规模 / 价值 / 盈利收益率）
    small_size = 0.0
    value_bm = 0.0
    earnings_yield = 0.0
    if basic:
        circ_mv = basic.get("circ_mv")            # 流通市值（万元）
        if circ_mv and float(circ_mv) > 0:
            small_size = -_safe(math.log(float(circ_mv)))   # −ln(市值)：市值越小值越大
        pb = basic.get("pb")
        if pb and float(pb) > 0:
            value_bm = _safe(1.0 / float(pb))               # 账面市值比 B/M（越高越“便宜”）
        pe = basic.get("pe_ttm") if basic.get("pe_ttm") not in (None, "") else basic.get("pe")
        if pe and float(pe) > 0:
            earnings_yield = _safe(1.0 / float(pe))          # 盈利收益率 E/P

    return {
        "mom_12_1": mom_12_1,
        "trend_ma": trend_ma,
        "high_52w": high_52w,
        "reversal_1m": reversal_1m,
        "low_turnover": low_turnover,
        "low_ivol": low_ivol,
        "vol_confirm": vol_confirm,
        # 候选因子
        "mom_6_1": mom_6_1,
        "max_lottery": max_lottery,
        "downside_vol": downside_vol,
        "amihud_illiq": amihud_illiq,
        "small_size": small_size,
        "value_bm": value_bm,
        "earnings_yield": earnings_yield,
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


def composite_score(table: pd.DataFrame, weights: dict[str, float],
                    strict: bool = True) -> pd.DataFrame:
    """横截面 z-score 加权；默认严格要求完整因子契约，并同时生成 0~1 分位分。"""
    missing = [name for name in weights if name not in table.columns]
    if missing and strict:
        raise ValueError(f"因子数据缺少契约成分：{','.join(missing)}")
    for f in weights:
        table[f"z_{f}"] = zscore(table[f]) if f in table.columns else 0.0
    table["score"] = sum(table[f"z_{f}"] * w for f, w in weights.items())
    table["score_percentile"] = table["score"].rank(method="average", pct=True)
    return table

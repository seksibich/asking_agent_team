---
name: quant-screening
description: 量化多因子选股与选板块，采用回测有效的趋势+情绪因子（12-1动量、1个月反转、低波动、低换手、52周高点、板块动量轮动）。调用 screen_quant 与 screen_sector。
user-invocable: true
disable-model-invocation: false
---

# 量化选股 & 选板块（趋势 + 情绪因子）

因子引擎在 `service/scripts/factors.py`，个股与板块分别用不同因子方向（见下）。
量化结果只是候选，最终须由 Agent 叠加 `priority-framework`（涨价>逻辑>预期>情绪）交叉验证。

## 关键回测结论（因子方向的依据）

- **个股层面**：12-1 动量（剔除最近 1 个月）有效；最近 1 个月呈**反转**（近月超跌者反弹）；低特质波动、低换手率是稳定正向 alpha。→ 个股既用中期动量、又用短期反转，二者互补。
- **板块/行业层面**：动量**为正**且具延续性（板块轮动），短期动量亦正向。→ 选板块用动量延续，不做反转。

> 因此选股与选板块口径不同：个股短期反转、板块短期动量。这是本因子库的核心设计。

## 一、个股量化选股（screen_quant）

### 因子与默认权重（横截面 z-score 加权，sum=1.0）

| 因子 | 维度 | 方向 | 权重 | 说明 |
|---|---|---|---|---|
| `mom_12_1` | 趋势 | + | 0.16 | 过去 252 日剔除最近 21 日的收益 |
| `trend_ma` | 趋势 | + | 0.14 | 多头排列（price>ma20>ma60）+ 乖离 |
| `high_52w` | 趋势 | + | 0.09 | 距 52 周高点接近度 |
| `reversal_1m` | 情绪 | + | 0.22 | 最近 21 日收益取负（短期反转） |
| `low_turnover` | 情绪 | + | 0.13 | 换手率取负（高换手未来收益低） |
| `low_ivol` | 情绪 | + | 0.20 | 近 60 日日收益波动取负（低波动异象） |
| `vol_confirm` | 量能 | + | 0.06 | 近 5 日/前 20 日均量，温和放量（截断防爆量） |

所有因子已对齐「值越大越好」，故权重全正。可通过请求体 `weights` 覆盖默认权重做因子测试。

### 请求示例
```json
POST /call
{"function": "screen_quant",
 "params": {"industries": ["光伏", "半导体"], "top_n": 30,
            "weights": {"reversal_1m": 0.30, "low_ivol": 0.25}}}
```
`industries` 省略则全市场（自动剔除 ST/退）。

### Agent 复核流程
1. 拿到 `candidates`（含各因子值与合成 `score`）
2. 对 Top N 逐一叠加**涨价/景气预期**判断（`price-hike/scan` + 外部行业价格/期货数据交叉验证），按四维打分；以预期驱动，不以过往业绩为主（业绩披露期除外）
3. 剔除逻辑不符、数据异常者；情绪主导（涨价+逻辑均<0.4）标高风险
4. 产出 `选股/yyyyMMdd-量化选股.md`（含因子表 + 四维打分表 + 数据来源）
5. 强信号追加 `predictions.jsonl`（driver 标注），线索入 `观察池`

## 二、选板块 / 板块轮动（screen_sector）

### 因子与默认权重（sum=1.0）

| 因子 | 方向 | 权重 | 说明 |
|---|---|---|---|
| `sec_mom_12_1` | + | 0.30 | 板块 12-1 中期动量 |
| `sec_mom_20d` | + | 0.25 | 板块 20 日动量（近端延续） |
| `sec_mom_5d` | + | 0.15 | 板块 5 日动量（情绪热度） |
| `sec_vol_confirm` | + | 0.10 | 板块量能确认（放量上行） |
| `sec_low_vol` | + | 0.20 | 板块低波动（稳健趋势优先） |

数据源：申万一级行业指数（`sw_daily` + `index_classify`），无权限回退概念/板块指数。

### 请求示例
```json
POST /call
{"function": "screen_sector",
 "params": {"top_n": 10, "with_stocks": true, "stocks_per_sector": 5}}
```
`with_stocks=true` 时，对排名前 3 的板块自动在成分股内跑个股量化选股，返回 `stock_picks_by_sector`。

### Agent 复核流程
1. 拿到板块排名 `sectors`（含各动量因子与 `score`）
2. 对强势板块叠加**涨价链/景气逻辑**验证（是否有真实涨价或业绩支撑，非纯情绪轮动）
3. 在通过验证的板块内选个股（`stock_picks_by_sector` 或再调 `screen_trend`）
4. 产出 `选股/yyyyMMdd-板块轮动.md`（板块打分表 + 主线四维打分 + 个股候选 + 数据来源）

## 三、因子权重可配置（服务端）

三个模型的因子权重可运行时配置，选股脚本运行时读取生效权重：
- `stock`（screen_quant）/ `sector`（screen_sector）/ `trend`（screen_trend）

### 查看当前配置
```json
POST /call
{"function": "get_factor_config", "params": {"model": "stock"}}
```
返回 `canonical_factors`（规范因子列表）、`weights`（生效权重）、`source`（default/override）。

### 更新权重（调参落地）
```json
POST /call
{"function": "set_factor_weights",
 "params": {"model": "stock",
            "weights": {"mom_12_1":0.16,"trend_ma":0.14,"high_52w":0.09,
                        "reversal_1m":0.22,"low_turnover":0.13,"low_ivol":0.20,"vol_confirm":0.06}}}
```
校验规则（任一不满足即被拒并返回原因）：
- 必须提交该模型的**全部**规范因子，不能缺失、不能多余、名称须一致
- 权重之和必须为 1.0（容差 0.01）

失败返回含 `expected_factors`/`missing`/`unexpected`/`hint`：**先 `get_factor_config` 同步最新因子列表**（服务端可能已新增因子），补齐后重试。这是回测→调参闭环的落地步骤（见 review-learning 与 backtest-analyst）。

## 严谨要求
- 因子值全部来自服务端真实行情计算，标注 `trade_date`
- 量化只给候选，不下确定性结论、不承诺收益
- 个股用反转、板块用动量，不可混用方向
- 最终入选须涨价或逻辑（预期）维度支撑，纯情绪/纯技术信号标注高风险
- **因子体系不含 PE**；PE/PB 只在报告风险提示中作过往估值背景，不作为选股或看多依据
- 调参须基于 `selection_backtest` 的真实回测证据，不可主观臆调

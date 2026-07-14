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

### 因子（横截面 z-score 加权合成，权重和=1.0）

> **权重不写死**：当前规范因子列表与生效权重一律以 `get_factor_config(model=stock)` 返回为准（服务端可增删因子/改权重）。下表仅说明因子语义与方向，不列具体权重。`screen_quant` 只返回并展示**权重≠0**的因子列。

**默认启用因子**（方向均为 +，值越大越好）：

| 因子 | 维度 | 说明 |
|---|---|---|
| `mom_12_1` | 趋势 | 过去 252 日剔除最近 21 日的收益 |
| `trend_ma` | 趋势 | 多头排列（price>ma20>ma60）+ 乖离 |
| `high_52w` | 趋势 | 距 52 周高点接近度 |
| `reversal_1m` | 情绪 | 最近 21 日收益取负（短期反转） |
| `low_turnover` | 情绪 | 换手率取负（高换手未来收益低） |
| `low_ivol` | 情绪 | 近 60 日日收益波动取负（低波动异象） |
| `vol_confirm` | 量能 | 近 5 日/前 20 日均量，温和放量（截断防爆量） |

**候选因子**（默认权重 0，不参与打分也不展示；源自学术/机构常用，按需在权重配置启用）：
`mom_6_1`(6-1中期动量) · `max_lottery`(MAX彩票效应反向) · `downside_vol`(下行波动率反向) · `amihud_illiq`(Amihud非流动性) · `small_size`(规模/小市值) · `value_bm`(账面市值比B/M) · `earnings_yield`(盈利收益率E/P)。

所有因子已对齐「值越大越好」，故权重全正。可通过请求体 `weights` 临时覆盖默认权重做因子测试（无需落库）。

### 请求示例
```json
POST /call
{"function": "screen_quant",
 "params": {"industries": ["光伏", "半导体", "机器人", "PCB铜箔"], "top_n": 30,
            "weights": {"reversal_1m": 0.30, "low_ivol": 0.25}}}
```
- `industries` 省略则全市场（自动剔除 ST/退）。
- **多源模糊匹配**：单个词条在 tushare 行业(stock_basic) + 申万 L1/L2/L3 + 同花顺/东财概念内取并集，支持"机器人/PCB铜箔/电子布"等细分主题与产业链概念。
- **多词条取交集（层层收窄）**：传多个词条时返回**同时属于全部词条**的个股，用于产业链下钻定位。例如 `["通信","PCB","铜箔"]` = 通信∩PCB∩铜箔，而非任一命中的并集。某词条无任何命中则交集为空。
- **输入宽容**：大小写不敏感、自动去首尾空格、中英文逗号(`，`/`,`)均可切分；可传数组或逗号分隔字符串。

### 选股数量
- **自动跑（定时任务 T7/W1 等由调度触发的选股）：`top_n=50`**。
- 手动/网页触发可自定义（网页默认 30）。

### Agent 复核流程
1. 拿到 `candidates`（含各因子值与合成 `score`）
2. 对候选逐一叠加**涨价/景气预期**判断（`price-hike/scan` + 外部行业价格/期货数据交叉验证），按四维打分；以预期驱动，不以过往业绩为主（业绩披露期除外）
3. 剔除逻辑不符、数据异常者；情绪主导（涨价+逻辑均<0.4）标高风险
4. **逐股定位板块/行业 + 炒作路径，并把关联个股归组**：用 `sector_dc`/`screen_sector`/申万行业为每只标的确定行业与细分环节，将同板块/同主线/同产业链（上下游、同题材、同涨价链、同催化）的个股放到一组；每组结合 `news_flash`/`news_filter`/`price_hike_scan` 的**消息面与行业新闻**、以及**近期主线**（观察池/板块动量）综合解读，标注每只的**炒作路径**（题材由来→催化→当前阶段）
5. 产出 `选股/yyyyMMdd-量化选股.md`：因子表 + 四维打分表 + **量化选股分组解读表（output-format 表格5，按主线/产业链归组）** + 数据来源
6. 强信号追加 `predictions.jsonl`（driver 标注），线索入 `观察池`；符合自动选股定义的用 `log_selection(category=auto)` 登记

## 二、选板块 / 板块轮动（screen_sector）

### 因子（方向均为 +，权重和=1.0；权重以 `get_factor_config(model=sector)` 为准）

| 因子 | 说明 |
|---|---|
| `sec_mom_12_1` | 板块 12-1 中期动量 |
| `sec_mom_20d` | 板块 20 日动量（近端延续） |
| `sec_mom_5d` | 板块 5 日动量（情绪热度） |
| `sec_vol_confirm` | 板块量能确认（放量上行） |
| `sec_low_vol` | 板块低波动（稳健趋势优先） |

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

### 更新权重（调参落地，署名 + 留痕）
**必须先 `get_factor_config(model=...)` 取回 `canonical_factors`，再提交其中的全部因子**（含默认 0 的候选因子），不能凭记忆写死一份清单——否则会因缺失/多余被拒。**每次修改都要署名 `actor` 并写 `reason`**（回测证据），服务端会生成类 commit 的 `version_id` 留痕：
```json
POST /call
{"function": "set_factor_weights",
 "params": {"model": "stock",
            "weights": {"<canonical_factors 里的每个因子>": 0.0, "...": "..."},
            "actor": "回测分析师", "reason": "回测30日超额: mom_12_1 领先, 情绪转负 → 小步上调动量/下调情绪"}}
```
校验规则（任一不满足即被拒并返回原因）：
- 必须提交该模型的**全部**规范因子，不能缺失、不能多余、名称须一致
- 权重之和必须为 1.0（容差 0.01）；不想启用的候选因子填 0
- 自主微调只动**权重≠0**的因子、小步（单因子 ≤0.03）后归一；0 权重候选保持 0

返回含 `version_id`（留痕版本，写入学习日志）。失败含 `expected_factors`/`missing`/`unexpected`/`hint`：**先 `get_factor_config` 同步最新因子列表**后重试。

### 配置版本留痕 / 定位 / 回滚
- `get_config_history {model|config_key, limit}`：查某模型权重（或 `sentiment_window`）的变更历史（倒序，含 actor/reason/version_id/parent）。
- `get_config_version {version_id}`：按版本号定位当时的完整权重快照。
- `restore_config_version {version_id, actor, reason}`：回滚到历史版本（回滚亦留痕为新版本）。

## 严谨要求
- 因子值全部来自服务端真实行情计算，标注 `trade_date`
- 量化只给候选，不下确定性结论、不承诺收益
- 个股用反转、板块用动量，不可混用方向
- 最终入选须涨价或逻辑（预期）维度支撑，纯情绪/纯技术信号标注高风险
- **因子体系不含 PE**；PE/PB 只在报告风险提示中作过往估值背景，不作为选股或看多依据
- 调参须基于 `selection_backtest` 的真实回测证据，不可主观臆调

---
name: quant-screening
description: 量化多因子选股与选板块，采用回测有效的趋势+情绪因子（12-1动量、1个月反转、低波动、低换手、52周高点、板块动量轮动）。调用 screen_quant 与 screen_sector。
user-invocable: true
disable-model-invocation: false
---

# 量化选股 & 选板块（趋势 + 情绪因子）

因子引擎在 `skills/quant-screening/scripts/factors.py`，个股与板块分别用不同因子方向（见下）。
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

### Agent 复核与正式理由流程
1. 拿到 `candidates`（含各因子值与合成 `score`），记录量化综合分、权重非零的关键因子及 `trade_date`。
2. 对候选逐一叠加涨价/景气预期判断（`price_hike_scan` + 外部行业价格/期货数据交叉验证），按四维打分；业绩披露期的数据也只作可核验事实，不能宣称必然利好。
3. 剔除逻辑不符、数据异常者；情绪主导（涨价+逻辑均<0.4）标高风险。
4. 逐股定位板块/行业/产业链，并用 `screen_sector`、`sector_dc` 等给出板块短期动量、中期动量、量能和阶段；把同板块/主线/产业链的关联个股归组，结合消息与行业新闻说明炒作路径。
5. 判断与当前主线关系，统一使用「核心/分支/补涨/非主线」；不得把板块归属直接等同于主线地位。
6. 每只正式量化候选按固定理由链输出：**量化信号 → 板块趋势 → 当前主线关系 → 涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪**。任一环缺资料必须原位写「无可核验证据」，不得省略或臆造。
7. 产出 `选股/yyyyMMdd-量化选股.md`：因子表、四维打分表、量化选股分组解读表、`skills/output-format/SKILL.md` 的「正式候选综合理由表」、数据来源。表格逐只保留量化综合分与关键因子、四维分、板块/产业链、板块趋势、主线关系、催化与炒作路径、理由链、情绪择时、风险/证伪。
8. 只有独立通过正式流程的强信号才按既有规则追加 `predictions.jsonl`、更新观察池并 `log_selection(category=auto)`；进入业绩增长参考池本身不得触发任何持久化。

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

## Skill 加载约束 / 依赖 Skills

- 使用前完整读取本文件并确认固定 11 Skills 已完整加载；不得只凭因子名、接口索引或角色摘要运行。
- **因子引擎路径**：`skills/quant-screening/scripts/factors.py`（唯一正确文档路径）。
- **直接依赖**：`data-service`（接口与 fallback）、`priority-framework`（候选复核）、`output-format`（分组报告）。
- **协同 Skills**：`industry-analysis`、`stock-screening`、`post-market`、`review-learning`。

## 量化报告 fallback

板块/指数上下文引用 `market_index` 时，允许数组或逗号字符串；失败、空或部分 code 缺失则逐 code 调 `market_daily(code,start,end)`，取最近记录并标 `degraded`/实际日期。候选分组新闻遇 `news_flash` 402 时用 `news_filter(keyword)+news_cctv+外部搜索`；同源失败继续 cctv+至少两个可信外部来源；全失败标“消息面不可用”，不得当作无风险。T7/W1/M1 中非关键接口失败不阻塞其余候选与报告，缺失因子不得臆造。
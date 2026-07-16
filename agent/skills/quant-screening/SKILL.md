---
name: quant-screening
description: 量化多因子选股与选板块，采用回测有效的趋势+情绪因子（12-1动量、1个月反转、低波动、低换手、52周高点、板块动量轮动）。调用 screen_quant 与 screen_sector。
user-invocable: true
disable-model-invocation: false
---

# 量化选股 & 选板块（趋势 + 情绪因子）

因子引擎在 `skills/quant-screening/scripts/factors.py`，个股与板块分别用不同因子方向（见下）。
量化结果只是候选，最终须由 Agent 叠加 `priority-framework`（涨价>逻辑>预期>情绪）交叉验证。

## 自动与用户主动双入口（强制）

- **自动入口**：T7/W1 等调度任务由消息面、热榜、涨停连板、量能与资金聚类识别动态题材/具体事件，再映射候选并执行 `screen_quant` / `screen_trend`。
- **用户主动入口**：用户可指定具体事件、行业导向或热门板块；先扩展产业链与直接/间接受益候选，再执行相同量化筛选。题材可独立于传统行业板块，不得被申万、东财、同花顺分类限制。
- 两个入口均按“题材/事件 → 个股”分组，统一计算：`综合选股分 = 利好程度×0.35 + 题材热度×0.25 + 量化横截面分位×0.40`，三项均为 0~100；`择时后评分 = 综合选股分 × buy_weight_hint`。
- 原始量化 score 若为 z-score，必须在当批候选内先转 percentile；四维硬门槛继续生效，不得以高量化分绕过。
- 用户主动入口形成**正式候选**后逐只 `log_selection(category=manual)`，保存选股时价格、热点/事件、炒作路线地位、完整理由链和全部量化因子快照；仅普通研究或未通过正式门槛的线索保持 `ephemeral`。
- `manual` 做隔离的 1/3/7/30 日回测，但永不进入 auto 胜率、`tuning_hints` 或调参；用户要求持续跟踪时再补记 `watch`。

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
| `industry_strength` | 行业趋势 | 同交易日申万一级行业量化评分横截面分位（0~1），顺应行业轮动 |

**候选因子**（默认权重 0，不参与打分也不展示；源自学术/机构常用，按需在权重配置启用）：
`mom_6_1`(6-1中期动量) · `max_lottery`(MAX彩票效应反向) · `downside_vol`(下行波动率反向) · `amihud_illiq`(Amihud非流动性) · `small_size`(规模/小市值) · `value_bm`(账面市值比B/M) · `earnings_yield`(盈利收益率E/P)。

所有因子已对齐「值越大越好」，故权重全正。可通过请求体 `weights` 临时覆盖默认权重做因子测试（无需落库）。

### 请求示例

按行业与市场范围筛选：
```json
POST /call
{"function": "screen_quant",
 "params": {"industries": ["光伏", "半导体"], "boards": ["main", "star"], "top_n": 30}}
```

按指定个股与市场范围筛选（存在 `stock_names` 时不再传 `industries`）：
```json
POST /call
{"function": "screen_quant",
 "params": {"stock_names": ["宁德时代", "贵州茅台"], "boards": ["main", "gem"], "top_n": 30}}
```
- `stock_names` 支持数组或中英文逗号分隔字符串，多个名称取并集；完整名称优先精确匹配，否则按名称包含匹配。**非空时优先于 `industries`**，用于只计算用户点名的个股。
- `industries` 省略则全市场（自动剔除 ST/退）；存在 `stock_names` 时忽略行业条件。
- `boards` 限定市场范围，取值 `main`（沪深主板）/`star`（科创板）/`gem`（创业板），多选取并集；省略或包含全部三类市场即筛选全部支持市场。科创板按 688/689 前缀识别，创业板按 300/301 前缀识别，其余有效 `.SH`/`.SZ` 股票归沪深主板。北交所按 `.BJ` 独立识别，但当前不纳入量化因子预计算和候选池，也不得并入主板。该维度与 `industries`/`stock_names` 取交集，响应和筛选运行快照均记录本次生效的 `boards`。
- `boards` 采用严格白名单；空数组、`bj`、未知值或拼写错误必须停止并修正，不得静默扩大为全市场。
- **行业匹配防污染**：每个词条先查 `stock_basic.industry` 与申万 L1/L2/L3 正式行业分类；正式分类有命中时不混入概念成分，避免“医药”等宽泛概念误收非行业股。只有正式分类完全无命中时，才回退同花顺/东财概念，以支持“机器人/PCB铜箔”等主题。
- **多行业词条取交集（层层收窄）**：传多个词条时只保留同时属于全部词条的个股。例如 `["通信","PCB","铜箔"]` = 通信∩PCB∩铜箔。某词条无命中则交集为空。
- **一般输入宽容**：个股/行业词条大小写不敏感、自动去首尾空格，中英文逗号(`，`/`,`)均可切分；市场值仍按上述白名单严格校验。

### 选股数量
- **自动跑（定时任务 T7/W1 等由调度触发的选股）：`top_n=50`**。
- 手动/网页触发可自定义（网页默认 30）。

### Agent 复核与正式理由流程
1. 拿到 `candidates`（含各因子值与合成 `score`），记录原始量化 score、权重非零关键因子及 `trade_date`；若 score 为横截面 z-score，必须在**同一批候选**内转换为 0~100 percentile 后再参与综合评分，禁止把 z-score 直接与百分制混加。
2. 对候选逐一叠加涨价/景气预期判断（`price_hike_scan` + 外部行业价格/期货数据交叉验证），按四维打分；业绩披露期的数据也只作可核验事实，不能宣称必然利好。
3. 剔除逻辑不符、数据异常者；情绪主导（涨价+逻辑均<0.4）标高风险。
4. 逐股定位板块/行业/产业链，并用同交易日持久化的 `daily_sector_scores`（经 `screen_sector` 返回）核对行业强度分位、短中期动量、量能和阶段；行业分位已作为 `industry_strength` 参与个股综合分，但不能替代涨价/景气/事件证据。把同板块/主线/产业链的关联个股归组，结合消息与行业新闻说明炒作路径。
5. 判断与当前主线关系，统一使用「核心/分支/补涨/非主线」；不得把板块归属直接等同于主线地位。
6. 每只正式量化候选按固定理由链输出：**量化信号 → 板块趋势 → 当前主线关系 → 涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪**。任一环缺资料必须原位写「无可核验证据」，不得省略或臆造。
7. 产出报告：调度器自动量化选股写 `选股/yyyyMMdd-量化选股.md`；用户指定事件/行业/热门板块时写 `投研/yyyyMMdd-{主题}选股/研究报告.md`。标题后第一节固定为 `## 一眼结论（核心摘要）`，先给 output-format「题材/事件—个股首屏结论表」，再目录和正文；正文含因子表、四维打分表、按“题材/事件 → 个股”的量化分组解读、正式候选综合理由表和数据来源。
8. 调度器正式自动候选逐只 `log_selection(category=auto)`；用户触发正式选股候选逐只 `log_selection(category=manual)`。两类都必须原样传同日 `screening_run_id`，并提供上海时间 `selected_at`、精炼 `core_event`、`reason` 与 `tags`。评分、排名、因子与契约只接受服务端快照。`manual` 只做隔离回测，不改自动 predictions、daily 或短期事项；所有选股情况均禁止写入永久 `MEMORY.md`。

## 二、选板块 / 板块轮动（screen_sector）

### 因子（方向均为 +，权重和=1.0；权重以 `get_factor_config(model=sector)` 为准）

| 因子 | 说明 |
|---|---|
| `sec_mom_12_1` | 板块 12-1 中期动量 |
| `sec_mom_20d` | 板块 20 日动量（近端延续） |
| `sec_mom_5d` | 板块 5 日动量（情绪热度） |
| `sec_vol_confirm` | 板块量能确认（放量上行） |
| `sec_low_vol` | 板块低波动（稳健趋势优先） |

数据源：申万一级行业指数（`sw_daily` + `index_classify`），无权限则如实失败。D1 每日盘后把全部行业原始因子、综合分和横截面分位写入 `daily_sector_scores`；`screen_sector` 优先读同交易日持久化结果，缺失才实时计算。个股预计算通过申万成分映射把行业分位写入 `industry_strength`。

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

- 使用前完整读取本文件并确认固定 12 Skills（含 `stock-research`）已完整加载；不得只凭因子名、接口索引或角色摘要运行。
- **因子引擎路径**：`skills/quant-screening/scripts/factors.py`（唯一正确文档路径）。
- **直接依赖**：`data-service`（接口与 fallback）、`priority-framework`（候选复核）、`output-format`（分组报告）。
- **协同 Skills**：`industry-analysis`、`stock-screening`、`stock-research`（单股量化分位与关键因子）、`post-market`、`review-learning`。

## 量化报告 fallback

板块/指数上下文引用 `market_index` 时，允许数组或逗号字符串；失败、空或部分 code 缺失则逐 code 调 `market_daily(code,start,end)`，取最近记录并标 `degraded`/实际日期（数据接口间等价回退）。**数据类接口失败则失败、如实披露，禁止编造或臆造缺失因子**。候选分组所需新闻/公告不在数据服务，从各财经平台多源检索（≥2 来源交叉，标来源与时间）；全失败标“资讯面不可用 + 已尝试来源”，不得当作无风险。T7/W1/M1 中非关键接口失败不阻塞其余候选与报告。

## v1.6 因子契约与运行证据（强制）

- 因子契约必须同时保存：模型公式版本、全部具体成分（包括当前权重为0的候选因子）、结构哈希、完整权重快照/权重版本，以及上游行业评分、历史成分区间和股票池规则的依赖指纹。公式或成分变化提升公式/结构版本；仅权重变化不得冒充公式变化。
- 预计算只有在 `status=success`、覆盖率达标、公式版本/结构哈希/完整依赖指纹一致，且 `daily_factors`/`daily_sector_scores` 与质量记录的 `run_id` 相同时可用。`partial|failed|legacy(NULL)` 一律不可被筛选消费；部分重算不得覆盖同契约既有成功快照。
- 历史申万成分必须用 `in_date<=trade_date<out_date`；接口缺少生效区间字段时拒绝历史补算，不得把当前成分当历史成分。股票池按目标日 `list_date/delist_date` 过滤。
- `screen_quant`/`screen_trend` 返回的 `screening_run_id`、完整契约、候选排名、`score_raw` 与 `score_percentile` 是正式选股证据。`score_raw` 仅限当批比较；跨批次、回测分桶和正式持久化统一使用0~1分位。
- `auto/manual` 调用 `log_selection` 时必须原样携带 `screening_run_id`，不得自行填写分数或因子冒充筛选结果。

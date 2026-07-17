---
name: pre-market
description: 盘前汇总（08:30，团队模式）。整合消息/全球市场/期货/宏观数据+情绪择时，判断重仓/空仓环境，结合近7日自动选股给出今日关注板块与个股，并把高热度股加入临时观察列表；不会自动触发后续竞价或盘中盯盘。
user-invocable: true
disable-model-invocation: false
---

# 盘前汇总（08:30）

> `bidding-analysis` 是用户可调用能力：仅在用户于竞价结束后明确请求时由主 Agent 单次执行，不设 09:25 自动任务，也不自动衔接盘中盯盘。

> **输出目录取决于触发来源**（见 output-format「触发来源决定输出目录」）：定时任务(T1)→ `yyyy年MM月dd日/01-盘前汇总.md` 并写自动记忆；**用户手动触发 → `投研/yyyyMMdd-手动盘前汇总/`，且不覆盖当日定时日报、不改写 `daily/yyyyMMdd.md`、不以 category=auto 登记选股**。

## 前置步骤（共用）

1. **交易日守卫**：`GET /health`，`trade_open=false` 则直接返回。
2. **强制读取当日观察对象记忆** `agent记忆/daily/yyyyMMdd.md`（若未生成则先按 memory 规则生成：合并 `关注与持仓.md` + 近7日自动选股）。
3. 读取 `短期记忆/` 中与本次盘前任务相关且未过期的涨价/趋势线索；先删除已完成、证伪或过期项。
4. **数据全部经由数据服务，禁止编造。**

---

## 一、盘前汇总（08:30，**团队模式**）

启用团队：宏观时事分析师、技术面趋势分析师、基本面研报分析师、情绪面分析师并行产出意见，主 Agent 二次验证复核后汇总。执行流程（按重心排序）：

#### 1. 涨价与行业景气扫描（第一优先，基本面研报分析师）
- 调 `price_hike_scan` + 自主外部渠道（行业平台/期货）获取涨价信号，外部财经资讯 + `macro_ppi` 交叉印证
- 复查相关短期涨价线索：更新待处理动作、证据、失效时间；完成或证伪后立即删除条目

#### 2. 消息汇总 + 全球市场 + 期货 + 宏观（宏观时事分析师）
- 时政/产业事件：外部财经资讯多源检索（新闻/时政/公告，见 data-service「资讯类外部获取」；≥2 来源交叉）
- 全球市场：`overseas_hk`（港股，数据服务）+ 美股/大宗商品/A50（外部多源）
- 期货市场：国内商品期货主力（涨价链先行信号，外部/`price_hike_scan`）
- 宏观数据：`macro_ppi`（涨价锚）`macro_cpi` `macro_pmi` `macro_m`
- 北向资金：`money_hsgt`

#### 3. 技术面与情绪·择时（技术面/情绪面分析师）
- 大盘趋势与成交量、强势板块动量（`market_index` `sector_dc` `screen_sector`）
- 情绪温度值 0-100（`sentiment_temperature`）
- **择时**：调 `market_timing` 获取连续冰点/高热 streak、stance、buy_weight_hint

#### 4. 结合近7日自动选股（**重点前一日**）
- 调 `selection_backtest` 或读近7日 auto 选股记录，重点看前一日自动选股表现与仍有效标的
- 持仓/关注股逐一检查催化与风险（重点盯）

#### 5. 重仓 / 空仓环境初判（★择时核心）
结合 **情绪择时（market_timing）+ 消息面 + 行业催化（含机构放话/研报观点）** 做初步判断：
- **具备重仓环境**：情绪健康或**连续冰点后反转** + 明确正向催化（涨价/政策/机构一致看多）+ 主线清晰 → 当日仓位上限上调，出手买入权重上调
- **需要空仓/降仓**：**连续高热见顶** / 重大利空 / 无主线且情绪退潮 / 催化落空 → 降仓或空仓，不追高
- 明确写出「今日仓位倾向 + 建议出手权重 + 依据」（非确定性指令），不得在用户报告中显示内部参数名。

#### 6. 高热度临时观察列表（★）
- 取 `hot_dc` `hot_ths` `hot_kpl_list` 热度排行，将高热度个股写入当日观察对象记忆的**临时观察列表**；仅在用户后续明确请求竞价或盘中分析时作为参考，不自动触发任何监控。
- 临时观察 ≠ 关注/持仓，不持久、不计入回测调参，也不产生自动盯盘任务。

#### 7. 主 Agent 二次验证 + 今日关注
- 交叉核对各子 Agent 关键数据（尤其涨价），并按四维打分 × `buy_weight_hint` 排序。
- 凡作为正式关注的量化/趋势候选，逐只按 `skills/output-format/SKILL.md` 的「正式候选综合理由表」输出，不得只列四维分或量化分。固定理由链为：**量化信号 → 板块趋势 → 当前主线关系 → 涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪**。
- 每只必须写明量化综合分与关键因子、四维分、板块/产业链、板块短中期动量/量能/阶段、主线关系（核心/分支/补涨/非主线）、催化与炒作路径、入选理由链和证伪条件；任一环缺资料写「无可核验证据」，不得省略或臆造。
- **定时 T1** 的明确正式预判才写 `predictions.jsonl`（标 driver）。若本轮新生成正式自动候选，必须实际执行同日 `screen_quant` 或 `screen_trend`，确认候选位于响应中，再以原样 `screening_run_id` 调用 `log_selection(category=auto)` 并更新 daily；未取得有效筛选运行时只能复核既有候选，不得新登记 auto。用户手动盘前默认 ephemeral，不写自动记忆。既有 watch/holding 只维护状态，新增 watch 必须有用户明确指令。

#### 8. 生成详尽报告与独立推送
- Markdown 正文严格执行 T1 完整模板，标题后第一节为 `## 🎯 一眼结论（核心摘要）`，首屏按顺序给出**📊 今日仓位倾向、🔥 重点题材/具体事件 Top N、🎯 “题材/事件 → 今日关注个股”、⚠️ 最大风险/证伪、题材/事件—个股首屏结论表**，然后才是目录导读和详细证据。首屏四类关键信息与主要章节标题按 output-format 的 emoji 约定加统一图标高亮。
- 今日题材由消息面、热榜、涨停连板、量能和资金综合识别，不限传统行业板块；具体事件需给驱动时间、热度、首次发酵/加速/分歧/退潮/证伪阶段及证据。
- 正文至少覆盖数据状态、隔夜/宏观/消息、涨价与景气、指数与板块趋势、情绪温度/极端指数/择时、近 7 日选股复核、今日关注完整理由、持仓、风险和来源。
- 报告在可核验范围内尽可能详尽；数据不可用时保留章节，写明失败接口、fallback、实际数据日期与缺失项。推送与报告分离，建议不超过 500 字并包含报告路径和降级提示。

### 常用调用
`price_hike_scan` `macro_ppi/cpi/pmi/m` `money_hsgt` `market_index` `sector_dc` `screen_sector` `sentiment_temperature` `market_timing` `hot_dc` `hot_ths` `hot_kpl_list` `overseas_hk` `selection_backtest` + 外部财经资讯/外盘（多源，见 data-service「资讯类外部获取」）

## Skill 加载约束 / 依赖 Skills

- 盘前任务启动前完整读取本文件，且确认当前角色已完整加载固定 12 Skills（含 `stock-research`）；不得仅凭 schedule 或角色摘要执行。
- **直接依赖**：`data-service`、`priority-framework`、`output-format`。
- **协同 Skills**：`industry-analysis`（涨价/消息）、`stock-screening` 与 `quant-screening`（候选）、`review-learning`（近7日回测）、`bidding-analysis`（临时观察列表交接）、`post-market`（前日结论）、`stock-research`（仅共享研究证据，不作为 T1 必执行绑定）。
- T1 必须点名并执行：`skills/pre-market/SKILL.md` + `skills/data-service/SKILL.md` + `skills/priority-framework/SKILL.md` + `skills/output-format/SKILL.md`，并按团队角色调用上述协同 Skill。

## 盘前 fallback

T1 关键数据接口 4xx/5xx/空数据时先记失败，5 分钟、15 分钟各重试一次；401/配置错误不盲目重试。`market_index` 失败/空/部分缺失时按 code 逐个调用 `market_daily(code,start,end)` 最近记录并标 `degraded`/实际日期（数据接口间等价回退，非编造）。**数据类接口失败则失败、如实披露，禁止编造兜底**。资讯类（新闻/时政/公告/外盘）不在数据服务，直接从各财经平台多源检索（≥2 来源交叉，标来源与时间）；全部资讯来源失败标“资讯面不可用 + 已尝试来源”，不得解释为无风险。关键数据源最终失败后继续可完成部分，非关键源失败不阻塞整份盘前报告。
## v2.2.0 当前调度与日终边界

- 现行 Agent 定时任务仅为 T1/T2/T3/W1/M1/P1；本 Skill 的定时入口仅为 T1，不承接旧 T6/T7/D1 或任何自动盯盘任务。
- 服务端在交易日 16:00 自动完成日终收口；Agent 只读 `health.daily_finalize` / `precompute_status`，不得自动调用 `precompute_daily_factors`。
- 只有用户当前明确要求管理员诊断或补数时，才允许单次手动调用 `precompute_daily_factors`；不得用于定时、自动补跑或失败回退。
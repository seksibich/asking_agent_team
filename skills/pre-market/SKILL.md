---
name: pre-market
description: 盘前汇总（08:30，团队模式）。整合消息/全球市场/期货/宏观数据+情绪择时，判断重仓/空仓环境，结合近7日自动选股给出今日关注板块与个股，并把高热度股加入临时观察列表。竞价分析见 bidding-analysis(09:25)。
user-invocable: true
disable-model-invocation: false
---

# 盘前汇总（08:30）

> 竞价分析已独立为 `bidding-analysis` 技能，在 **09:25 竞价结束**后由主 Agent 执行。

> **输出目录取决于触发来源**（见 output-format「触发来源决定输出目录」）：定时任务(T1)→ `yyyy年MM月dd日/01-盘前汇总.md` 并写自动记忆；**用户手动触发 → `投研/yyyyMMdd-手动盘前汇总/`，且不覆盖当日定时日报、不改写 `daily/yyyyMMdd.md`、不以 category=auto 登记选股**。

## 前置步骤（共用）

1. **交易日守卫**：`GET /health`，`trade_open=false` 则直接返回。
2. **强制读取当日观察对象记忆** `agent记忆/daily/yyyyMMdd.md`（若未生成则先按 memory 规则生成：合并 `关注与持仓.md` + 近7日自动选股）。
3. 读取 `观察池.md`（涨价/趋势线索）。
4. **数据全部经由数据服务，禁止编造。**

---

## 一、盘前汇总（08:30，**团队模式**）

启用团队：宏观时事分析师、技术面趋势分析师、基本面研报分析师、情绪面分析师并行产出意见，主 Agent 二次验证复核后汇总。执行流程（按重心排序）：

#### 1. 涨价与行业景气扫描（第一优先，基本面研报分析师）
- 调 `price_hike_scan` + 自主外部渠道（行业平台/期货）获取涨价信号，`news_filter` + `macro_ppi` 交叉印证
- 复查 `观察池.md` 涨价线索，更新状态

#### 2. 消息汇总 + 全球市场 + 期货 + 宏观（宏观时事分析师）
- 时政/产业事件：`news_flash` `news_filter` `news_cctv`
- 全球市场：`overseas_us` `overseas_hk` + 大宗商品/A50（外部）
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
- 明确写出「今日仓位倾向 + buy_weight_hint + 依据」（非确定性指令）

#### 6. 高热度临时观察列表（★）
- 取 `hot_dc` `hot_ths` `hot_kpl_concept` 热度排行，将高热度个股写入当日观察对象记忆的**临时观察列表**（当日有效，供 09:25 竞价分析与盘中盯盘参考）
- 临时观察 ≠ 关注/持仓，不持久、不计入回测调参

#### 7. 主 Agent 二次验证 + 今日关注
- 交叉核对各子 Agent 关键数据（尤其涨价），并按四维打分 × `buy_weight_hint` 排序。
- 凡作为正式关注的量化/趋势候选，逐只按 `skills/output-format/SKILL.md` 的「正式候选综合理由表」输出，不得只列四维分或量化分。固定理由链为：**量化信号 → 板块趋势 → 当前主线关系 → 涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪**。
- 每只必须写明量化综合分与关键因子、四维分、板块/产业链、板块短中期动量/量能/阶段、主线关系（核心/分支/补涨/非主线）、催化与炒作路径、入选理由链和证伪条件；任一环缺资料写「无可核验证据」，不得省略或臆造。
- 明确预判写 `predictions.jsonl`（标 driver）；今日 auto/watch/holding 标的按既有规则 `log_selection` 登记，并更新当日观察对象记忆。

#### 8. 生成详尽报告与独立推送
- Markdown 正文严格执行 `skills/output-format/SKILL.md` 的 T1 完整模板：**核心摘要后立即输出目录导读**，并至少覆盖数据状态、隔夜/宏观/消息、涨价与景气、指数与板块趋势、情绪温度/极端指数/择时、近 7 日选股复核、今日主线、今日关注标的完整理由、持仓关注、风险、来源。
- 报告在可核验范围内尽可能详尽，禁止为了推送字数删减正文。数据不可用时保留章节，写明失败接口、fallback、实际数据日期与缺失项，禁止静默删除。
- 推送与报告分离，引用 output-format「独立推送摘要模板」，建议不超过 500 字，包含任务/日期、仓位倾向、1~3 条核心主线/事件、重点候选或持仓风险、报告路径、数据降级提示；不得复制全文，也不得只发“报告已生成”。

### 常用调用
`price_hike_scan` `news_flash` `news_filter` `news_cctv` `overseas_us` `macro_ppi/cpi/pmi/m` `money_hsgt` `market_index` `sector_dc` `screen_sector` `sentiment_temperature` `market_timing` `hot_dc` `hot_ths` `hot_kpl_concept` `selection_backtest`

## Skill 加载约束 / 依赖 Skills

- 盘前任务启动前完整读取本文件，且确认当前角色已完整加载固定 11 Skills；不得仅凭 schedule 或角色摘要执行。
- **直接依赖**：`data-service`、`priority-framework`、`output-format`。
- **协同 Skills**：`industry-analysis`（涨价/消息）、`stock-screening` 与 `quant-screening`（候选）、`review-learning`（近7日回测）、`bidding-analysis`（临时观察列表交接）、`post-market`（前日结论）。
- T1 必须点名并执行：`skills/pre-market/SKILL.md` + `skills/data-service/SKILL.md` + `skills/priority-framework/SKILL.md` + `skills/output-format/SKILL.md`，并按团队角色调用上述协同 Skill。

## 盘前 fallback

T1 关键接口 4xx/5xx/空数据时先记失败，5 分钟、15 分钟各重试一次；401/配置错误不盲目重试。`market_index` 失败/空/部分缺失时按 code 逐个调用 `market_daily(code,start,end)` 最近记录并标 `degraded`/实际日期。`news_flash` 402 时执行 `news_filter(keyword)+news_cctv+外部搜索`；同源失败继续 cctv+至少两个可信外部来源；全部失败标“消息面不可用”，不得解释为无风险。关键源最终失败按 fallback 降级并继续可完成部分，非关键源失败不阻塞整份盘前报告。
---
name: post-market
description: 盘后。当日总结（17:30，主Agent轻量）汇总当日盘面；综合复盘（22:00，完整团队）跑复盘+选股+回测，跟踪趋势主线与涨价链，产出次日候选并调参。
user-invocable: true
disable-model-invocation: false
---

# 盘后：当日总结 + 综合复盘

> **输出目录取决于触发来源**（见 output-format）：定时任务(T6/T7)→ `yyyy年MM月dd日/04-当日总结.md`、`05-综合复盘.md`，并写自动记忆（`daily` 快照、`log_selection(category=auto)`、predictions/学习日志）；**用户手动触发复盘 → `投研/yyyyMMdd-手动复盘/`，不覆盖当日定时日报、不改写自动记忆、不以 category=auto 登记选股**（避免污染回测调参）。

## 前置步骤
1. 交易日守卫（`GET /health`）。
2. **强制读取当日观察对象记忆**（持仓/关注/相关板块/自动选股）、`观察池`、当日 `predictions.jsonl`。

---

## 一、当日总结（17:30，**主 Agent 轻量**）

不启用团队，主 Agent 快速汇总并形成详尽 Markdown：
1. 数据状态：交易日、接口状态、实际数据日期、重试与 fallback。
2. 指数与成交：大盘收盘、成交额、量价变化（`market_index`）。
3. 板块趋势与主线阶段：板块涨跌、短中期动量、量能与启动/主升/高位/退潮阶段（`sector_dc`）。
4. 情绪/连板/极端指数：`sentiment_temperature`、`sentiment_extreme_index`、`market_limit`、`market_lianban`、`market_timing`。
5. 资金：北向及可核验资金变化（`money_hsgt` 等）；缺失时明确降级。
6. 涨价链：`price_hike_scan` 与观察池线索的当日进展。
7. 持仓/关注：逐一核对当日表现、催化、板块联动与风险。
8. 明日环境初判：结合择时、消息面和行业催化给出重仓环境/中性/降仓或空仓倾向，说明依据，非确定性指令。
9. 风险与来源：披露缺口、证伪条件、来源与时间。

生成 `04-当日总结.md` 时必须执行 `skills/output-format/SKILL.md` 的 T6 完整模板：**核心摘要 → 目录导读 → 详细正文**；正文至少保留数据状态、指数成交、板块趋势与主线阶段、情绪/连板/极端指数、资金、涨价链、持仓关注、明日环境初判、风险、来源。数据缺失不得删节。推送另按独立模板生成，建议不超过 500 字，不复制正文。

### 常用调用
`market_index` `sector_dc` `market_limit` `market_lianban` `sentiment_temperature` `sentiment_extreme_index` `market_timing` `money_hsgt` `price_hike_scan`

---

## 二、综合复盘 + 选股 + 回测（22:00，**完整团队**）

启用全部子 Agent 并行，主 Agent 二次验证复核后汇总。

```
22:00 主 Agent（读当日观察对象记忆）
  ├── 技术面趋势分析师：全天行情、趋势主线定位、板块动量轮动
  ├── 情绪面分析师：情绪温度、极端指数、连板生态与择时
  ├── 基本面研报分析师：涨价/景气；业绩窗口判定与业绩增长参考池
  ├── 宏观时事分析师：晚间公告/消息、外盘展望、北向
  ├── 资金复盘：龙虎榜、游资/机构动向
  └── 回测分析师：predictions_backtest + selection_backtest，仅处理正式 auto 样本
  ↓ 主 Agent 汇总 + 二次验证（单子Agent超时5分钟标[超时]跳过）
  → 详尽综合复盘 + 正式次日候选 + 隔离的业绩增长参考池 + 因子调参
```

### 择时与重仓/空仓环境（明日）
- 调 `market_timing`（连续冰点/高热 streak、`buy_weight_hint`）+ 情绪 + 消息面 + 行业催化。
- 明确明日仓位倾向：具备重仓环境 / 中性 / 需要降仓或空仓，并说明依据，作为正式候选出手权重和仓位上限参考。

### 正式量化/趋势选股（团队协同）
- `screen_sector` 定强势主线 → `screen_trend`/`screen_quant`（自动跑 `top_n=50`）在主线内/全市场选股 → 团队叠加涨价/逻辑/情绪复核 → 主 Agent 二次验证。
- 出手评分 = 四维综合分 × `buy_weight_hint`；量化/技术信号不能代替基本面证据。
- 候选须按主线/产业链分组，并逐只使用 output-format「正式候选综合理由表」。每只同时提供量化综合分与关键因子、四维分、板块/产业链、板块短中期动量/量能/阶段、主线关系（核心/分支/补涨/非主线）、催化与炒作路径、完整理由链、风险与证伪。
- 固定理由链：**量化信号 → 板块趋势 → 当前主线关系 → 涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪**。任一环缺资料写「无可核验证据」。
- 仅符合正式自动选股定义的候选才用 `log_selection(category=auto)` 登记并按规则写自动记忆。

### 业绩增长参考池（与正式选股完全隔离）
1. **窗口判定**：当前处于法定/惯例业绩预告或定期报告披露季，或接口返回最近 3 个交易日的新预告/快报/公告，即执行。以实际公告日期与接口返回为准，禁止仅凭日历臆断。
2. **调用**：由基本面研报分析师调用 `fundamental_forecast`、`fundamental_express`、`news_anns`；必要时对重点个股用 `fundamental_income`、`fundamental_fina_indicator` 复核。
3. **全量整理**：罗列接口中可核验的业绩增长、预增、略增、扭亏、续盈等正向公告，按 `code+report_period+announcement_date` 去重。输出 output-format「业绩增长参考池表」，保留公告类型、报告期、公告日期、净利润或增速区间（仅接口真实字段）、所属板块/主线、来源、风险；字段缺失写「接口未返回」。
4. **无数据**：保留章节并明确写「当晚无可核验的增长/预增公告」，同时说明接口状态和实际查询日期。
5. **重点说明**：可选 3~5 个代表标的，覆盖公告事实、增速/利润区间、所属板块、板块趋势、是否当前主线、业绩与行业逻辑是否共振，以及兑现/基数/一次性损益风险；但表格必须全量保留其余可核验公告。
6. **隔离边界**：参考池不是正式选股，不调用 `log_selection`，不写 `predictions.jsonl`、观察池，不纳入 auto/watch/holding，不参与回测调参。若同一股票独立通过正式量化/趋势流程，只能由正式流程按正常规则持久化，不得因进入参考池持久化。
7. **判断边界**：业绩增长不代表必然利好，不能单独替代四维、板块趋势与择时判断；PE/PB 仍仅作风险背景。

### 因子调参闭环（回测分析师 → 主 Agent）
- 依据 `selection_backtest` 的正式 auto 样本给出权重建议；**业绩增长参考池记录不得进入样本、统计或调参证据**。
- 主 Agent 复核后：`get_factor_config` 取最新因子列表 → `set_factor_weights` 提交全部因子权重（模型 stock/sector/trend），失败按 `expected_factors` 修正后重试。
- 调参依据与结果写入学习日志。

### T7 详尽报告结构

严格使用 `skills/output-format/SKILL.md` 的 T7 完整模板，顺序为**核心摘要 → 目录导读 → 详细正文**。正文至少覆盖：数据状态、全天行情、板块趋势/主线、情绪与连板、资金龙虎榜、涨价景气、晚间公告、正式量化/趋势候选、业绩增长参考池、明日策略、回测调参、风险、来源。报告不得因推送限制删减；不可用数据保留章节并写明缺失、fallback、重试轨迹和实际日期。

### 回测逻辑
1. 读当日 `predictions.jsonl` 中 direction≠neutral 的正式记录。
2. 调 `market_daily` 取标的当日实际涨跌，判定准确/偏差。
3. 分 `driver` 统计准确率，写入 `学习日志-yyyy年MM月.md`。
4. 观察池涨价/趋势线索按当日进展更新；业绩增长参考池不更新观察池。

### 完成后推送
独立摘要建议不超过 500 字，包含任务/日期、明日仓位倾向、1~3 条核心主线/事件、正式候选或持仓风险、完整报告路径、数据降级提示；不得复制整篇报告，也不得只发“报告已生成”。

## Skill 加载约束 / 依赖 Skills

- T6/T7 或手动盘后任务启动前完整读取本文件并确认固定 11 Skills 已完整加载，禁止只读 schedule/角色摘要。
- **直接依赖**：`data-service`、`priority-framework`、`output-format`。
- **协同 Skills**：`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`；并读取 `pre-market`/`bidding-analysis`/`intraday-watch` 的当日预判与事实作为复盘输入。
- T6 必须点名 `skills/post-market/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md`；T7 还必须点名 `skills/industry-analysis/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/review-learning/SKILL.md`。

## 盘后 fallback

T6/T7 关键接口 4xx/5xx/空数据先记录，延后 5 分钟、15 分钟各重试一次；401/配置错误不盲目重试。`market_index` 失败/空/部分缺失时按 code 逐个 `market_daily(code,start,end)` 取最近记录，标 `degraded` 和实际日期。新闻按 `news_flash` 402 → `news_filter(keyword)+news_cctv+外部搜索`；同源失败 → cctv+至少两个可信外部来源；全失败标“消息面不可用”。关键源最终失败后降级并继续可完成部分，非关键接口不阻塞报告，禁止编造。
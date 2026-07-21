# 定时任务清单

> Agent 只注册本文件列出的非盯盘任务（v2.7.0：交易日仅 T1 08:30、T3 22:00）。初始化时删除全部旧 T2/T3/T4/T5、旧 T6/T7、旧 D1、**已取消的 T2（17:30 当日总结）**，以及任何 Agent 自动竞价、Agent 盘中盯盘、午间总结或 Agent 预计算任务。
>
> **禁止 Agent 自动盯盘与 Agent 预计算**：竞价与解释型盘中 Skill 只接受用户当前明确请求并单轮执行；数据服务获准在连续竞价时独立运行无 LLM 的确定性 `quant_watch` 扫描，但 Agent 不负责启动、循环、补跑或主动消费通知。全市场行业/个股日终预计算仍由数据服务在交易日 16:00 自动执行。
>
> 时间统一为 Asia/Shanghai。每个 Agent 定时任务先调 `GET /health`，协调版本并读取所需记忆；服务端日终任务状态只读 `health.daily_finalize` / `precompute_status`。

## 任务描述结构化模板（强制）

加载完工作目录文档后，Agent 依据**工作目录内化路径**为每个定时任务生成描述，每条任务描述固定含四段：

1. **【工作目标】**：这个任务要让用户明确知道什么、产出什么结论（用通俗中文一句话说清）。
2. **【结果存放的目录结构】**：最终报告与允许保存的业务快照落到哪些目录；中间产物只进 `tmp/` 且任务结束清理。
3. **【Agent 编排】**：由主 Agent 单跑还是启用团队、各子 Agent 分工与主 Agent 二次复核。
4. **【使用的 Skill 和接口文档/索引（工作目录路径）】**：点名本任务用到的 `工作文档/skills/<name>/SKILL.md`，以及接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

> 下表是速查；每个任务的完整结构化描述见「每日任务链」「周期任务」后的结构化条目。任务定义有更新时按结构化模板重设日程。

## 每日任务链（交易日）

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill / 子 Agent | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| T1 | 08:30 | **盘前汇总**：首屏给今日仓位、题材/事件 Top N、关注个股和最大风险 | 团队 | `pre-market`、`data-service`、`priority-framework`、`output-format`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning` | 宏观、涨价、行业、情绪、热榜、回测；正式 auto 候选必须来自真实 `screen_quant`/`screen_trend` 运行并携带 `screening_run_id` | `01-盘前汇总.md` + 独立重点推送 |
| T3 | 22:00 | **综合复盘 + 正式选股 + 预判登记与成熟回测**：完整复盘当天市场、板块强度与轮动，正式候选、预判、历史回测和业绩参考池严格隔离 | 团队 | `post-market`、`review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format` | 行情、板块、涨跌停、连板、热榜、情绪、资金、涨价、龙虎榜、行业、量化/趋势筛选、财务参考及外部多源资讯；先核验服务端 16:00 日终状态，先登记新预判，再仅回测已成熟历史预判 | `02-综合复盘.md` + 独立重点推送 |

> **v2.7.0：已取消 T2（17:30 当日总结）**。交易日只保留 T1（08:30）与 T3（22:00）。当天市场复盘统一并入 T3。

### T1 结构化描述（08:30 盘前汇总，团队）
- **【工作目标】**：开盘前告诉用户今天市场大环境如何、隔夜有哪些重点事件、今日该关注哪些板块与个股、仓位怎么摆、最大风险是什么。
- **【结果存放的目录结构】**：`盯盘/yyyy年MM月dd日/01-盘前汇总.md` + 独立重点推送；正式 auto 候选写服务端 `log_selection(category=auto)` 与 `daily` 快照。中间意见/原始返回只进 `盯盘/tmp/`，任务结束清理。
- **【Agent 编排】**：团队。主 Agent 分发宏观时事 / 基本面研报 / 技术趋势 / 情绪四子 Agent 并行取数，回收结构化意见后二次验证复核、按四维加权输出。
- **【使用的 Skill 和接口文档/索引（工作目录路径）】**：`工作文档/skills/{pre-market,data-service,priority-framework,output-format,industry-analysis,stock-screening,quant-screening,review-learning}/SKILL.md`；接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

### T3 结构化描述（22:00 综合复盘 + 正式选股 + 回测，团队）
- **【工作目标】**：完整复盘今天市场发生了什么、板块强度与轮动，给出明日该关注的核心/龙头/受益个股，并产出经热点→申万分级行业量化的正式候选（含逐只选股理由），登记预判与成熟回测。
- **【结果存放的目录结构】**：`盯盘/yyyy年MM月dd日/02-综合复盘.md` + 独立重点推送；正式候选 `log_selection(category=auto)` 上传服务端、`daily`、`predictions.jsonl`、`学习日志-yyyy年MM月.md`。子 Agent 中间意见只进 `盯盘/tmp/` 并于结束清理。
- **【Agent 编排】**：完整团队。主 Agent 分发技术趋势 / 情绪 / 基本面研报 / 宏观时事 / 资金 / 回测子 Agent，二次验证复核后汇总；正式选股由主 Agent 统一走「热点→申万分级行业→行业内量化→逐只理由→上传」。
- **【使用的 Skill 和接口文档/索引（工作目录路径）】**：`工作文档/skills/{post-market,review-learning,quant-screening,stock-screening,industry-analysis,data-service,priority-framework,output-format}/SKILL.md`；接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

## 周期任务

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| W1 | 周日 20:00 | 周回测、趋势周报与下周候选池；仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format` | 回测、行业/量化/趋势筛选、合格后的权重更新 | `周报/` |
| M1 | 每月最后交易日 21:00 | 月回测、月报与 `USER.md` 稳定偏好复核；仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`data-service`、`priority-framework`、`output-format` | 回测、因子配置读取与合格后的权重更新 | `月报/` |
| P1 | 周六 12:00 | 涨价链专项扫描 | 主 | `industry-analysis`、`data-service`、`priority-framework`、`output-format` | `price_hike_scan`、`macro_ppi` 及外部行业价格/资讯多源核验 | 更新 `短期记忆/` 涨价线索及失效时间 |

### W1 结构化描述（周日 20:00 周回测/周报，团队）
- **【工作目标】**：复盘本周主线与涨价链、给出下周候选池与仓位倾向，门禁通过时调参。
- **【结果存放的目录结构】**：`盯盘/周报/`；正式候选 `log_selection(category=auto)`、回测快照与 `学习日志`。中间产物只进 `盯盘/tmp/` 并于结束清理。
- **【Agent 编排】**：团队，主 Agent 复核后汇总。
- **【使用的 Skill 和接口文档/索引（工作目录路径）】**：`工作文档/skills/{review-learning,quant-screening,stock-screening,industry-analysis,data-service,priority-framework,output-format}/SKILL.md`；接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

### M1 结构化描述（月末 21:00 月回测/月报，团队）
- **【工作目标】**：月度复盘与因子表现、复核 `USER.md` 稳定偏好，门禁通过时调参。
- **【结果存放的目录结构】**：`盯盘/月报/`；回测快照与 `学习日志`。中间产物只进 `盯盘/tmp/` 并于结束清理。
- **【Agent 编排】**：团队，主 Agent 复核后汇总。
- **【使用的 Skill 和接口文档/索引（工作目录路径）】**：`工作文档/skills/{review-learning,quant-screening,data-service,priority-framework,output-format}/SKILL.md`；接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

### P1 结构化描述（周六 12:00 涨价链专项扫描，主 Agent）
- **【工作目标】**：扫描全行业涨价链线索，更新有效涨价主线与失效时间。
- **【结果存放的目录结构】**：更新 `盯盘/agent记忆/短期记忆/` 的涨价线索（带时效与删除条件）；不产出正式选股。中间产物只进 `盯盘/tmp/`。
- **【Agent 编排】**：主 Agent 单跑。
- **【使用的 Skill 和接口文档/索引（工作目录路径）】**：`工作文档/skills/{industry-analysis,data-service,priority-framework,output-format}/SKILL.md`；接口规范 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`。

## 执行规则

- T1 先回测近 7 日自动选股，再执行行业、趋势和量化筛选；T3 显式执行 `post-market`，并执行回测和正式选股（当天市场复盘也在 T3 完成）。
- T3 只读取服务端日终状态。若 `health.daily_finalize.status != success` 或当日因子未达门禁，只披露缺口并按报告接口规则重试，禁止调用 `precompute_daily_factors`。
- W1/M1/P1 必须逐个点名绑定 Skill；正式调度候选登记 `auto`，用户主动正式候选登记 `manual`，普通研究不得混入。
- 用户明确要求竞价或盯盘时才可手动执行对应 Skill，每次一轮；禁止平台调度器、Agent 循环、Hook、cron 自动调用。
- T3 先保存真实筛选运行，再登记候选；先登记下一交易日新预判，再回测已成熟历史预判。业绩增长参考池不得进入选股、预判、短期事项或调参。
- T3/W1/M1 仅在 `optimization_gate.eligible=true` 时调参，并绑定快照、父权重版本和完整契约；否则只记录建议。
- T1/T3 遇非交易日直接返回；关键接口首次失败后按 5 分钟、15 分钟各重试一次，401、明确参数或配置错误不盲目重试。
- 数据失败必须披露接口、状态、时间、实际日期和缺失项；资讯允许至少两个独立来源交叉获取，全部失败不得解释为无风险。

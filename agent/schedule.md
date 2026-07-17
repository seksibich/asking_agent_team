# 定时任务清单

> Agent 只注册本文件列出的非盯盘任务。初始化时删除全部旧 T2/T3/T4/T5、旧 T6/T7、旧 D1，以及任何自动竞价、盘中盯盘、午间总结或预计算任务。
>
> **禁止自动盯盘与 Agent 预计算**：竞价、盘中能力只接受用户当前明确请求并单轮执行；全市场行业/个股预计算由数据服务在交易日 16:00 自动执行，Agent 不注册、不触发、不补跑。
>
> 时间统一为 Asia/Shanghai。每个 Agent 定时任务先调 `GET /health`，协调版本并读取所需记忆；服务端日终任务状态只读 `health.daily_finalize` / `precompute_status`。

## 每日任务链（交易日）

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill / 子 Agent | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| T1 | 08:30 | **盘前汇总**：首屏给今日仓位、题材/事件 Top N、关注个股和最大风险 | 团队 | `pre-market`、`data-service`、`priority-framework`、`output-format`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning` | 宏观、涨价、行业、情绪、热榜、回测；正式 auto 候选必须来自真实 `screen_quant`/`screen_trend` 运行并携带 `screening_run_id` | `01-盘前汇总.md` + 独立重点推送 |
| T2 | 17:30 | **当日总结**：次日倾向、最强题材/事件、代表个股、风险与证伪 | 主 | `post-market`、`data-service`、`priority-framework`、`output-format` | 行情、板块、涨跌停、连板、热榜、情绪、资金、涨价及外部多源资讯；先核验服务端 16:00 日终状态 | `04-当日总结.md` + 独立重点推送 |
| T3 | 22:00 | **综合复盘 + 正式选股 + 预判登记与成熟回测**：正式候选、预判、历史回测和业绩参考池严格隔离 | 团队 | `post-market`、`review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format` | 热榜、涨跌停、龙虎榜、行业、量化/趋势筛选、财务参考；先登记新预判，再仅回测已成熟历史预判 | `05-综合复盘.md` + 独立重点推送 |

## 周期任务

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| W1 | 周日 20:00 | 周回测、趋势周报与下周候选池；仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format` | 回测、行业/量化/趋势筛选、合格后的权重更新 | `周报/` |
| M1 | 每月最后交易日 21:00 | 月回测、月报与 `USER.md` 稳定偏好复核；仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`data-service`、`priority-framework`、`output-format` | 回测、因子配置读取与合格后的权重更新 | `月报/` |
| P1 | 周六 12:00 | 涨价链专项扫描 | 主 | `industry-analysis`、`data-service`、`priority-framework`、`output-format` | `price_hike_scan`、`macro_ppi` 及外部行业价格/资讯多源核验 | 更新 `短期记忆/` 涨价线索及失效时间 |

## 执行规则

- T1 先回测近 7 日自动选股，再执行行业、趋势和量化筛选；T2/T3 显式执行 `post-market`，T3 另执行回测和正式选股。
- T2/T3 只读取服务端日终状态。若 `health.daily_finalize.status != success` 或当日因子未达门禁，只披露缺口并按报告接口规则重试，禁止调用 `precompute_daily_factors`。
- W1/M1/P1 必须逐个点名绑定 Skill；正式调度候选登记 `auto`，用户主动正式候选登记 `manual`，普通研究不得混入。
- 用户明确要求竞价或盯盘时才可手动执行对应 Skill，每次一轮；禁止平台调度器、Agent 循环、Hook、cron 自动调用。
- T3 先保存真实筛选运行，再登记候选；先登记下一交易日新预判，再回测已成熟历史预判。业绩增长参考池不得进入选股、预判、短期事项或调参。
- T3/W1/M1 仅在 `optimization_gate.eligible=true` 时调参，并绑定快照、父权重版本和完整契约；否则只记录建议。
- T1/T2/T3 遇非交易日直接返回；关键接口首次失败后按 5 分钟、15 分钟各重试一次，401、明确参数或配置错误不盲目重试。
- 数据失败必须披露接口、状态、时间、实际日期和缺失项；资讯允许至少两个独立来源交叉获取，全部失败不得解释为无风险。

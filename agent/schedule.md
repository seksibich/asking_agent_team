# 定时任务清单

> 智能体仅注册本文件列出的非盯盘任务；注册前清理同名旧任务，并必须删除历史遗留的 T2/T3/T4/T5 竞价、盘中盯盘和午间总结任务。
>
> **禁止自动盯盘**：不得通过平台调度器、Agent 循环、Hook、cron 或其他触发器自动调用 `bidding-analysis`、`intraday-watch`、`bidding_analysis`、`watch_intraday` 或盘中资讯总结。竞价和盘中能力仅在用户当前明确请求时由主 Agent 单轮执行，响应结束即停止。
>
> 时间为中国标准时间（Asia/Shanghai）。每条保留任务启动时先调 `GET /health`，按任务需要读取当日观察对象记忆，并完整加载固定 12 个 Skills；完整加载不代表允许调用竞价或盯盘能力。

## 每日任务链（交易日）

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill / 子 Agent | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| T1 | 08:30 | **盘前汇总**：按“一眼结论→目录导读→详细正文”，首屏给今日仓位、动态题材/具体事件 Top N、关注个股和最大风险 | 团队 | `pre-market`、`data-service`、`priority-framework`、`output-format`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`；角色按 TEAM 主绑定 | 外部多源资讯；`macro_ppi/cpi/pmi`、`price_hike_scan`、`screen_sector`、`sentiment_temperature`、`sentiment_extreme_index`、`market_timing`、`hot_dc/ths`、`hot_kpl_list`、`selection_backtest` | `01-盘前汇总.md` + 独立重点推送 |
| T6 | 17:30 | **当日总结**：首屏给次日倾向、最强题材/事件、代表个股、最大风险和证伪条件 | 主 | `post-market`、`data-service`、`priority-framework`、`output-format` | `market_index`、`sector_dc`、`market_limit`、`market_lianban`、热榜、情绪、择时、北向资金、涨价扫描及外部多源资讯 | `04-当日总结.md` + 独立重点推送 |
| T7 | 22:00 | **综合复盘 + 正式选股 + 回测 + 业绩增长参考池**：正式候选和参考池严格隔离 | 团队 | `post-market`、`review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format`；角色按 TEAM 主绑定 | 热榜、涨跌停、连板、龙虎榜、`screen_sector`、`screen_quant(top_n=50)`、`screen_trend`、财务参考、`predictions_backtest`、`selection_backtest`；`log_selection` 仅正式候选调用 | `05-综合复盘.md` + 独立重点推送 |

## 周期任务

| 序号 | 时间 | 任务 | 模式 | 绑定 Skill | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| W1 | 周日 20:00 | 周回测 + 趋势周报 + 下周候选池；仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`stock-screening`、`industry-analysis`、`data-service`、`priority-framework`、`output-format` | `selection_backtest`、`predictions_backtest`、`screen_sector/quant/trend`、因子配置读取与合格后的权重更新 | `周报/` |
| M1 | 每月最后交易日 21:00 | 月回测 + 月报 + `USER.md` 稳定偏好复核；仅用户明确表达或反复确认时更新，且仅门禁通过时调参 | 团队 | `review-learning`、`quant-screening`、`data-service`、`priority-framework`、`output-format` | `selection_backtest`、因子配置读取与合格后的权重更新 | `月报/` |
| D1 | 交易日 17:45 | 全市场个股因子与行业评分预计算 | 外部调度器/主 Agent | `quant-screening`、`data-service` | `precompute_daily_factors`；失败后仅按 `retryable_dates` 重跑 | `daily_factors`、`daily_sector_scores`、运行质量记录 |
| P1 | 周六 12:00 | 涨价链专项扫描 | 主 | `industry-analysis`、`data-service`、`priority-framework`、`output-format` | `price_hike_scan`、`macro_ppi` 及外部行业价格/资讯多源核验 | 更新 `短期记忆/` 中的涨价线索；逐条设置复查动作与失效时间 |

## 保留任务执行规则

- T1 先由 `review-learning` 执行选股回测并读取近 7 日自动选股，重点检查前一交易日；再执行行业、趋势和量化筛选，最终按四维重心排序输出。
- T6/T7 显式执行 `post-market`；T7 另执行回测、趋势/量化筛选和行业分析。业绩增长参考池不得调用 `log_selection`，不得写 predictions 或创建短期事项，也不进入回测调参。
- W1/M1/D1/P1 必须逐个点名并完整执行表中绑定 Skill，不得以“全体子 Agent”等摘要替代。
- 仅调度器正式候选登记 `category=auto`；用户触发并通过完整正式流程的候选登记 `category=manual`。这些业务记录不得写入永久 `MEMORY.md`。
- 用户当前明确要求竞价、盯盘或时段总结时，才可手动执行对应 Skill；每次仅一轮，不建立后续触发器。
- 用户手动竞价/盯盘产出进入 `投研/yyyyMMdd-手动xx/`，不改 predictions、daily、短期事项或 `category=auto`。

## D1 预计算门禁

- **交易日判定**：统一使用 Tushare `trade_cal`。上海时间交易日 15:00 后才允许计算当天；收盘前、周末或休市日补最近已完成交易日。
- **数据就绪**：当日行情未就绪时记失败并进入 `retryable_dates`；历史空缓存允许重新获取，禁止用当前行业成分冒充历史成分。
- **发布条件**：只有状态为 `success`、覆盖率达标、公式版本/结构哈希/完整依赖指纹一致，且因子行、质量记录和运行记录 `run_id` 一致时才可供筛选读取。
- `partial`、`failed`、`legacy` 可审计或按规则重试，但不得覆盖同契约既有成功快照，也不得作为正式筛选数据。

## T7 正式选股、预判与回测

- 正式选股先保存当日 `screen_quant` / `screen_trend` 返回的真实 `screening_run_id`，再登记 `auto`；无有效筛选运行不得持久化为正式候选。
- 当晚预判目标为下一 SSE 交易日；当晚回测只核验目标日已成熟的历史预判。未成熟、legacy 和行情失败样本必须披露，不得按预判当天行情补算。
- T7/W1/M1 仅当 `optimization_gate.eligible=true` 时调参，并提交 `backtest_snapshot_id`、`expected_parent_version` 和完整因子权重；否则只记录建议，不修改配置。
- 调参只对当前非零权重因子小步调整并归一，保存执行者与原因；情绪权重仅在回测与情绪指数持续背离时调整，所有版本必须可追溯、可回滚。

## 非交易日与容错

- T1/T6/T7 遇 `trade_open=false` 直接返回、不产出；W1/M1/P1 使用最近已完成交易日数据。
- T1/T6/T7 关键接口首次失败后按 5 分钟、15 分钟各重试一次；401 鉴权、明确参数或配置错误不盲目重试。
- 数据类接口失败必须披露接口、状态、时间、实际数据日期和缺失项，禁止编造；只允许同类数据接口等价回退。
- 资讯类允许外部多源获取，至少两个来源交叉验证；全部失败写“资讯面不可用 + 已尝试来源”，不得解释为无风险。
- 非关键接口失败不得阻塞整份报告，但必须在来源表和风险提示中说明缺口。
- 本地 cron 仅可配置非盯盘任务，例如 `selection_backtest`；禁止配置 `watch_intraday`、`bidding_analysis` 或任何盘中循环。
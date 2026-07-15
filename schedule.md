# 定时任务清单

> 智能体在所在平台的定时任务/触发器中逐条注册；注册前清理同名旧任务。
> 时间为**中国标准时间（Asia/Shanghai）**。每条任务首步 `GET /health` 检查 `trade_open`，并**强制读取当日观察对象记忆** `agent记忆/daily/yyyyMMdd.md`。
> 调用名为数据服务 function 名，统一经 `POST /call`。
> **模式**：团队=启用子 Agent 团队协作；主=仅主 Agent 单跑（见 agents/TEAM.md）。

## 每日任务链（交易日）

| 序号 | 时间 | 任务 | 模式 | 绑定 skill / 子Agent | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| T1 | 08:30 | **盘前汇总**：报告按“一眼结论（核心摘要）→目录导读→详细正文”；首屏先给今日仓位、动态题材/具体事件 Top N、“题材/事件 → 今日关注个股”、最大风险/证伪和结论表。题材由消息面+热榜+涨停连板+量能资金识别，不限传统板块 | 团队 | `skills/pre-market/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md`、`skills/industry-analysis/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/review-learning/SKILL.md`；宏观/技术/基本面/情绪角色按 TEAM 主绑定 | `news_flash` `news_filter` `overseas_us` `macro_ppi/cpi/pmi` `price_hike_scan` `screen_sector` `sentiment_temperature` `sentiment_extreme_index` `market_timing` `hot_dc/ths` `selection_backtest` | `01-盘前汇总.md`（详尽正文）+ 独立重点推送（含路径/降级） |
| T2 | 09:25 | **竞价分析**（竞价结束）：昨日选股+关注+持仓+高热度+异常高开+竞价爆量+竞价成交额Top20，判断超预期/抄底 | 主 | `skills/bidding-analysis/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md` | `bidding_analysis` `market_timing` `sentiment_temperature` | `02-竞价分析.md` + 推送 |
| T3 | 09:35~11:30（每10分钟）| **盘中盯盘**：重点盯关注+持仓+相关板块，捕捉突发板块异动 | 主 | `skills/intraday-watch/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md` | `watch_intraday` | 异动推送 / 静默 |
| T4 | 12:50 | **盘中消息 + 早盘总结** | 主 | `skills/intraday-watch/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md` | `news_flash` `news_filter` `news_cctv` `market_limit`（新闻按 fallback 链） | `03-早盘总结.md`（+ 有消息影响则推送） |
| T5 | 13:05~14:55（每10分钟）| 盘中盯盘 | 主 | `skills/intraday-watch/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md` | `watch_intraday` | 异动推送 / 静默 |
| T6 | 17:30 | **当日总结**：报告按“一眼结论（核心摘要）→目录导读→详细正文”；首屏先给次日倾向、当日最强题材/具体事件及延续初判、“题材/事件 → 代表个股”、最大风险/证伪和结论表；动态题材不限传统板块 | 主 | `skills/post-market/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md` | `market_index` `sector_dc` `market_limit` `market_lianban` `news_filter` `news_anns` `hot_dc` `hot_ths` `hot_kpl_concept` `sentiment_temperature` `sentiment_extreme_index` `market_timing` `money_hsgt` `price_hike_scan`；新闻不足时以 `news_cctv` + 外部可信来源 fallback（关键接口按5/15分钟重试及fallback） | `04-当日总结.md`（详尽正文）+ 独立重点推送（含路径/降级） |
| T7 | 22:00 | **综合复盘 + 正式选股 + 回测 + 业绩增长参考池**：首屏先给今日题材复盘、晚间新增具体事件、次日候选及风险，按“题材/事件 → 个股”分组；报告顺序为“一眼结论（核心摘要）→目录导读→详细正文”。参考池与正式选股、记忆、回测调参隔离 | 团队 | `skills/post-market/SKILL.md`、`skills/review-learning/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/industry-analysis/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md`；基本面分析师执行业绩池，全部角色按 TEAM 主绑定 | `news_filter` `news_anns` `hot_dc` `hot_ths` `hot_kpl_concept` `market_limit` `market_lianban` `money_toplist` `screen_sector` `screen_quant`(top_n=50) `screen_trend` `fundamental_forecast` `fundamental_express`（新闻不足时 `news_cctv` + 外部可信来源；必要时 `fundamental_income` `fundamental_fina_indicator`） `predictions_backtest` `selection_backtest`；`log_selection` **仅正式候选调用，业绩池禁用** | `05-综合复盘.md`（含首屏结论表、正式候选综合理由表、全量业绩增长参考池表、回测调参）+ 独立重点推送 |

## 周期任务

| 序号 | 时间 | 任务 | 模式 | 绑定 | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| W1 | 周日 20:00 | **周回测** + 趋势周报 + 下周候选池（量化 `top_n=50`，按主线/产业链分组解读） + 因子调参 | 团队 | `skills/review-learning/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/industry-analysis/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md`；回测角色主绑定 | `selection_backtest` `predictions_backtest` `screen_sector/quant`(top_n=50)`/trend` `get_factor_config` `set_factor_weights` | `周报/...周报.md` |
| M1 | 每月最后交易日 21:00 | **月回测** + 月报 + 用户画像更新 + 因子调参 | 团队 | `skills/review-learning/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md`；回测角色主绑定 | `selection_backtest` `get_factor_config` `set_factor_weights` | `月报/...月报.md` |
| D1 | 交易日 17:45 | **全市场因子预计算**（落库 daily_factors，供次日选股读库提速） | 服务/主 | `skills/quant-screening/SKILL.md`、`skills/data-service/SKILL.md` | `precompute_daily_factors` | daily_factors 更新 |
| P1 | 周六 12:00 | 涨价链专项扫描 | 主 | `skills/industry-analysis/SKILL.md`、`skills/data-service/SKILL.md`、`skills/priority-framework/SKILL.md`、`skills/output-format/SKILL.md` | `price_hike_scan` `news_filter` `macro_ppi` | 更新 `观察池.md` |

## 关键规则
- **T1 盘前**显式执行 `skills/pre-market/SKILL.md`，并由 `skills/review-learning/SKILL.md` 先跑 `selection_backtest`/读近7日自动选股（重点前一日）；行业/候选分别执行 `skills/industry-analysis/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/quant-screening/SKILL.md`，最终由 `skills/priority-framework/SKILL.md` 排序、`skills/output-format/SKILL.md` 输出。
- **T2-T5**分别显式执行绑定列中的 `skills/bidding-analysis/SKILL.md` 或 `skills/intraday-watch/SKILL.md`；T4 新闻必须执行 `skills/data-service/SKILL.md` 的降级链。
- **T6/T7**显式执行 `skills/post-market/SKILL.md`；T7 另执行 `skills/review-learning/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/industry-analysis/SKILL.md`。
- **W1/M1/D1/P1**必须按绑定列逐个点名并完整执行对应 `skills/<name>/SKILL.md`，不得用“全体子Agent”“回测子Agent”等摘要替代 Skill 绑定。
- **盯盘（T3/T5）用主 Agent 单跑**，追求时效；重量级（T1/T7/W1/M1/用户分析）才启用团队。
- **回测→自主微调闭环**（T7/W1/M1，署名+留痕）：回测子Agent 出调参建议 → 主 Agent 复核 → `get_factor_config` 取最新因子列表 → `set_factor_weights` 提交全部因子权重（**仅微调权重≠0 的因子、小步、归一，署名 actor + reason**，缺失/多余/差异/和≠1 会被拒并指引）。综合情绪指数判断确定性，**回测与情绪指数持续背离时**才 `set_factor_weights(model=sentiment)` 微调情绪权重。每次修改返回 `version_id` 写入学习日志（可 `get_config_history`/`get_config_version` 定位、`restore_config_version` 回滚）。
- 仅调度器正式候选登记 `category=auto`；既有 watch/holding 按状态维护。用户主动研究默认 ephemeral，明确持久化后才新增 watch，且 watch 不参与 auto 调参。

## 非交易日
- T1~T7 遇 `trade_open=false` 直接返回不产出。
- W1/M1/P1 为研究/回测类，非交易日照常执行（数据用最近交易日）。

## 本地服务侧定时（可选）
容器内 cron 直接调 `service/cli.py`，如：
```
*/10 9-11 * * 1-5  python /app/service/cli.py call watch_intraday '{"codes":[...]}'
0 20 * * 0         python /app/service/cli.py call selection_backtest '{}'
```

## 定时容错与报告连续性（强制）

- **T1/T6/T7 关键接口**：遇 4xx/5xx/空数据，先记录接口、状态、时间和影响；首次失败后延迟 **5 分钟**重试一次，并在首次失败后 **15 分钟**再重试一次。
- **不盲目重试**：401 鉴权失败、明确参数错误或配置错误直接标配置问题并提示修复；402 按接口 fallback，不以相同参数空转。
- **重试后仍失败**：按 `skills/data-service/SKILL.md` 执行 fallback，标 `degraded`、缺失来源、实际数据日期与重试轨迹，继续完成可用章节；禁止编造。
- **market_index**：接受数组或逗号字符串；失败/空/部分 code 缺失时逐 code 调 `market_daily(code,start,end)` 取最近记录，注明实际日期。
- **T4 新闻降级链**：`news_flash` 402 → `news_filter(keyword)+news_cctv+外部搜索`；`news_filter` 同源失败 → `news_cctv` + 至少两个可信外部来源；全部失败 → 标“消息面不可用”，不得解释为无风险。
- **非关键接口**：失败不得阻塞整份报告；在来源表和风险提示中披露缺口即可继续。

> 调度器触发每一条任务时，主 Agent 与被启动角色仍须先完整加载固定 12 个 `skills/*/SKILL.md`（含 `stock-research`）；绑定列是主用 Skill 清单，不是免读清单。`stock-research` 仅作为用户主动单股调研入口，不加入定时 T1/T6/T7 的必执行绑定。
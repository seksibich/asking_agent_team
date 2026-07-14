# 定时任务清单

> 智能体在所在平台的定时任务/触发器中逐条注册；注册前清理同名旧任务。
> 时间为**中国标准时间（Asia/Shanghai）**。每条任务首步 `GET /health` 检查 `trade_open`，并**强制读取当日观察对象记忆** `agent记忆/daily/yyyyMMdd.md`。
> 调用名为数据服务 function 名，统一经 `POST /call`。
> **模式**：团队=启用子 Agent 团队协作；主=仅主 Agent 单跑（见 agents/TEAM.md）。

## 每日任务链（交易日）

| 序号 | 时间 | 任务 | 模式 | 绑定 skill / 子Agent | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| T1 | 08:30 | **盘前汇总**：消息+全球+期货+宏观(PPI/CPI/PMI)+情绪择时 → **重仓/空仓环境初判**；结合近7日自动选股(**重点前一日**)出今日关注板块与个股；高热度股入**临时观察列表** | 团队 | pre-market（宏观时事+技术趋势+基本面研报+情绪 子Agent） | `news_flash` `news_filter` `overseas_us` `macro_ppi/cpi/pmi` `price_hike_scan` `screen_sector` `sentiment_temperature` `market_timing` `hot_dc/ths` `selection_backtest` | `01-盘前汇总.md` + 推送 |
| T2 | 09:25 | **竞价分析**（竞价结束）：昨日选股+关注+持仓+高热度+异常高开+竞价爆量+竞价成交额Top20，判断超预期/抄底 | 主 | bidding-analysis | `bidding_analysis` `market_timing` `sentiment_temperature` | `02-竞价分析.md` + 推送 |
| T3 | 09:35~11:30（每10分钟）| **盘中盯盘**：重点盯关注+持仓+相关板块，捕捉突发板块异动 | 主 | intraday-watch | `watch_intraday` | 异动推送 / 静默 |
| T4 | 12:50 | **盘中消息 + 早盘总结** | 主 | intraday-watch | `news_flash` `news_filter` `market_limit` | `03-早盘总结.md`（+ 有消息影响则推送） |
| T5 | 13:05~14:55（每10分钟）| 盘中盯盘 | 主 | intraday-watch | `watch_intraday` | 异动推送 / 静默 |
| T6 | 17:30 | **当日总结**（盘后轻量，主 Agent） | 主 | post-market | `market_index` `sector_dc` `market_limit` `money_hsgt` | `04-当日总结.md` + 推送 |
| T7 | 22:00 | **综合复盘 + 选股 + 回测**（完整团队） | 团队 | post-market + review-learning + 全体子Agent | `money_toplist` `screen_sector` `screen_quant` `screen_trend` `predictions_backtest` `selection_backtest` `log_selection` | `05-综合复盘.md` + 次日候选 + 推送 |

## 周期任务

| 序号 | 时间 | 任务 | 模式 | 绑定 | 关键调用 | 产出 |
|---|---|---|---|---|---|---|
| W1 | 周日 20:00 | **周回测** + 趋势周报 + 下周候选池 + 因子调参 | 团队 | review-learning + 回测子Agent | `selection_backtest` `predictions_backtest` `screen_sector/quant/trend` `get_factor_config` `set_factor_weights` | `周报/...周报.md` |
| M1 | 每月最后交易日 21:00 | **月回测** + 月报 + 用户画像更新 + 因子调参 | 团队 | review-learning + 回测子Agent | `selection_backtest` `get_factor_config` `set_factor_weights` | `月报/...月报.md` |
| D1 | 交易日 17:45 | **全市场因子预计算**（落库 daily_factors，供次日选股读库提速） | 服务/主 | quant-screening | `precompute_daily_factors` | daily_factors 更新 |
| P1 | 周六 12:00 | 涨价链专项扫描 | 主 | industry-analysis | `price_hike_scan` `news_filter` `macro_ppi` | 更新 `观察池.md` |

## 关键规则
- **T1 盘前**要先跑 `selection_backtest`/读近7日自动选股（重点前一日），据此结合今日消息/宏观/期货，产出今日关注板块与个股，并写入当日观察对象记忆。
- **盯盘（T3/T5）用主 Agent 单跑**，追求时效；重量级（T1/T7/W1/M1/用户分析）才启用团队。
- **回测→调参闭环**（T7/W1/M1）：回测子Agent 出调参建议 → 主 Agent 复核 → `get_factor_config` 取最新因子列表 → `set_factor_weights` 提交全部因子权重（缺失/多余/差异/和≠1 会被拒并指引修正）。
- 每日 auto/watch/holding 标的用 `log_selection` 登记到服务端供回测。

## 非交易日
- T1~T7 遇 `trade_open=false` 直接返回不产出。
- W1/M1/P1 为研究/回测类，非交易日照常执行（数据用最近交易日）。

## 本地服务侧定时（可选）
容器内 cron 直接调 `service/cli.py`，如：
```
*/10 9-11 * * 1-5  python /app/service/cli.py call watch_intraday '{"codes":[...]}'
0 20 * * 0         python /app/service/cli.py call selection_backtest '{}'
```

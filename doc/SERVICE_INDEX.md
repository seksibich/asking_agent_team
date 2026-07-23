# 服务业务索引 & Agent↔服务交叉文档

> 本文把「数据服务功能」与「agent 技能 / 定时任务」双向关联，便于开发与排障时快速定位。
> 权威功能清单以运行时 `GET /functions` 为准（含参数）；调用协议与接口可用性说明见 `doc/AGENT_SERVICE_GUIDE.md` 与 `agent/skills/data-service/SKILL.md` 分组表。功能数随注册内容自动变化，不在本文硬编码。

## 零、统一业务响应、探针与版本自检

- `/live` 只判断进程存活；`/ready` 校验数据库、Tushare 配置、功能数量和模块装载，是 Docker、部署与生产流量的权威探针；`/health` 保留 Agent 诊断与版本协调结构，不作为 readiness。
- `GET /health` 的健康与五轨版本字段位于顶层；其他已连通的业务 JSON 响应无论成功或失败，都在保留原状态码的同时携带同口径 `health`，并保留顶层 `data_version`。
- Agent 必须先消费 `health` 并协调 `agent_doc_version`、`git_revision`、`data_version`、`selection_tag_version`、`portfolio_version`，再处理业务结果；同一目标版本元组每个任务只执行一次协调。旧服务缺少 `health` 时只补调一次 `/health`。
- 404、405、422、未捕获 500 以及权限和业务错误均遵守该结构；静态文件、根路径重定向、文档和 OpenAPI 不在业务 JSON 封装范围。

## 一、按业务分组的功能索引（功能 → 用途 → 主要使用方）

### portfolio 管理员自选
| 功能 | 用途 | 主要使用方 |
|---|---|---|
| portfolio_stock_search | 按股票名称或代码片段模糊搜索；新增时必须从结果选择标准股票 | Web 管理员 / Agent |
| portfolio_get | 获取按代码唯一的当前关注与持仓，以及独立业务版本 | Web 管理员 / Agent 开场同步 |
| portfolio_upload | 批量新增、更新或移除；重复代码取最后一项，持仓必填成本和整数手数 | Web 管理员 / Agent 状态更新 |

`portfolio_version` 在 `/health` 中暴露，只有当前内容实际变化才递增；访客 Key 不可搜索、读取或修改自选。

### market 行情
| 功能 | 用途 | 主要 skill |
|---|---|---|
| market_index | 大盘/三大指数日线，`codes` 兼容数组/字符串，空数据回退 | pre-market / post-market |
| market_realtime | 批量实时行情（代码+名称混合），盯盘/竞价用 | intraday-watch / bidding-analysis |
| market_daily / market_adj_daily / market_weekly | 个股/指数日线（不复权/前复权/周线），回测与趋势 | quant/stock-screening / review-learning |
| market_limit / market_lianban | 涨跌停炸板、连板梯队（情绪面） | post-market / 情绪面分析师 |
| market_stk_limit | 个股涨跌停价 | intraday-watch |
| market_index_dailybasic | 大盘每日指标（PE/PB/换手/市值，仅风险背景） | post-market |

### money 资金
| 功能 | 用途 | 主要 skill |
|---|---|---|
| money_hsgt / money_hsgt_top10 | 北向资金净流入/十大成交 | pre-market / post-market |
| money_toplist / money_topinst | 龙虎榜个股/席位 | post-market（资金复盘） |
| money_hm_list / money_hm_detail | 游资名录/交易明细 | post-market |
| money_flow / money_flow_ind | 个股/行业资金流向 | intraday-watch / industry-analysis |

### fundamental 基本面（预期驱动为主，PE 仅风险背景）
| 功能 | 用途 | 主要 skill |
|---|---|---|
| fundamental_forecast | 业绩预告（前瞻预期核心，无参默认最近交易日公告） | post-market（T3 业绩池）/ 基本面分析师 |
| fundamental_express | 业绩快报 | post-market（T3 业绩池） |
| fundamental_income / fundamental_fina_indicator | 利润表/财务指标（披露期验证、重点复核） | stock-research / 基本面分析师 |
| fundamental_daily_basic | 个股每日指标（PE/PB，风险背景） | stock-research |

### macro 宏观（涨价强相关）
`macro_ppi`（涨价链锚）`macro_cpi` `macro_pmi` `macro_m` → pre-market / industry-analysis / 宏观分析师。

### price_hike 涨价（第一重心）
`price_hike_scan`：价格信号 + 外部渠道提示（新闻侧因无权限为空） → industry-analysis / pre-market / P1 涨价专项。

### hot 热度
`hot_dc` `hot_ths` `hot_kpl_list` → 动态题材识别（pre/post-market、industry-analysis）。

### sector 板块
`sector_dc`（板块排名）`sector_index_classify`（行业分类）`sector_sw_daily` `sector_ths_daily`（板块动量） → quant-screening（screen_sector）/ industry-analysis。

### sentiment 情绪与择时
| 功能 | 用途 | 主要 skill |
|---|---|---|
| sentiment_temperature | 0-100 情绪温度（权重可配） | 情绪面分析师 / pre/post-market |
| sentiment_extreme_index | 0-100 极端指数（固定规则，Agent 不复算） | 情绪面分析师 |
| market_timing | 择时：冰点/高热 streak + 出手买入权重 | priority-framework（择时叠加） |
| bidding_analysis | 集合竞价数据 + 成交额 TopN + 异常高开 | bidding-analysis（仅用户显式调用） |
| get_sentiment_config / set_sentiment_config | 情绪归一窗口读/写（管理员） | review-learning |

### screening 选股/手动盯盘/预计算/因子配置
| 功能 | 用途 | 主要 skill |
|---|---|---|
| screen_quant | 量化多因子选股（默认 8 个启用因子 + 7 个候选 0 权重因子） | quant-screening |
| screen_trend | 趋势+行业逻辑选股 | stock-screening |
| screen_sector | 板块轮动量化 | quant-screening |
| watch_intraday | 用户明确请求时的单轮盘中异动扫描 | intraday-watch（禁止定时/循环） |
| precompute_daily_factors / precompute_status | 服务端 16:00 日终收口与管理员单次诊断 | quant-screening（Agent 只读状态） |
| get_factor_config / set_factor_weights | 因子权重读/写（写=管理员，留痕） | review-learning（调参闭环） |
| get_config_history / get_config_version / restore_config_version | 配置留痕/定位/回滚（回滚=管理员） | review-learning |

### watch 服务端量化盯盘

`quant_watch_status`（可按日期读取最近 30 个自然日内的聚合结果；省略日期时返回最近有数据日）`quant_watch_get_config` / `quant_watch_set_config`（管理员设置）`quant_watch_scan_once`（仅用户明确要求时单次诊断）。状态响应区分请求日期、实际日期、当前上海日期和历史标记；历史读取不触发扫描。服务端可在连续竞价自动扫描，使用数据库租约、fencing token、消息持久化和通知幂等；Agent 不轮询、不自动触发，也不把盘中结论写成正式选股或日终因子。

### research / review
`research_build`（投研数据包）→ industry-analysis / stock-research。
`selection_tag_catalog`（版本化固定标签及说明）→ 正式选股上传前读取；`log_selection`（同日筛选运行、精炼事件/理由/标签）`selection_dashboard`（默认四个交易日、实时行情、题材/日期智能聚合；管理员查看关注/持仓或全部类别时实时并入当前自选 `portfolio_items`，标注「当前自选」，不计入回测）`log_prediction` `selection_backtest` `predictions_backtest`（回测闭环，仅正式候选/预判登记）→ review-learning。`/health.selection_tag_version` 变化时必须刷新标签目录。

> **资讯类不在数据服务**（新闻/时政/公告/美股外盘，token 无权限）：由 agent 从各财经平台多源获取，≥2 来源交叉，见 `agent/skills/data-service/SKILL.md`「资讯类外部获取」。

## 二、按定时任务的接口/技能交叉（schedule.md）

| 任务 | 时间 | 主用 skill | 关键数据接口 | 资讯（外部多源） |
|---|---|---|---|---|
| T1 盘前汇总 | 08:30 | pre-market + 全角色 | macro_ppi/cpi/pmi、price_hike_scan、screen_sector、sentiment_temperature/extreme、market_timing、hot_dc/ths/kpl_list、overseas_hk、selection_backtest；正式 auto 候选必须来自真实 screen_quant/screen_trend 并携带 screening_run_id | 新闻/时政/外盘 |
| T3 综合复盘、选股与成熟回测 | 22:00 | post-market + review + 选股 + 全角色 | market_index、sector_dc、market_limit/lianban、hot_dc/ths/kpl_list、sentiment_*、market_timing、money_hsgt/toplist、price_hike_scan、screen_sector/quant/trend、fundamental_forecast/express、log_prediction、predictions_backtest、selection_backtest、selection_tag_catalog→log_selection；只读 health.daily_finalize / precompute_status | 新闻/公告 |

> **v2.7.0：已取消 T2（17:30 当日总结）**。交易日只保留 T1（08:30）与 T3（22:00），当天市场复盘并入 T3。
| W1 周回测/周报 | 周日 20:00 | review + 选股 | selection_backtest、predictions_backtest、screen_*、get/set_factor_weights | — |
| M1 月回测/月报 | 月末 21:00 | review + quant | selection_backtest、get/set_factor_weights | — |
| P1 涨价链扫描 | 周六 12:00 | industry-analysis | price_hike_scan、macro_ppi | 行业价格/资讯 |

> 全市场个股因子与行业评分由服务端在交易日 16:00 自动收口，不注册 Agent D1，不因状态失败自动调用 `precompute_daily_factors`。

## 三、降级二分（贯穿全部取数）

- **数据类接口禁止降级——失败则失败**：如实披露失败接口/状态/时间/实际数据日期/缺失项，禁止编造；仅允许同类数据接口等价回退（`market_index`→`market_daily`，标 `degraded`）。
- **资讯类允许多源外部获取**：新闻/时政/公告/外盘从各财经平台 ≥2 来源交叉验证，标来源与时间；全失败标「资讯面不可用 + 已尝试来源」，不得当作无风险。

## 四、扩展新功能

在 `agent/skills/<skill>/scripts/` 用 `@register` 声明，`service/loader.py` 自动发现、`/functions` 自动收录、`data_version` 自动变化。详见 `doc/AGENT_SERVICE_GUIDE.md` 第 5 节。

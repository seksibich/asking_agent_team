---
name: data-service
description: 通过本地数据服务获取 A股行情/资金/财务/宏观/涨价/板块数据。智能体无法直连 tushare，所有市场数据必须走本地服务的统一 /call 接口；新闻/公告/外盘等资讯类不在数据服务（当前 token 无权限），由外部财经平台多源获取。需要任何市场数据时使用此技能。
user-invocable: true
disable-model-invocation: false
---

# 本地数据服务调用规范

## 为什么需要本地服务

智能体运行环境无法直接访问 tushare。工具包在 `service/` 提供一个 Docker 化的 FastAPI 服务，
封装 tushare 接口与分析脚本，智能体通过 HTTP 调用。**服务当前部署形态：本地 Docker。**

## 服务地址与鉴权

- **当前：本地 Mac Docker**，基址 `http://localhost:18901`
- **后续上云**：部署云服务器后改为公网 API 基址（协议/鉴权/功能不变），同步更新记忆 `service_state.json` 的 `base_url`
- 鉴权：请求头 `X-API-Key: {service_api_key}`（值见 `init.md` / `.env`）
- Key 分级：管理员 Key(`API_KEY`) 全权限；访客 Key(`USER_API_KEY`) 可查看/选股/读情绪/查看回测结果，但不能改权重/归一窗口、不能运行全市场预计算（否则 403）。智能体用的是管理员 Key
- 详细服务文档见 `doc/AGENT_SERVICE_GUIDE.md`

## 三个核心端点

| 端点 | 用途 |
|---|---|
| `GET /health` | 健康检查，返回 `status/date/trade_open/data_version`，及 `agent_doc_version`/`git_revision`（文档版本对齐用） |
| `GET /functions` | 全部功能索引（名称/分组/描述/参数），含 `data_version` |
| `POST /call` | 统一调用：body `{"function":"<名>","params":{...}}` |

**所有取数都通过 `POST /call`**，例如：
```json
POST /call
{"function": "market_limit", "params": {"date": "20260714"}}
```
返回：`{ "ok": true, "function": "...", "fetched_at": "...", "data": {...}, "data_version": "v1.xxxx" }`

## ★ 版本机制（必须遵守）

1. 每个响应都带 `data_version`（也在响应头 `X-Data-Version`）。
2. 智能体在记忆中保存「最近已知 data_version」与「功能索引」（见 memory 规则）。
3. **每次调用任何功能后，对比返回的 `data_version` 与记忆中的版本**：
   - 一致 → 继续使用记忆中的功能索引
   - 不一致 → 立即 `GET /functions` 拉取最新索引，更新记忆中的版本与索引，再继续
4. 初始化时先 `GET /functions` 建立索引与版本基线。
5. 版本号由服务端功能索引内容自动生成：任何功能新增/参数/描述变化都会改变版本，无需人工维护。

## 功能分组（通过 /functions 获取完整清单与参数）

| 分组 | 代表功能 |
|---|---|
| market | market_index, market_realtime（代码/名称批量实时行情）, market_daily, market_adj_daily, market_weekly, market_limit, market_lianban, market_stk_limit, market_index_dailybasic |
| money | money_flow, money_flow_ind, money_hsgt, money_hsgt_top10, money_toplist, money_topinst, money_hm_list, money_hm_detail |
| fundamental | fundamental_daily_basic, fundamental_income, fundamental_forecast, fundamental_express, fundamental_fina_indicator |
| macro（涨价强相关） | macro_ppi, macro_cpi, macro_pmi, macro_m |
| price_hike（重心） | price_hike_scan |
| ~~news / 时事~~ | **已移除**：当前 token 无 news/anns_d/cctv_news 权限（402）。新闻快讯、新闻联播、公司公告一律走外部财经平台多源获取（见「资讯类外部获取」） |
| overseas | overseas_hk（港股）。**美股 overseas_us 已移除**（token 无 us_daily 数据），外盘美股/大宗商品改由外部获取 |
| hot | hot_dc, hot_ths, hot_kpl_list（**hot_kpl_concept 已移除**：接口名不可用；题材强度用 kpl_list+dc/ths+涨停连板聚合） |
| sector | sector_dc, sector_index_classify, sector_sw_daily, sector_ths_daily |
| meta | meta_stock_basic, meta_trade_cal |
| screening | screen_trend, screen_quant, screen_sector, watch_intraday, get_factor_config, set_factor_weights, precompute_daily_factors |
| sentiment | sentiment_temperature（0-100 情绪温度，11 项：含涨跌家数比、平均涨幅、大盘/平均股价指数振幅方向+实体长度等+窗口低/均/高）, market_timing（择时：连续冰点/高热+出手权重）, get_sentiment_config / set_sentiment_config（归一窗口 3-30 天，落库）, bidding_analysis（09:25 竞价分析数据） |
| research | research_build |
| review | log_selection（category=auto/watch/holding，DB 幂等去重）, log_prediction（DB 幂等）, selection_backtest（成熟样本固化）, predictions_backtest |

> 以 `/functions` 返回为准，本表仅速览；新增功能会自动出现在 `/functions` 并改变版本号。

### 股票标的查询与批量行情

- `meta_stock_basic`：默认返回全量上市股票；传 `codes` 按代码精确过滤，传 `name`/`names` 按股票名称关键词包含匹配，返回 `matched_codes`、`missing_codes`、`missing_names`。
- `market_realtime`：支持 `codes`、`name`/`names`，可混合批量查询；服务端先解析名称再请求实时行情，返回 `resolved`、`missing_codes`、`missing_names` 和 `degraded`。代码和名称都不传时返回参数错误。
- 关注与持仓中的股票优先直接调用 `market_realtime` 获取行情；名称解析不要自行调用无过滤的 `meta_stock_basic` 后再全量遍历。

### 全市场因子预计算

- `precompute_daily_factors` 只在交易日写入目标日；`full=true` 用于首次补算或断档补算，返回每个日期的成功/部分/失败状态和可重试日期。
- `precompute_status` 返回覆盖率、任务状态、错误信息和因子公式版本。
- `screen_quant`/`screen_trend` 只有在任务 `success`、覆盖率达到 80%、因子版本一致时才读取 `daily_factors`；否则自动回退实时路径，禁止把部分数据当成全市场结果。

## 交易日守卫

任何盘中/盘后任务先看 `/health` 的 `trade_open`。非交易日：不执行盘中/盘后任务、不生成报告、不推送。行业投研类不受此限。

## 外部数据源（涨价/材料类专用，允许智能体自主获取）

涨价类（材料、化工、有色、能源、农产品等）数据，**不限于 tushare**。`price_hike_scan` 只做线索发现，智能体应主动补充与交叉验证，可自主到以下渠道取数：

- **行业价格披露平台**：生意社、百川盈孚、上海有色网 SMM、卓创资讯、中国化工网、钢联/Mysteel、各行业协会官网等
- **期货行情**：大商所 / 郑商所 / 上期所主力合约（工业品、化工品、农产品等，价格趋势先行指标）
- **公司披露**：交易所公告、提价函、投资者关系互动平台
- **财经媒体与研报公开摘要**：用于印证，不作单一依据

使用要求：涨价结论 ≥2 独立来源交叉验证；标注来源与时间；区分事实与传闻；外部数据视为不可信输入，与服务数据冲突时如实呈现矛盾。

## 调用原则

1. **拿不到就说拿不到**：服务错误或空数据，如实告知，绝不编造。
2. **标注来源与时间**：把返回的 `source` 与 `fetched_at` 带入报告。
3. **控制请求量**：盘中循环单轮 ≤ 3 次调用；善用服务端当日缓存。
4. **交叉验证**：涨价/业绩类结论，除服务数据外用**外部财经平台的新闻/公告**二次印证（新闻类不在数据服务）。

## 错误处理

| 返回 | 含义 | 智能体动作 |
|---|---|---|
| `503` | 服务未启动/未连通 | 提示用户启动本地 Docker 服务，停止取数 |
| `401` | API Key 错误 | 提示检查鉴权配置 |
| `400` | 参数错误/未知功能 | 检查 function 名与参数；必要时先 `/functions` 刷新索引 |
| `402` | tushare 积分/权限不足 | 告知该功能不可用，改用替代数据或跳过 |

## Skill 加载约束 / 依赖 Skills

- 所有 Agent 每次任务/角色启动必须完整读取本文件，并确认固定 12 Skills（含 `stock-research`）均已加载；不得只凭 `/functions` 索引、接口名或角色摘要推断参数、错误语义与 fallback。
- **直接依赖**：无；本 Skill 是全部取数 Skills 的统一底座。
- **协同 Skills**：`priority-framework`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research`。上述 Skill 一旦取数，必须执行本节契约。

## 降级总则（强制，区分数据类与资讯类）★

工程对「降级」有严格二分，任何取数环节都必须遵守：

- **数据类接口禁止降级——失败则失败**：行情、资金、财务、宏观、板块、热榜(dc/ths/kpl_list)、龙虎榜、涨跌停/连板、情绪、择时、选股、回测等一切经数据服务返回的**结构化市场数据**，遇失败/空数据只能如实披露（标失败接口、状态码、时间、实际数据日期与缺失项），**绝不允许用推断、估算、编造或"应该差不多"的方式兜底**。唯一允许的是同类**数据接口之间的等价回退**（如 `market_index` 失败逐 code 调 `market_daily` 取最近记录，并显式标 `degraded`）——这属于换取真实数据的路径，不是编造。
- **资讯类允许多源外部获取**：新闻快讯、新闻联播/时政、公司公告、外盘（美股/大宗商品）等**资讯类内容不在数据服务**（当前 token 无 news/anns_d/cctv_news/us_daily 权限）。这类信息由 agent 直接从**各财经平台多源获取**（≥2 个可信来源交叉验证，标名称/URL/出处与时间，区分事实与传闻）。资讯缺失时标"资讯面暂缺 + 已查来源"，**不得把查不到解释为无消息、无利空或无风险**。

> 一句话：**数据宁缺毋编，资讯多方求证。** 数据优先、结论优先——报告先给可核验的数据与结论，缺口显式暴露。

## 资讯类外部获取（新闻 / 公告 / 外盘）★

数据服务不再提供新闻、公司公告与美股外盘接口，需要这些资讯时：

1. **新闻 / 时政 / 题材催化**：直接检索各财经平台（如财联社、新浪财经、东方财富、华尔街见闻、央视新闻联播稿源、行业垂直媒体等）与可信外部搜索；同一事件 ≥2 来源交叉，标来源与时间。
2. **公司公告 / 业绩预告线索**：优先交易所公告、巨潮资讯、公司投资者关系互动平台；业绩数值仍以数据服务的 `fundamental_forecast`/`fundamental_express`/`fundamental_income` 等**结构化财务接口**为准（这些接口可用）。
3. **外盘美股 / 大宗商品**：从外部行情源（指数收盘、期货主力）获取隔夜表现，标来源与时间；港股可用数据服务 `overseas_hk`。
4. 全部资讯来源均失败：标"资讯面不可用 + 已尝试来源"，继续完成基于可用数据的章节，禁止编造。

## 统一 fallback 与延迟重试（强制）

### market_index 降级链（数据接口间等价回退，非编造）

1. `market_index` 的 `codes` 参数允许**代码数组**或**逗号分隔字符串**；调用前保留原 code 清单用于完整性核验。
2. 若请求出现可降级的 4xx（**不含 401、明确参数/配置错误**）、5xx、空数据，或返回只覆盖部分 code，则对失败/缺失 code **逐个调用** `market_daily(code,start,end)`；401/配置错误须先修复鉴权或配置，不得盲目调用同服务 fallback。
3. 每个 code 取区间内最近一条可用记录；输出必须标 `degraded=true`、原失败接口、实际 `trade_date`（不得伪装成请求日/实时值）及仍缺失的 code。
4. `market_daily` 仍失败时保留缺失项，继续完成其他可用部分，禁止编造指数点位、涨跌幅或日期。

### 资讯（新闻/公告/外盘）获取链

1. 资讯类不在数据服务，直接按上文「资讯类外部获取」从各财经平台多源检索并交叉验证。
2. 至少 **2 个可信外部来源**互相印证；标名称、URL/出处与时间，区分事实与传闻。
3. 全部来源失败：标"资讯面不可用"及已尝试来源；**不得把不可用解释为无消息、无利空或无风险**。

### 定时任务 T1/T6/T7

- 关键接口遇 4xx/5xx/空数据：先记录首次失败；延后 **5 分钟**重试一次，再延后至首次失败后 **15 分钟**重试一次。
- 401 鉴权失败及明确的参数/配置错误不盲目重试：立即标配置问题并提示修复。402 按对应 fallback 执行。
- 两次延迟重试后关键接口仍失败：执行上述或接口专属 fallback，标 `degraded`、缺失来源、尝试时间与实际数据日期，继续可完成部分。
- 非关键接口失败不阻塞整份报告；所有任务坚持“缺失可见、报告继续、禁止编造”。T4 资讯直接执行「资讯类外部获取」，不因单一来源失败中止早盘总结。
---
name: data-service
description: 通过本地数据服务获取 A股行情/资金/财务/宏观/涨价/新闻/板块数据。智能体无法直连 tushare，所有数据必须走本地服务的统一 /call 接口。需要任何市场数据时使用此技能。
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
- Key 分级：管理员 Key(`API_KEY`) 全权限；用户 Key(`USER_API_KEY`) 只读，不能改权重/归一窗口、不能触发回测（否则 403）。智能体用的是管理员 Key
- 详细服务文档见 `service/AGENT_SERVICE_GUIDE.md`

## 三个核心端点

| 端点 | 用途 |
|---|---|
| `GET /health` | 健康检查，返回 `status/date/trade_open/data_version` |
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
| market | market_index, market_realtime, market_daily, market_adj_daily, market_weekly, market_limit, market_lianban, market_stk_limit, market_index_dailybasic |
| money | money_flow, money_flow_ind, money_hsgt, money_hsgt_top10, money_toplist, money_topinst, money_hm_list, money_hm_detail |
| fundamental | fundamental_daily_basic, fundamental_income, fundamental_forecast, fundamental_express, fundamental_fina_indicator |
| macro（涨价强相关） | macro_ppi, macro_cpi, macro_pmi, macro_m |
| price_hike（重心） | price_hike_scan |
| news / 时事 | news_flash, news_filter, news_anns, news_cctv |
| overseas | overseas_us, overseas_hk |
| hot | hot_dc, hot_ths, hot_kpl_list, hot_kpl_concept |
| sector | sector_dc, sector_index_classify, sector_sw_daily, sector_ths_daily |
| meta | meta_stock_basic, meta_trade_cal |
| screening | screen_trend, screen_quant, screen_sector, watch_intraday, get_factor_config, set_factor_weights, precompute_daily_factors |
| sentiment | sentiment_temperature（0-100 情绪温度，11 项：含涨跌家数比、平均涨幅、大盘/平均股价指数振幅方向+实体长度等+窗口低/均/高）, market_timing（择时：连续冰点/高热+出手权重）, get_sentiment_config / set_sentiment_config（归一窗口 3-30 天，落库）, bidding_analysis（09:25 竞价分析数据） |
| research | research_build |
| review | log_selection（category=auto/watch/holding，DB 幂等去重）, log_prediction（DB 幂等）, selection_backtest（成熟样本固化）, predictions_backtest |

> 以 `/functions` 返回为准，本表仅速览；新增功能会自动出现在 `/functions` 并改变版本号。

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
4. **交叉验证**：涨价/业绩类结论，除服务数据外用 news_filter 或公告二次印证。

## 错误处理

| 返回 | 含义 | 智能体动作 |
|---|---|---|
| `503` | 服务未启动/未连通 | 提示用户启动本地 Docker 服务，停止取数 |
| `401` | API Key 错误 | 提示检查鉴权配置 |
| `400` | 参数错误/未知功能 | 检查 function 名与参数；必要时先 `/functions` 刷新索引 |
| `402` | tushare 积分/权限不足 | 告知该功能不可用，改用替代数据或跳过 |

# 数据服务使用文档（供智能体阅读）

本文件描述本地数据服务的调用协议、版本机制、功能索引与扩展方式。
服务当前部署形态：**本地 Mac Docker，基址 `http://localhost:18901`**。
后续上云后改为公网 API 基址（协议/鉴权/功能不变），智能体同步更新记忆 `service_state.json` 的 `base_url` 即可。
鉴权：所有请求带头 `X-API-Key`（值见 `init.md` / `.env`）。

## 1. 协议总览

| 端点 | 方法 | 用途 |
|---|---|---|
| `/health` | GET | 健康检查：`status/date/trade_open/data_version/functions` |
| `/functions` | GET | 全部功能索引（名称/分组/描述/参数/返回），含 `data_version` |
| `/call` | POST | 统一调用：`{"function":"<名>","params":{...}}` |

鉴权：所有请求带头 `X-API-Key: <service_api_key>`（见 `.env`）。

调用返回结构：
```json
{
  "ok": true,
  "function": "market_limit",
  "fetched_at": "2026-07-14 15:10:00",
  "data": { "source": "limit_list_d", "fetched_at": "...", "rows": [ ... ] },
  "data_version": "v1.6d62877a"
}
```
错误返回：`{"ok": false, "error": "...", "status": 4xx/5xx, "data_version": "..."}`。

## 2. 版本机制（智能体必须遵守）

- `data_version` 由「功能索引内容」自动哈希生成。**新增功能或修改参数/描述都会改变版本号**。
- 每个响应（含响应头 `X-Data-Version`）都带 `data_version`。
- 智能体流程：
  1. 初始化：`GET /functions`，把 `data_version` 与功能索引存入记忆（见 memory 规则）。
  2. 每次调用任意功能后，对比返回 `data_version` 与记忆版本。
  3. 不一致 → 立即重新 `GET /functions`，更新记忆中的版本与索引，再继续。
- 这样即使服务端新增/调整了功能，智能体也能自动感知并获取最新能力，无需改初始化提示词。

## 3. 功能分组

调用 `GET /functions` 获取权威清单（含每个功能的 `params` 定义）。分组：
`market / money / fundamental / macro / price_hike / news / overseas / hot / sector / meta / screening / research / review`。

常用：
- 行情：`market_index` `market_daily` `market_adj_daily` `market_limit` `market_lianban`
- 板块：`sector_dc` `sector_sw_daily` `sector_index_classify`
- 资金：`money_hsgt` `money_toplist` `money_flow_ind`
- 涨价/宏观：`price_hike_scan` `macro_ppi` `macro_cpi` `macro_pmi`
- 选股/盯盘：`screen_trend` `screen_quant` `screen_sector` `watch_intraday`
- 因子预计算：`precompute_daily_factors`（盘后落库 daily_factors，选股读库提速）
- 因子配置：`get_factor_config` `set_factor_weights`（提交全部因子权重，缺失/多余/和≠1 报错并指引）
- 情绪温度：`sentiment_temperature`（0-100，指标权重 model=sentiment 可配置）
- 择时：`market_timing`（连续冰点/高热、出手买入权重提示）
- 竞价分析：`bidding_analysis`（09:25 竞价数据 + 竞价成交额 TopN + 异常高开/竞价爆量）
- 投研：`research_build`
- 回测：`log_selection`(category=auto/watch/holding) `log_prediction` `selection_backtest` `predictions_backtest`

## 持久化（DB）

- 选股/预判/前向收益/回测快照落库（`selections`/`predictions`/`selection_forward_returns`/`backtest_snapshots`，DDL 见 `service/db/schema.sql`）。
- `DB_URL` 未设=本地 SQLite（`DATA_DIR/stock_agent.db`，随卷持久化）；上云设为 RDS MySQL。
- `log_selection` 按 (日期,代码,category)、`log_prediction` 按 (日期,标的,方向) **幂等去重**；`selection_backtest` 对已满持有期的样本把前向收益**固化到 DB**，之后只增量计算。

## 4. 错误码

| status | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 / 未知功能 | 校验 function 与 params；必要时先刷新 `/functions` |
| 401 | 鉴权失败 | 检查 X-API-Key |
| 402 | tushare 积分/权限不足 | 跳过该功能或改用替代 |
| 503 | 服务未启动 | 提示启动本地 Docker |

## 5. 扩展方式（开发者）

新增一个数据/分析功能：
1. 在 `skills/<某skill>/scripts/` 下新增或编辑模块。
2. 用装饰器登记：
   ```python
   from registry import register
   import common

   @register("my_new_func", "market", "功能描述",
             params=[{"name":"code","type":"string","required":True}],
             returns="返回说明")
   def my_new_func(p: dict) -> dict:
       pro = common.get_pro()
       ...
       return {"source": "...", "fetched_at": common.now_str(), "rows": [...]}
   ```
3. 重启/重建服务。`loader` 自动发现新模块，`/functions` 自动收录，`data_version` 自动变化。
   智能体在下次调用后会检测到版本变化并刷新索引 —— **无需手工改版本号**。

> 破坏性协议变更（改调用格式等）时，手工把 `service/registry.py` 的 `SCHEMA_VERSION` +1。

## 6. 本地运行 / 调试

```bash
# 在仓库根目录（部署入口已移到根，详见 DEPLOY.md）
docker compose up -d --build
curl -H "X-API-Key: <key>" http://localhost:18901/health
curl -H "X-API-Key: <key>" http://localhost:18901/functions

# 不启服务直接调试（在容器内或本机 service/ 目录下）
cd service
python cli.py functions
python cli.py call screen_sector '{"top_n":10}'
```

Web 面板（与服务同源）：浏览器打开 `http://localhost:18901/ui/`，设置里填 X-API-Key。

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
| `/whoami` | GET | 返回当前 Key 角色：`role`(admin/user) / `is_admin` / `admin_only`(仅管理员可调用的功能名) |
| `/call` | POST | 统一调用：`{"function":"<名>","params":{...}}` |

鉴权：所有请求带头 `X-API-Key: <service_api_key>`（见 `.env`）。

**Key 分级（管理员 vs 用户）**：
- **管理员 Key**（`.env` 的 `API_KEY`，向后兼容，亦可用 `ADMIN_API_KEY`）：完整权限。
- **访客 Key**（`.env` 的 `USER_API_KEY`，可选）：可查看/选股/读情绪/查看回测结果，但**不能**调用以下管理员专属功能，否则返回 `403`：
  `set_factor_weights`（改因子权重）、`set_sentiment_config`（改归一窗口）、`precompute_daily_factors`（写入全市场预计算结果）。
- 未输入 token 时，Web 界面按访客隐藏管理员入口；若服务未配置任何 Key，服务端允许只读访问。若服务已配置管理员 Key，未授权请求仍返回 `401`；管理员操作必须使用 `API_KEY` 或 `ADMIN_API_KEY`。
- **访客 Key 动态管理**（仅管理员，落库 `config_kv.user_api_keys`；Web 设置页「访客 Key 管理」可视化操作）：
  - `GET /admin/user-keys`：列出全部访客 Key
  - `POST /admin/user-keys` `{label}`：生成新访客 Key
  - `POST /admin/user-keys/toggle` `{id}`：启用/停用
  - `POST /admin/user-keys/delete` `{id}`：删除（立即失效）
  动态访客 Key 与 `.env` 的 `USER_API_KEY` 均视为用户角色（可并存）。

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
- 因子配置：`get_factor_config` `set_factor_weights`（提交全部因子权重，缺失/多余/和≠1 报错并指引）。
  个股(stock)模型含 7 个默认启用因子 + 7 个默认 0 权重候选因子（mom_6_1/max_lottery/downside_vol/amihud_illiq/small_size/value_bm/earnings_yield，源自学术/机构常用）；screen_quant 仅返回权重≠0 的因子列
- 配置留痕/版本：`set_factor_weights` 与 `set_sentiment_config` 支持传 `actor`（署名）+ `reason`，每次成功修改生成类 commit 的 `version_id` 并落库留痕（config_versions 表，含 parent/payload）。
  - `get_config_history {config_key|model, limit}`：查配置变更历史（倒序）
  - `get_config_version {version_id}`：按版本号定位当时完整权重快照
  - `restore_config_version {version_id, actor, reason}`（管理员）：回滚到历史版本（回滚亦留痕为新版本）
  - config_key 约定：`factor_weights:<stock|sector|trend|sentiment>`、`sentiment_window`
- 情绪温度：`sentiment_temperature`（0-100，11 项指标；含大盘/平均股价指数振幅方向+实体长度，指标权重 model=sentiment 可配置）
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
| 403 | 权限不足（用户 Key 调用管理员专属功能） | 改用管理员 Key（改权重/窗口、触发回测需管理员） |
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

## 7. v1.1.0 Agent 接口契约与统一 fallback

### market_index

- `code` 参数允许代码数组或逗号分隔字符串；Agent 必须保存请求 code 集并核对返回完整性。
- 4xx/5xx、空数据或部分 code 缺失时，对失败/缺失 code 逐个调用 `market_daily(code,start,end)`，取区间最近记录。
- 降级结果必须标 `degraded=true`、原失败接口/状态、实际 `trade_date`、仍缺失 code；不得将最近历史记录表述为实时或请求日数据。

### 新闻

- `news_flash` 402 → `news_filter(keyword)` + `news_cctv` + 外部搜索。
- `news_filter` 同源失败 → 继续 `news_cctv` + 至少两个可信外部来源，并标出处/时间。
- 全部失败 → 标“消息面不可用”与失败来源；不可解释为无消息、无利空或无风险。

### 定时任务重试

T1/T6/T7 关键接口 4xx/5xx/空数据：记录首次失败，首次失败后 5 分钟重试一次、15 分钟再重试一次。401 鉴权及明确参数/配置错误不盲目重试；402 直接进入专属 fallback。最终仍失败时标降级与缺失并继续可完成部分；非关键接口失败不阻塞整份报告，禁止编造。T4 新闻直接使用上述新闻降级链。

## 8. v1.1.0 情绪接口速览

- `sentiment_temperature`：返回 `temperature`、`level`、`indicators`、`weights`、`window_size`；指标/权重以实时返回为准。
- `sentiment_extreme_index`：参数 `date`、`days`；极端指数固定按含当日最近 7 个交易日归一，平均振幅与成交额缩量子分各 50%，返回 `extreme_index/components/recent/selection_bias`。Agent 只消费接口结果，不自行复算或调整固定规则。
- `market_lianban` + `market_limit`：用于连板梯队、连板个股、断板及 1-3 日反包分析。极端指数 ≥80 强倾向、60-80 适度倾向、<60 不额外倾斜；这只是风格优先级，不能替代四维逻辑与风险过滤。
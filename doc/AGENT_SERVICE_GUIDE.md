# 数据服务使用文档（供智能体阅读）

本文件描述本地数据服务的调用协议、版本机制、功能索引与扩展方式。
服务当前部署形态：**本地 Mac Docker，基址 `http://localhost:18901`**。
后续上云后改为公网 API 基址（协议/鉴权/功能不变），同时更新运行期 `服务状态与能力.md` 与 `关注与持仓.md` 的 `BASE_URL`。
鉴权：所有请求带头 `X-API-Key`（值见 `init.md` / `.env`）。

## 1. 协议总览

| 端点 | 方法 | 用途 |
|---|---|---|
| `/live` | GET | 进程存活探针：不访问数据库和外部数据源 |
| `/ready` | GET | 生产流量就绪探针：数据库、Tushare 配置、功能数量和模块装载全部通过才返回 200，否则 503 |
| `/health` | GET | 兼容诊断快照：市场阶段、日终/盯盘状态、版本和依赖状态；不作为生产流量探针 |
| `/functions` | GET | 全部功能索引（名称/分组/描述/参数/返回），含 `data_version` |
| `/whoami` | GET | 返回当前 Key 角色：`role`(admin/user) / `is_admin` / `admin_only`(仅管理员可调用的功能名) |
| `/call` | POST | 统一调用：`{"function":"<名>","params":{...}}` |
| `/selections/quotes` | POST | 仅刷新当前选股列表行情，不重新执行看板查询 |
| `/admin/monitor/daily` | GET | 管理员生成/读取指定日接口与量化盯盘中文运行汇总 |

鉴权：所有业务请求带头 `X-API-Key: <service_api_key>`（见 `.env`）；`/live`、`/ready` 与 `/health` 用于探针和诊断，可不带 Key。

**探针分工**：负载均衡、Docker、systemd 和部署验收必须检查 `/ready`；只有进程监管需要区分“进程存在但依赖未就绪”时才单独检查 `/live`。`/health` 为 Agent 版本协调保留兼容结构，即使依赖异常也返回可诊断快照，不能据其 `status=ok` 判定可接收生产流量。

**Key 分级（管理员 vs 用户）**：
- **管理员 Key**（`.env` 的 `API_KEY`，向后兼容，亦可用 `ADMIN_API_KEY`）：完整权限。
- **访客 Key**（`.env` 的 `USER_API_KEY`，可选）：可查看/选股/读情绪/查看回测结果，但**不能**修改配置、运行/查看预计算诊断，也不能搜索、读取或修改用户自选及量化盯盘。管理员专属功能以 `/whoami.admin_only` 为准，包括 `portfolio_stock_search`、`portfolio_get`、`portfolio_upload` 和全部 `quant_watch_*` 功能。
- 未输入 token 时，Web 界面按访客隐藏管理员入口；若服务未配置任何 Key，服务端允许只读访问。若服务已配置管理员 Key，未授权请求仍返回 `401`；管理员操作必须使用 `API_KEY` 或 `ADMIN_API_KEY`。
- **访客 Key 动态管理**（仅管理员，独立表 `user_api_keys`）：数据库只保存高熵 Key 的 SHA-256 摘要、展示前缀、名称和状态；旧版 `config_kv.user_api_keys` 明文会在启动时迁移后删除。列表只返回掩码，完整 Key 仅创建响应返回一次：
  - `GET /admin/user-keys`：列出名称、掩码、创建时间和启停状态
  - `POST /admin/user-keys` `{label}`：生成新访客 Key；必须立即安全保存本次响应中的完整值
  - `POST /admin/user-keys/toggle` `{id}`：原子启用/停用
  - `POST /admin/user-keys/delete` `{id}`：原子删除并立即失效
  动态访客 Key 与 `.env` 的 `USER_API_KEY` 均视为用户角色（可并存）。

- `GET /health` 保持现有顶层结构。其他已连通的业务 JSON API 响应，无论成功还是失败，均保留原状态码并新增 `health` 对象；其字段与 `/health` 同口径，顶层 `data_version` 和响应头 `X-Data-Version` 继续保留以兼容旧客户端。
- Agent 收到任一业务 JSON 响应后必须先消费 `health`、完成当前权限允许的五轨版本协调，再处理业务数据或错误；同一目标版本元组每个任务只触发一次。旧服务缺少 `health` 时整条请求链最多补调一次 `/health`。
- 静态文件、根路径重定向、FastAPI 文档和 OpenAPI 文件不属于业务 JSON 封装范围。

调用返回结构（下列日期、数量和版本号均为占位示例，实际值以每次响应为准）：
```json
{
  "ok": true,
  "function": "market_limit",
  "fetched_at": "YYYY-MM-DD HH:mm:ss",
  "data": { "source": "<实际数据源>", "fetched_at": "...", "rows": [] },
  "data_version": "v<schema>.<hash>",
  "health": {
    "status": "ok",
    "date": "YYYYMMDD",
    "trade_open": true,
    "tushare_ready": true,
    "db_ready": true,
    "portfolio_version": "<动态版本>",
    "selection_tag_version": "<动态版本>",
    "functions": 0,
    "agent_doc_version": "vMAJOR.MINOR.PATCH",
    "git_revision": "<短提交号>",
    "data_version": "v<schema>.<hash>"
  }
}
```
其中 `functions: 0` 仅表示该字段是运行时整数占位，并非实际功能数量。
错误返回同样带 `data_version` 和 `health`，例如：`{"ok":false,"error":"...","status":422,"details":[...],"data_version":"...","health":{...}}`。服务端统一覆盖业务 400/401/402/403/503、路由 404、方法 405、请求校验 422 和未捕获 500；健康快照探测失败不得覆盖原业务响应。

## 2. 版本机制（智能体必须遵守）

- `data_version` 由「功能索引内容」自动哈希生成。**新增功能或修改参数/描述都会改变版本号**。
- 每个响应（含响应头 `X-Data-Version`）都带 `data_version`。
- 智能体流程：
  1. 初始化：`GET /health` 与 `GET /functions` 建立五轨版本及功能索引基线。
  2. 每次业务 JSON 响应先读取 `health`，按文档、部署、功能、标签、持仓五轨协调；同一目标版本元组本任务只处理一次。
  3. `data_version` 不一致时重新 `GET /functions`；标签版本变化时刷新标签目录；持仓版本按任务需要同步；文档版本按 `agent/init.md` 升级。完成当前权限允许的协调后再处理本次业务结果。
  4. 旧服务缺少 `health` 时整条请求链最多补调一次 `/health`；401/403 阻塞的刷新写有时效短期事项，不得声称同步完成。
- 这样即使服务端新增功能、更新文档或改变独立业务契约，智能体也能自动感知并按职责刷新，无需把版本状态写入主 MEMORY。

### 2之一. 独立业务契约版本

- `portfolio_version` 是关注与持仓当前内容的独立版本，只保存在 `关注与持仓.md`。版本变化时调用 `portfolio_get`，以同一响应的 rows 和版本全量覆盖镜像；重复上传相同内容不升版。
- `selection_tag_version`、`data_version`、`agent_doc_version`、`git_revision` 和功能/标签目录保存在 `服务状态与能力.md`。标签版本变化时调用 `selection_tag_catalog` 全量刷新目录。
- 标签合集不是封闭枚举：固定属性优先复用目录，板块、题材、产品和事件标签允许 Agent 基于证据精炼。
- 五轨版本分别保存和比较，不得混用；任何版本、功能清单都不得写入主 `MEMORY.md`。

## 2 之二. Agent 文档版本对齐（`agent_doc_version` + `git_revision`）

`/health` 除 `data_version`（功能索引版本）外，另回传两个字段用于 **agent 文档与线上服务对齐**：

- `agent_doc_version`：agent 文档语义版本（服务端从 `agent/init.md` 顶部 `AGENT_DOC_VERSION` 解析）。**决定是否/如何更新文档**。
- `git_revision`：本次部署的 git commit 短 sha（部署脚本写入根目录 `VERSION` 文件，服务端优先读取，回退 `git rev-parse`）。**用于按版本精确拉取文档内容**。

Agent 侧逻辑（详见 `agent/init.md`「文档版本与同步」）：
1. 常驻期间在每次对话/任务开场比对 `/health` 的 `agent_doc_version` 与记忆值；
2. 落后时按 `profile/CHANGELOG-AGENT.md` 的变更文件清单**只重读变动文件**（增量，省 token）；
3. 按目标 `git_revision` 取文件内容——**本地优先**（`git show <rev>:<path>`），本机关机时**回退 GitHub raw**（公开仓库，无需 token）；
4. 仅 `git_revision` 变而 `agent_doc_version` 未变时，不重读文档。

> 前后端一致性由「同仓库同 commit 部署 + 重启」保证；agent 侧为会话级最终一致（每次开场对齐）。

## 3. 功能分组

调用 `GET /functions` 获取权威清单（含每个功能的 `params` 定义）。分组：
`market / money / fundamental / macro / price_hike / overseas / hot / sector / meta / screening / research / review`。

常用：
- 行情：`market_index` `market_realtime`（支持 `codes` 与 `name`/`names` 混合批量）`market_daily` `market_adj_daily` `market_limit` `market_lianban`
- 标的解析：`meta_stock_basic`（`codes` 精确过滤，`name`/`names` 名称包含匹配；返回 `missing_codes`/`missing_names`）
- 板块：`sector_dc` `sector_sw_daily` `sector_index_classify`
- 资金：`money_hsgt` `money_toplist` `money_flow_ind`
- 涨价/宏观：`price_hike_scan` `macro_ppi` `macro_cpi` `macro_pmi`
- 选股/手动盯盘/预计算：`screen_trend` `screen_quant` `screen_sector`（优先读取每日行业评分）`watch_intraday`（仅用户明确请求时单轮调用，禁止定时/循环）`precompute_daily_factors`（盘后同时计算个股因子与行业评分并做覆盖校验）`precompute_status`（覆盖率/失败日期/因子版本）
- 服务端量化盯盘（仅管理员）：`quant_watch_status {limit?, trade_date?}` 读取聚合结果；`trade_date` 接受 `YYYYMMDD` 或 `YYYY-MM-DD`，省略时返回不晚于当前上海日期的最近有数据日。响应区分 `requested_trade_date`、`effective_trade_date`、`current_trade_date`、`is_historical`，并返回 `available_trade_dates`。聚合消息保留最近 30 个自然日，原始分钟快照仍只在进程内存；历史查询只读，不触发扫描，WebSocket 只推“最新/实时”模式。`quant_watch_get_config` / `quant_watch_set_config` 管理设置，`quant_watch_scan_once` 仅用于连续竞价时的管理员单次诊断。
- 自选管理（仅管理员）：`portfolio_stock_search`（按名称或代码片段模糊搜索，添加前必须选择结果）`portfolio_get`（获取当前关注/持仓及版本）`portfolio_upload`（同代码取最新；持仓必填成本和整数手数；`deleted=true` 移除）
- 选股标签/持久化/看板：`selection_tag_catalog`（版本化固定标签及中文说明）`log_selection`（保存按日正式选股/观察历史快照）`selection_dashboard`（查询选股并刷新可核验行情与市场标签）
  - **当前自选实时并入（管理员）**：`selection_dashboard` 在 `category=watch/holding` 或不限类别时，实时合并当前自选 `portfolio_items`。这些行标注 `live_portfolio=true`、`category=watch/holding`、`id="pf-<代码>"`、带「当前自选」标签，`primary_theme=当前自选`；始终展示、不受 `date_from/date_to` 限制，不写入 `selections`、不参与回测；持仓以真实成本 `cost_price` 作为“选股后”涨跌基准。访客范围不返回这些行（沿用 `selection_read_scope` 敏感类别隔离）。
  - 正式 `auto/manual` 上传字段：完整代码、同日真实 `screening_run_id`、`selected_at`、精炼 `core_event`、精炼 `reason`、`tags` 和类别；评分、排名、因子契约及依赖从筛选运行固化，调用方不得覆盖。
  - 标签顺序为“主板块/题材 → 细分方向 → 具体事件 → 固定属性”；服务端自动补最新价及同日可核验的涨停/跌停标签，取数失败保留为空并返回错误，不推断。
  - 看板无日期参数时默认目标交易日及其之前三个交易日；仅日期筛选按题材聚合，带题材/标签筛选按日期聚合，全局排序取消聚合；聚合块内依次为龙头、核心、其余按评分。
- 因子配置：`get_factor_config` `set_factor_weights`（提交全部因子权重，缺失/多余/和≠1 报错并指引）。
  个股(stock)模型含 8 个默认启用因子（新增 `industry_strength`：同交易日申万一级行业评分分位）+ 7 个默认 0 权重候选因子（mom_6_1/max_lottery/downside_vol/amihud_illiq/small_size/value_bm/earnings_yield，源自学术/机构常用）；screen_quant 仅返回权重≠0 的因子列
- 配置留痕/版本：`set_factor_weights` 与 `set_sentiment_config` 支持传 `actor`（署名）+ `reason`，每次成功修改生成类 commit 的 `version_id` 并落库留痕（config_versions 表，含 parent/payload）。
  - `get_config_history {config_key|model, limit}`：查配置变更历史（倒序）
  - `get_config_version {version_id}`：按版本号定位当时完整权重快照
  - `restore_config_version {version_id, actor, reason}`（管理员）：回滚到历史版本（回滚亦留痕为新版本）
  - config_key 约定：`factor_weights:<stock|sector|trend|sentiment>`、`sentiment_window`
- 情绪温度：`sentiment_temperature`（0-100，11 项指标；含大盘/平均股价指数振幅方向+实体长度，指标权重 model=sentiment 可配置）
- 择时：`market_timing`（连续冰点/高热、出手买入权重提示）
- 竞价分析：`bidding_analysis`（09:25 竞价数据 + 竞价成交额 TopN + 异常高开/竞价爆量；仅用户明确请求时单轮调用，禁止定时或自动衔接）
- 投研：`research_build`
- 回测：`log_selection`(category=auto/manual/watch/holding) `selection_dashboard` `log_prediction` `selection_backtest` `predictions_backtest`

## 持久化（DB）

- 动态访客 Key 落独立 `user_api_keys` 表，只保存 SHA-256 摘要与展示前缀；列表不可恢复明文，启停/删除为单行原子操作。
- 因子契约与对应筛选运行通过同一事务固化；任一步失败整组回滚，避免正式候选证据链不完整。
- 当前关注与持仓落库 `portfolio_items`，股票代码唯一；持仓保存 `cost_price` 与 `lots`。`portfolio_meta` 保存单调 revision 和当前内容哈希，供 `/health.portfolio_version` 与 Agent 同步。
- 选股/预判/前向收益/回测快照/每日个股因子/每日行业评分继续落原有表（DDL 见 `service/db/schema.sql`）。
- `daily_sector_scores` 保存申万一级行业的原始因子、综合分与横截面分位；`daily_factors.factors.industry_strength` 保存个股所属行业当日分位，`_meta` 保存行业代码/名称/原始分。
- `DB_URL` 未设=本地 SQLite（`DATA_DIR/stock_agent.db`，随卷持久化）；上云设为 RDS MySQL。
- 量化盯盘聚合消息落库 `quant_watch_messages`，按交易日期和扫描时间查询，保留最近 30 个自然日；清理只删除截止日前记录，不重置当前调度状态。数据库不保存全市场原始分钟快照，服务重启后可恢复聚合历史，但不能重算已丢失的盘中采样序列。
- `portfolio_upload` 在单批次按代码去重并取最后一项，数据库再按代码 upsert；相同内容为 no-op，不升级 `portfolio_version`。`selections` 的 watch/holding 只保留历史快照兼容，不作为当前持仓事实源。当前自选事实源始终是 `portfolio_items`；`selection_dashboard` 展示层实时读取 `portfolio_items` 并入看板（见上「当前自选实时并入」），二者仍是各自独立的表与版本（`portfolio_version` vs 选股记录）。
- `log_selection` 按 (日期,代码,category)、`log_prediction` 按 (日期,标的,方向) **幂等去重**；只有 auto 样本进入自动调参。
- `log_selection` 重复上传返回 HTTP 200，`logged=true`、`inserted=false`、`duplicate=true`；`record` 保持首次固化记录，`current_quote` 明确表示本次重试刷新行情。历史版本曾在写库成功后因 `Decimal`/日期类型直接交给 JSON 响应而偶发 HTTP 500，现统一经标准编码后返回，避免“已写入但响应 500”。

## 4. 错误码

| status | 含义 | 处理 |
|---|---|---|
| 400 | 参数错误 / 未知功能 | 校验 function 与 params；必要时先刷新 `/functions` |
| 401 | 鉴权失败 | 仍先消费响应 `health`，再检查 X-API-Key；不要盲目重试 |
| 402 | tushare 积分/权限不足 | 跳过该功能或改用已允许的真实数据路径，不得编造 |
| 403 | 权限不足（用户 Key 调用管理员专属功能） | 仍先消费响应 `health`；改用管理员 Key。被阻塞的同步写有时效短期事项 |
| 404 | 业务资源或路由不存在 | 区分业务对象不存在与 URL 错误；必要时刷新接口文档/功能索引 |
| 405 | HTTP 方法错误 | 按端点契约改用正确方法 |
| 422 | 请求体、路径或查询参数校验失败 | 根据 `details` 修正字段类型、必填项或结构，不盲目重试 |
| 500 | 未捕获服务异常 | 保留原任务上下文和时间，披露服务内部错误；不得依据缺失数据继续推断 |
| 503 | 服务未启动或运行依赖不可用 | 提示启动/检查本地 Docker；若服务已返回响应仍先消费其中 `health` |

> 上述业务 JSON 错误均保留原 HTTP 状态码，并携带顶层 `data_version`、响应头 `X-Data-Version` 和嵌套 `health`；错误处理必须后于版本协调。静态文件、重定向、文档和 OpenAPI 不适用该封装。

## 5. 扩展方式（开发者）

新增一个数据/分析功能：
1. 在 `agent/skills/<某skill>/scripts/` 下新增或编辑模块。
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

Web 面板（与服务同源）：浏览器打开 `http://localhost:18901/ui/`，设置里填写访问凭据。业务日期统一使用 `/health` 的上海服务日期和市场阶段；健康快照暂缺时才按 `Asia/Shanghai` 计算，不使用浏览器本地时区决定交易日。只有上午/下午连续竞价允许行业盘中模式，其他阶段自动展示最近完整交易日。

探针验证：

```bash
curl -fsS http://localhost:18901/live
curl -fsS http://localhost:18901/ready
curl -fsS http://localhost:18901/health
```

## 7. v1.1.0 Agent 接口契约与统一 fallback

### market_index

- `code` 参数允许代码数组或逗号分隔字符串；Agent 必须保存请求 code 集并核对返回完整性。
- 4xx/5xx、空数据或部分 code 缺失时，对失败/缺失 code 逐个调用 `market_daily(code,start,end)`，取区间最近记录。
- 降级结果必须标 `degraded=true`、原失败接口/状态、实际 `trade_date`、仍缺失 code；不得将最近历史记录表述为实时或请求日数据。

### 资讯类（新闻 / 公告 / 外盘）——不在数据服务

- 当前 token 无 `news`/`anns_d`/`cctv_news`/`us_daily` 权限，服务已**移除** `news_flash`/`news_filter`/`news_anns`/`news_cctv`/`overseas_us`（及不可用的 `hot_kpl_concept`）。
- 新闻快讯、时政/新闻联播、公司公告、美股/大宗商品外盘一律由 Agent 从**各财经平台多源获取**（≥2 个可信来源交叉验证，标名称/URL/出处与时间，区分事实与传闻）。
- 全部资讯来源失败 → 标“资讯面不可用 + 已尝试来源”，不可解释为无消息、无利空或无风险。

### 降级二分（强制）

- **数据类接口禁止降级——失败则失败**：行情/资金/财务/宏观/板块/热榜/龙虎榜/涨跌停/情绪等结构化数据遇失败/空数据只能如实披露（标接口/状态/时间/实际数据日期/缺失项），禁止编造兜底；唯一允许同类**数据接口等价回退**（如 `market_index`→`market_daily` 取最近记录并标 `degraded`）。
- **资讯类允许多源外部获取**（见上）。

### 定时任务重试

T1/T3 关键数据接口 4xx/5xx/空数据：记录首次失败，首次失败后 5 分钟重试一次、15 分钟再重试一次（v2.7.0 已取消 T2 17:30 当日总结）。401 鉴权及明确参数/配置错误不盲目重试。最终仍失败时标降级与缺失并继续可完成部分；非关键接口失败不阻塞整份报告，禁止编造。竞价与盘中能力仅在用户明确请求时单次调用，不得注册自动任务或因失败重试而启动持续盯盘。

## 8. v1.1.0 情绪接口速览

- `sentiment_temperature`：返回 `temperature`、`level`、`indicators`、`weights`、`window_size`；指标/权重以实时返回为准。
- `sentiment_extreme_index`：参数 `date`、`days`；极端指数固定按含当日最近 7 个交易日归一，平均振幅与成交额缩量子分各 50%，返回 `extreme_index/components/recent/selection_bias`。Agent 只消费接口结果，不自行复算或调整固定规则。
- `market_lianban` + `market_limit`：用于连板梯队、连板个股、断板及 1-3 日反包分析。极端指数 ≥80 强倾向、60-80 适度倾向、<60 不额外倾斜；这只是风格优先级，不能替代四维逻辑与风险过滤。

## 9. schema v2：因子、筛选、回测与预判证据链

- `schema_version=2` 表示正式选股与预测回测存在不兼容旧调用的强门禁。`data_version` 仍由功能索引自动哈希；二者必须同时检查。
- 因子契约分为：公式版本、完整成分（含权重0候选）、结构哈希、权重版本/权重哈希和上游依赖指纹。个股上游依赖包含行业评分公式与权重、申万历史成分区间口径、目标日股票池口径。
- 预计算可用条件为：日级 `status=success`、覆盖率达标、公式版本/结构哈希/依赖指纹一致，且因子行/行业行与质量记录 `run_id` 相同。`partial|failed|legacy(NULL)` 不可读；部分重算不会覆盖同契约既有成功快照。
- `screen_quant`/`screen_trend` 固化 `screening_run_id`、完整运行契约、参数、候选集合、排名、`score_raw` 和0~1 `score_percentile`。`log_selection(category=auto|manual)` 必须引用该运行，服务端不信任调用方自填分数。
- `selection_backtest` 使用版本化前向收益表，按 SSE 统一交易日、qfq 前复权及同日沪深300基准计算；默认保存快照。自动调参需 `optimization_gate.eligible=true`，并向 `set_factor_weights` 提交 `backtest_snapshot_id`、`expected_parent_version` 和全部因子。
- `log_prediction` 固化上海时间预测时刻及下一 SSE 目标交易日，同一预判日期+标的不可变且反向冲突拒绝。`predictions_backtest` 只评估成熟目标日，默认保存快照，并显式返回未成熟、行情失败及 `legacy_unverifiable` 数量。
- 兼容迁移不删除旧表、不伪造旧版本；旧列补为 NULL，只有新口径记录可进入当前契约筛选与优化。
## 运行期服务记忆归属（v2.0.0）

- `服务状态与能力.md`：BASE_URL、health 状态（不含持仓版本）、`data_version`、`selection_tag_version`、`agent_doc_version`、`git_revision`、文档同步记录、功能索引、标签和接口约定。
- `关注与持仓.md`：BASE_URL、`portfolio_version` 和 `portfolio_get` 全量 rows 镜像。
- `MEMORY.md`：不得保存版本、接口清单、业务响应或故障待办。
- `短期记忆/`：接口失败、待同步和临时处理事项；必须有时效与删除条件，解决后立即删除。
- `/health` 的 `db_ready=false` 或 `portfolio_version=unavailable` 时不得覆盖持仓镜像；只能创建短期待同步事项。
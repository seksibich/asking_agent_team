# 短线盯盘 & 投研 Agent 初始化工具包

一套通用的「金融 + 时事 + 行业分析」型短线盯盘 + 投研智能体初始化工具包（与具体平台无关）。
智能体读取 `init.md` 完成自我初始化：加载人格、建立记忆、连通数据服务、注册定时任务、明确输出规范。

## 核心定位

覆盖近期热门与趋势股、行业逻辑挖掘、时事驱动；不局限于连板打板。

## 分析重心（贯穿全部技能与脚本）

> **涨价 > 逻辑 > 预期炒作 > 情绪**

- 涨价（第一优先）：真实涨价/供需反转，允许自主到行业平台/期货取数并 ≥2 来源交叉验证
- 逻辑：产业链/景气拐点，**以预期驱动为主，不以过往业绩为主**（业绩披露期除外）
- 预期炒作：政策/事件/题材催化，评估兑现概率
- 情绪：连板/涨停/活跃度，仅作节奏与仓位
- PE/PB 权重为 0，仅作风险提示中的过往估值背景

## 目录结构

```
stock_agent_kit/
├── init.md                 # ★ 自我初始化入口
├── index.md                # ★ 强制阅读：技能索引/时机/输出规范
├── SOUL.md                 # 人格
├── schedule.md             # 定时任务清单
├── README.md
├── agents/                 # Agent 团队（主 + 5 子 Agent 角色描述）
│   ├── TEAM.md
│   ├── main-orchestrator.md
│   ├── technical-trend-analyst.md
│   ├── sentiment-analyst.md
│   ├── fundamental-research-analyst.md
│   ├── macro-news-analyst.md
│   └── backtest-analyst.md
├── memory/                 # 记忆指引 + 模板（含服务端版本号/功能索引、关注持仓、每日观察对象）
│   ├── MEMORY.md
│   └── templates/
├── skills/                 # 所有技能 + 对应脚本
│   ├── <skill>/SKILL.md
│   └── <skill>/scripts/*.py   （服务端功能模块，随镜像加载）
├── Dockerfile docker-compose.yml docker-compose.override.yml   # 部署入口（根目录）
├── requirements.txt .env.example                               # 依赖 / 配置模板
├── DEPLOY.md                # 部署说明（Docker / 直接部署）
├── service/                # 后端服务
│   ├── app.py registry.py loader.py common.py db.py cli.py
│   ├── AGENT_SERVICE_GUIDE.md
│   └── db/                 # schema.sql / PERSISTENCE.md / PRECOMPUTE_PLAN.md
└── web/                    # Web 面板（与服务同源，/ui/）
    ├── index.html style.css app.js
```
> 运行数据 `data/`（DB/SQLite）与缓存 `cache/` 在根目录生成（已 gitignore）。

## 架构

```
┌───────────────────────────────────────────┐
│  智能体（读取 init.md / index.md 自我初始化）│
│  SOUL + skills + memory + 定时任务          │
└───────────────┬───────────────────────────┘
                │ HTTP  POST /call {function, params}   (X-API-Key)
                │       GET /functions / GET /health    (data_version)
                ▼
┌───────────────────────────────────────────┐
│  本地数据服务（Docker, FastAPI）            │
│  registry 自动发现 skills/*/scripts 功能    │
│  版本号 = 功能索引哈希（新增功能自动变化）   │
└───────────────┬───────────────────────────┘
                ▼  tushare pro（15000 积分档）
```

## 快速开始（详见 [DEPLOY.md](DEPLOY.md)，支持 Docker / 直接部署）

前后端与数据库一体，**仓库根目录即单一部署目录**。

```bash
# 1) 配置密钥（.env 不入库，从模板复制）
cp .env.example .env
#   TUSHARE_TOKEN=<你的 tushare token>
#   API_KEY=<强随机字符串，用作 X-API-Key>
#   DB_URL=            # 留空=本地 SQLite；上云填 RDS MySQL 连接串

# 2) 一键启动（在仓库根目录，含 Web 面板）
docker compose up -d --build
curl -H "X-API-Key: <你的API_KEY>" http://localhost:18901/health

# 3) Web 面板：浏览器打开 http://localhost:18901/ui/ ，右上角设置填入 API_KEY
```
`/health` 返回 `tushare_ready` / `db_ready` / `trade_open` 供自检。直接部署（无 Docker）见 DEPLOY.md。
3. 把 `init.md` 作为初始化指令交给智能体，它会按索引完成自我初始化。
4. **Web 面板**（与服务同源部署）：浏览器打开 `http://localhost:18901/ui/`，右上角设置里填入 `X-API-Key`。
   - 量化选股：调 `screen_quant` 看结果
   - 情绪温度：调 `sentiment_temperature` 看 0-100 温度与指标分解
   - 权重配置：查看/微调 stock/sector/trend/sentiment 各模型权重（`get_factor_config`/`set_factor_weights`）

> 目前先在本地 Docker 验证；后续迁移阿里云 ECS 时，把服务部署到 ECS 并将基址改为 ECS 地址即可（协议不变）。

## 关键机制

- **统一调用**：所有数据/分析走 `POST /call {function, params}`。
- **版本自感知**：每个响应带 `data_version`；智能体对比后不一致就重拉 `/functions`，自动获知新增能力。
- **可扩展**：在 `skills/*/scripts/` 用 `@register` 加一个函数即自动进索引并改版本，无需改初始化提示词。详见 `service/AGENT_SERVICE_GUIDE.md`。
- **选股回测闭环**：自动选股经 `log_selection`(category=auto) 登记，`selection_backtest` 跟踪 1/3/7/30 日收益与超额并给出调参建议。
- **因子可配置**：`get_factor_config` / `set_factor_weights`（提交全部因子权重，缺失/多余/差异/和≠1 会被拒并指引修正）实现回测→调参闭环。
- **Agent 团队**：主 Agent + 5 子 Agent；重量级任务（盘前/复盘/周月回测/用户分析）启用团队，盯盘主 Agent 单跑。见 `agents/TEAM.md`。

## 用户主动研究入口

| 用户需求 | 入口 Skill | 输出目录 | 默认持久化 |
|---|---|---|---|
| 指定事件、行业导向或热门板块选股 | `industry-analysis` + `stock-screening` + `quant-screening` | `投研/yyyyMMdd-{主题}选股/` | `ephemeral` |
| 主动调研单只股票 | `stock-research` | `投研/yyyyMMdd-{股票名}个股调研/` | `ephemeral` |

两类入口都先动态识别“题材/具体事件 → 行业产业链 → 个股”，题材不限申万、东财或同花顺分类；报告标题后首节固定为“一眼结论（核心摘要）”，先给仓位/次日倾向、事件 Top N、事件映射个股和最大风险/证伪。仅用户明确要求「加入观察/持续跟踪/纳入后续回测」时才转 `category=watch`，跟踪 1/3/7/30 日收益；watch 与 auto 完全隔离，不参与自动胜率、`tuning_hints` 或调参。`category=auto` 仅限调度器正式自动候选。

## 安全

- `.env` 含真实 token，已 gitignore；务必设置强随机 `API_KEY`。
- **API Key 分级**：`API_KEY` 为管理员 Key（完整权限）；可选配置 `USER_API_KEY` 作为只读用户 Key —— 用户 Key 可查看/选股/读情绪，但**不能修改因子权重、归一化窗口，也不能触发回测**（服务端返回 403，Web 面板对应入口自动禁用）。留空 `USER_API_KEY` 则不启用。
- 本地/内网使用；迁移公网（ECS）时建议加防火墙白名单或反向代理鉴权。

## Agent→Skill 强制绑定（AGENT_DOC_VERSION v1.2.0）

首次初始化、每次任务启动以及每个 Agent/角色启动时，必须完整读取全部 12 个 `SKILL.md`：`priority-framework`、`data-service`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research`。`agents/TEAM.md` 的角色主绑定只决定主职责，不允许只凭 `index.md`、矩阵或角色摘要执行。`stock-research` 是用户主动单股调研入口，不加入定时 T1/T6/T7 的必执行绑定。

- 面向用户的报告必须使用通俗中文：首屏和正文不得堆砌英文接口名、参数名、JSON 字段或因子代码；技术名称只在数据来源附录、故障诊断或用户明确要求时保留，并同时给出中文解释。

## 接口契约与统一 fallback 速览

- `market_index.code` 接受数组或逗号分隔字符串；4xx/5xx、空数据或部分 code 缺失时，按 code 逐个调用 `market_daily(code,start,end)` 取最近记录，必须标 `degraded` 与实际 `trade_date`。
- `news_flash` 402：`news_filter(keyword)+news_cctv+外部搜索`；若 filter 同源失败，继续 CCTV + 至少两个可信外部来源；全部失败标“消息面不可用”，不得推断无风险。
- T1/T6/T7 关键接口失败：记录后延迟 5 分钟、15 分钟各重试一次；401/明确参数或配置错误不盲目重试；最终失败按 fallback 降级并继续可完成部分，非关键接口不阻塞报告，禁止编造。

## v1.1.0 情绪接口速览

- `sentiment_temperature`：0-100 情绪温度、动态指标与权重，以接口返回为准。
- `sentiment_extreme_index`：0-100 情绪极端指数，固定最近 7 个交易日归一，振幅/缩量各 50%，Agent 不得自行复算或配置；返回 `components`、`recent`、`selection_bias`。
- `market_lianban` + `market_limit`：分析连板生态、连板个股、断板与 1-3 日反包候选。极端指数高仅提高分析优先级，最终仍由 `skills/priority-framework/SKILL.md` 复核。
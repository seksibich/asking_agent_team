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
├── service/                # 数据服务工程（Docker）
│   ├── app.py registry.py loader.py common.py cli.py
│   ├── AGENT_SERVICE_GUIDE.md
│   ├── Dockerfile docker-compose.yml requirements.txt
│   └── .env / .env.example
└── web/                    # Web 面板（与服务同源，/ui/）
    ├── index.html style.css app.js
```

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

## 快速开始

1. 配置 `service/.env`（`TUSHARE_TOKEN` 已填入；把 `API_KEY` 改成强随机值）。
2. 启动本地数据服务：
   ```bash
   cd service
   docker compose up -d --build
   curl -H "X-API-Key: <你的API_KEY>" http://localhost:18901/health
   curl -H "X-API-Key: <你的API_KEY>" http://localhost:18901/functions
   ```
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

## 安全

- `service/.env` 含真实 token，已 gitignore；务必设置强随机 `API_KEY`。
- 本地/内网使用；迁移公网（ECS）时建议加防火墙白名单或反向代理鉴权。

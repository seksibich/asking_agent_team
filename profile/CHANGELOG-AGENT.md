# Agent 文档变更日志（CHANGELOG-AGENT）

> 本文件记录所有 **agent 相关文档/规范**（`agent/init.md` / `agent/index.md` / `agent/schedule.md` / `agent/SOUL.md` /
> `agent/memory/**` / `agent/agents/**` / `agent/skills/**/SKILL.md` / `doc/AGENT_SERVICE_GUIDE.md` 等）的版本化变更。
> 数据服务**功能索引**的变更由服务端 `data_version` 机制单独管理（见 `agent/index.md`），二者互不替代。
>
> **路径说明**：v1.3.0 起工程已重组——agent 相关内容统一在 `agent/`，交叉文档在 `doc/`，配置与本变更日志在 `profile/`，前端并入 `service/web/`。**v1.2.0 及更早的历史条目中的相对路径（如 `skills/...`、`agents/...`、`service/AGENT_SERVICE_GUIDE.md`）为当时目录结构，重组后对应 `agent/skills/...`、`agent/agents/...`、`doc/AGENT_SERVICE_GUIDE.md`。**

## 版本号规则

- 采用 `vMAJOR.MINOR.PATCH`（语义化）：
  - MAJOR：流程/铁律/编排的重大重构（agent 行为可能不兼容旧记忆）
  - MINOR：新增技能、新增规范章节、输出目录规则调整等
  - PATCH：措辞订正、表格补充等不改变行为的小修
- **每次调整 agent 文档，必须同时：**
  1. 在本文件**顶部新增一条版本记录**（版本号递增、日期、提交号、摘要、变更文件清单、agent 需执行的动作）；
  2. 更新 `init.md` 顶部的 `AGENT_DOC_VERSION` 为该新版本号。

## Agent 同步流程（如何用本日志）

每次把 `init.md` 提交给 agent 时，agent 按 init.md「文档版本与同步」一节执行：

1. 读取 `init.md` 的 `AGENT_DOC_VERSION`（= 目标版本）。
2. 读取自身记忆 `agent记忆/service_state.json` 的 `agent_doc_version`（= 已内化版本；缺失视为首次）。
3. 若两者一致 → 无需变更，继续正常初始化/任务。
4. 若目标版本更高 → 打开本文件，**按顺序处理所有「> 已内化版本」且「≤ 目标版本」的条目**：
   - 逐条重读该版本「变更文件清单」中列出的文件，重新内化其规则；
   - 若条目「agent 动作」要求更新记忆/模板，照做。
5. 全部补齐后，把记忆 `agent_doc_version` 更新为目标版本，并在初始化回执中报告「文档版本：vX.Y.Z（已从 vA.B.C 同步）」。

> 首次初始化（无 `agent_doc_version`）：全量内化当前 init.md 指向的所有文档，记 `agent_doc_version` = 当前 `AGENT_DOC_VERSION`。

---

## 版本记录（最新在上）

### v1.6.1 — 2026-07-16（输出规约人性化：首屏重心前置 + emoji 高亮）

- **摘要**：只润色面向用户报告的表达与信息重心，不改任何取数/筛选/回测/调参/持久化行为。统一「一眼结论（核心摘要）」首屏，把复盘总结、关键消息面、选股/持仓/关注结论前置，数据口径与来源清单靠后；为一眼结论四类关键信息（📊 仓位、🔥 题材/事件、🎯 个股/候选、⚠️ 风险）与主要章节标题引入统一 emoji 图标做视觉锚点。覆盖 T1/T6/T7、竞价、早盘、周报、月报模板。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（版本→v1.6.1；新增「v1.6.1 补充执行约束（输出规约人性化）」）
  - `agent/skills/output-format/SKILL.md`（报告通用结构信息重心铁律、首屏任务语义、T1/T6/T7 完整模板与推送模板加 emoji、新增 emoji 约定与信息重心前置两条硬约束）
  - `agent/skills/pre-market/SKILL.md`、`agent/skills/post-market/SKILL.md`（T1/T6/T7 首屏与章节 emoji 图标约定）
  - `agent/skills/review-learning/SKILL.md`（周报新增一眼结论首屏 + emoji，月报首屏复盘总结前置）
  - `agent/skills/bidding-analysis/SKILL.md`、`agent/skills/intraday-watch/SKILL.md`（竞价分析、早盘总结报告首屏与章节 emoji）
- **agent 动作**：
  1. 重读上述文件；此后所有面向用户报告首屏固定为 `## 🎯 一眼结论（核心摘要）`，按 📊 仓位 / 🔥 题材事件 / 🎯 个股候选 / ⚠️ 风险顺序先给结论，复盘总结、消息面、选股持仓关注结论前置。
  2. 一眼结论关键信息与主要章节标题按 output-format 的 emoji 约定加统一图标；同类信息全局同图标，不堆砌。
  3. 本版本不改变任何数据、筛选、回测、调参与持久化逻辑，无需刷新 `/functions` 或重算因子。

### v1.6.0 — 2026-07-16（因子契约闭环 + 无未来泄漏回测）

- **摘要**：建立可审计的量化筛选、正式选股、前向收益、预测回测和自动调参闭环。因子契约区分公式版本、完整成分（含权重0候选）、结构哈希、权重版本及上游行业/成分/股票池依赖；预计算结果按 `run_id` 强绑定并禁止部分重算覆盖既有成功快照；正式选股必须引用筛选运行；选股收益统一交易日与前复权；预判改为不可变的下一交易日口径；自动调参增加样本量、时序样本外、快照和父版本门禁。数据协议升级为 schema v2，旧数据保留为 legacy/NULL，不伪造版本。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（版本→v1.6.0；正式选股、预测回测和自动调参门禁）
  - `agent/schedule.md`、`agent/agents/main-orchestrator.md`（D1 成功口径、T7 预测目标日、筛选运行与调参证据）
  - `agent/skills/quant-screening/SKILL.md`、`agent/skills/stock-screening/SKILL.md`、`agent/skills/review-learning/SKILL.md`（因子契约、筛选运行、收益与预测回测、自优化门禁）
  - `agent/memory/MEMORY.md`（manual 分类、不可变预判与目标交易日审计）
  - `doc/AGENT_SERVICE_GUIDE.md`（schema v2、契约字段、预计算可用条件和回测协议）
  - 服务端（非 agent 文档，一并记录）：`service/db.py`、`service/db/schema.sql`、`service/registry.py`、`service/web/app.js`、`agent/skills/quant-screening/scripts/factor_contract.py`、`factor_config.py`、`factors.py`、`precompute.py`、`quant_screen.py`、`screen_sector.py`、`sentiment.py`、`agent/skills/stock-screening/scripts/screen_trend.py`、`agent/skills/review-learning/scripts/selection_backtest.py`、`predictions_backtest.py`
- **agent 动作**：
  1. 刷新 `/functions` 并确认 `schema_version=2`；D1 重新生成当前契约因子，旧 NULL/legacy 数据不得进入筛选。
  2. 正式候选先调用 `screen_quant`/`screen_trend`，登记时原样携带其 `screening_run_id`；统一使用 `score_percentile` 做跨样本分桶。
  3. 只有 `selection_backtest.optimization_gate.eligible=true` 才允许自动调参，并提交 `backtest_snapshot_id`、`expected_parent_version` 与全部因子权重。
  4. `log_prediction` 只登记下一 SSE 交易日预判；`predictions_backtest` 的 legacy、未成熟和失败样本必须披露，不得按同日行情补算。

### v1.5.0 — 2026-07-16（行业评分因子 + 正式选股持久化看板）

- **摘要**：完成量化链路与看板整套改造：①修复行业筛选污染，正式行业分类命中时不再混入宽泛概念，并新增个股名称多选且优先于行业条件；②新增每日行业量化评分持久化和行业分析 Tab，把行业横截面强度聚合进个股量化因子；③重构自动/用户触发正式选股链路，新增 `manual` 隔离类别、标准化选股快照与 `selection_dashboard` 看板；④预计算改为全服务唯一后台任务并持续暴露进度；⑤新增网页 favicon，访客彻底隐藏 Key 管理入口。自动样本与用户触发样本严格隔离。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（版本→v1.5.0；行业因子、正式选股链路、auto/manual/watch/holding 分类与看板约束）
  - `agent/index.md`、`agent/schedule.md`（输出目录、D1 行业评分预计算、选股持久化和回测分类）
  - `agent/skills/quant-screening/SKILL.md`、`agent/skills/stock-screening/SKILL.md`、`agent/skills/priority-framework/SKILL.md`（行业强度因子；热点→强势股→量化→理由链；正式候选快照字段）
  - `agent/skills/review-learning/SKILL.md`、`agent/skills/output-format/SKILL.md`（manual 隔离回测、selection_dashboard 与用户选股输出规则）
  - `agent/agents/main-orchestrator.md`、`agent/agents/ORCHESTRATION.md`、`agent/memory/MEMORY.md`（编排与记忆分类同步）
  - `doc/AGENT_SERVICE_GUIDE.md`（新增行业评分表、industry_strength、manual 与 selection_dashboard 协议）
  - 服务端/前端（非 agent 文档，一并记录）：`service/db.py`、`service/db/schema.sql`、`service/app.py`、`agent/skills/quant-screening/scripts/factors.py`、`precompute.py`、`screen_sector.py`、`quant_screen.py`、`agent/skills/stock-screening/scripts/screen_trend.py`、`agent/skills/review-learning/scripts/selection_backtest.py`、`service/web/index.html`、`service/web/app.js`、`service/web/style.css`、`service/web/favicon.svg`、`Dockerfile`、`.dockerignore`
- **agent 动作**：
  1. 重新读取本条列出的 Agent 文档；刷新 `/functions`，确认 `stock_names`（优先于 industries）、`industry_strength`、`selection_dashboard`、预计算任务状态与 `log_selection` 新参数。
  2. D1 盘后运行 `precompute_daily_factors`，确认同时写入个股与行业评分；旧 `stock-factors-v1` 结果不再供新选股读取。
  3. 每日自动正式候选登记 `auto`；用户明确触发的正式选股候选登记 `manual`。逐只保存选股价、热点、事件、短线地位、完整理由链和全部量化因子；只有 auto 可用于调参。
  4. 普通研究保持 ephemeral；用户要求持续跟踪时使用 watch，持仓使用 holding；通过 `selection_dashboard` 按日期/热点/类别核对记录与最近行情。

### v1.4.0 — 2026-07-15（health 版本对齐 + 增量文档更新）

- **摘要**：`/health` 新增 `agent_doc_version`（语义版本）与 `git_revision`（部署 commit）两个字段；agent 常驻运行，在每次对话/任务开场比对 `/health` 版本，发现 `agent_doc_version` 变高时按本变更日志的「变更文件清单」**只重读变动文件**（增量，省 token），并按目标 `git_revision` 取文档内容（本地优先、回退 GitHub raw 公开仓库免 token）。前后端一致由同仓库同 commit 部署保证，agent 侧为会话级最终一致。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（版本→v1.4.0，重写「文档版本与同步」为 git 对齐 + 增量 + 来源优先级，第 4/5/9 步与 v1.4.0 约束）
  - `agent/index.md`（开场检查清单第 1之二 / 第 4 步纳入 agent_doc_version/git_revision 与增量更新）
  - `agent/memory/MEMORY.md`、`agent/memory/templates/service_state.json`（新增 `git_revision` 字段与维护规则）
  - `doc/AGENT_SERVICE_GUIDE.md`（`/health` 字段 + 新增「Agent 文档版本对齐」小节）、`agent/skills/data-service/SKILL.md`（`/health` 字段说明）
  - 服务端（非 agent 文档，一并记录）：`service/version.py`（新增）、`service/app.py`（/health 加字段）、`service/loader.py`（排除 version 模块）、`deploy/remote_deploy.sh`（写 VERSION 文件）、`.gitignore`（忽略 /VERSION）
- **agent 动作**：
  1. 每次对话/任务开场调 `GET /health`，比对 `agent_doc_version` 与记忆；落后按本清单只重读变动文件（增量），按目标 `git_revision` 取内容（本地优先→GitHub raw）。
  2. 记忆 `service_state.json` 新增并维护 `git_revision`；仅 `git_revision` 变而 `agent_doc_version` 未变时不重读文档、只更新该字段。
  3. 本条起，每条版本记录都以「变更文件清单」为增量更新依据；改 agent 文档务必同步 bump `AGENT_DOC_VERSION` 与本清单。

### v1.3.0 — 2026-07-15（工程重组 + 接口可用性 + 降级二分）

- **摘要**：①工程目录按职责重组为 `agent/`（agent 全部内容）、`service/`（后端+前端 `service/web`+DB）、`doc/`（agent↔服务交叉文档与业务索引）、`profile/`（配置+变更日志）+ 根级全局文档；②审计数据服务接口可用性，剔除当前 token 不可用的 6 个接口（4 个 news + `overseas_us` + `hot_kpl_concept`），修复 `fundamental_forecast` 无参失败，功能数 65→59；③确立「数据类接口禁止降级、失败则失败；资讯类由外部财经平台多源获取」的降级二分，贯穿初始化/定时任务/子 Agent 编排，强调数据优先、结论优先。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（新增「路径约定」、v1.3.0 约束、版本号→v1.3.0）、`agent/index.md`（降级二分、路径与 T7 公告外部核验）、`agent/schedule.md`（T1/T4/T6/T7/P1 关键调用改可用接口 + 降级二分）
  - `agent/SOUL.md`（无改）、`agent/memory/MEMORY.md`（hot_kpl_list）
  - `agent/agents/TEAM.md`（无改）、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`、`agent/agents/macro-news-analyst.md`、`agent/agents/sentiment-analyst.md`、`agent/agents/fundamental-research-analyst.md`
  - 全部相关 `agent/skills/*/SKILL.md`：`data-service`（降级总则/资讯类外部获取/分组表）、`pre-market`、`post-market`、`intraday-watch`、`industry-analysis`、`quant-screening`、`stock-screening`、`stock-research`、`output-format`、`priority-framework`
  - `agent/skills/data-service/scripts/market_data.py`（移除 6 个不可用接口，修复 `fundamental_forecast` 默认 `ann_date`）
  - `service/loader.py`、`service/app.py`、`Dockerfile`、`docker-compose.yml`、`docker-compose.override.yml`、`.dockerignore`（适配新目录）
  - `doc/AGENT_SERVICE_GUIDE.md`（由 service/ 迁入 doc/，更新资讯类与降级二分）、`doc/SERVICE_INDEX.md`（新增，服务业务索引 + 交叉表）
  - `README.md`、`DEPLOY.md`（目录结构与路径更新）、`profile/CHANGELOG-AGENT.md`（本条 + 路径说明）
- **agent 动作**：
  1. 按 `agent/init.md`「路径约定」使用新目录；所有跨目录引用用仓库根相对路径。
  2. 不再调用已移除接口（4 个 news、`overseas_us`、`hot_kpl_concept`）；题材强度用 `hot_kpl_list`+`hot_dc/ths`+涨停连板聚合，外盘美股/大宗商品与新闻/公告走外部财经平台多源。
  3. 严格执行降级二分：数据类接口失败则失败、如实披露、禁止编造（仅允许同类数据接口等价回退）；资讯类多源外部获取并 ≥2 来源交叉。
  4. 数据版本因接口精简而变化（`v1.5bc938eb`→`v1.32fb4ac0`），调用后按机制刷新 `/functions` 与记忆。

### v1.2.0 — 2026-07-15（提交 b3f1927）

- **摘要**：新增 `stock-research` 单股调研 Skill、动态题材/具体事件驱动的报告首屏与双入口选股，建立 ephemeral/watch/auto 严格隔离；同时要求面向用户的报告使用通俗中文，避免用大量英文接口名、参数名和因子代码干扰阅读。
- **变更文件清单（agent 相关）**：
  - `CHANGELOG-AGENT.md`、`README.md`、`index.md`、`init.md`、`schedule.md`
  - `agents/TEAM.md`、`agents/ORCHESTRATION.md`、`agents/main-orchestrator.md`
  - `agents/technical-trend-analyst.md`、`agents/sentiment-analyst.md`、`agents/fundamental-research-analyst.md`、`agents/macro-news-analyst.md`、`agents/backtest-analyst.md`
  - `memory/MEMORY.md`、`memory/templates/关注与持仓.md`、`memory/templates/daily-观察对象.md`
  - 全部 12 个 `skills/*/SKILL.md`，其中新增 `skills/stock-research/SKILL.md`
- **agent 动作**：
  1. 重新逐文件完整加载固定 12 个 Skills；`stock-research` 仅作为用户主动单股调研入口，不加入 T1/T6/T7 定时必执行绑定。
  2. T1/T6/T7、趋势/量化选股、用户方向选股和单股调研统一按“一眼结论（核心摘要）→目录导读→详细正文”，按“题材/具体事件 → 个股”展示重点。
  3. 自动与用户主动选股均先做动态题材映射，再执行量化/趋势筛选；量化原始分先转当批横截面排名，四维硬门槛继续生效。
  4. 用户主动研究默认仅生成本次报告；只有用户明确要求时才加入观察，进行隔离的 1/3/7/30 日观察性回测，禁止进入自动胜率、调参提示和因子/情绪调优。
  5. T6/T7 动态题材调用链必须覆盖消息/公告、热榜、涨停连板、量能与资金，并遵守新闻降级链和关键接口 5/15 分钟重试。
  6. 面向用户的报告首屏、结论、正文和表格字段必须使用通俗中文；不得堆砌英文接口名、参数名、JSON 字段或因子代码。技术名称仅在数据来源附录、故障诊断或用户明确要求时保留，并紧邻中文解释。
  7. T7 业绩增长参考池继续与正式选股、记忆、回测和调参严格隔离。

### v1.1.0 — 2026-07-15（提交 48a2e57）

- **已提交基线（事实保留）**：提交 `48a2e57` 已落地情绪极端指数、连板与断板反包、强制 Agent→Skill 绑定、统一 fallback、T1/T6/T7 详尽报告 + 独立推送、正式候选理由链和隔离的业绩增长参考池。
- **版本边界**：本条只记录已由提交 `48a2e57` 落地的 v1.1.0 能力；动态题材、单股调研、持久化隔离和通俗中文报告归入上方 v1.2.0。
- **变更文件清单（agent 相关）**：
  - `CHANGELOG-AGENT.md`
  - `init.md`
  - `index.md`
  - `schedule.md`
  - `README.md`
  - `agents/TEAM.md`
  - `agents/ORCHESTRATION.md`
  - `agents/main-orchestrator.md`
  - `agents/technical-trend-analyst.md`
  - `agents/sentiment-analyst.md`
  - `agents/fundamental-research-analyst.md`
  - `agents/macro-news-analyst.md`
  - `agents/backtest-analyst.md`
  - `skills/priority-framework/SKILL.md`
  - `skills/data-service/SKILL.md`
  - `skills/output-format/SKILL.md`（T1/T6/T7 完整模板、独立推送模板、正式候选综合理由表、业绩增长参考池表及必含表格矩阵）
  - `skills/pre-market/SKILL.md`（T1 详尽正文、目录导读、推送分离与正式关注理由链）
  - `skills/bidding-analysis/SKILL.md`
  - `skills/intraday-watch/SKILL.md`
  - `skills/post-market/SKILL.md`（T6/T7 完整结构、业绩窗口流程、参考池隔离及重点说明）
  - `skills/industry-analysis/SKILL.md`
  - `skills/stock-screening/SKILL.md`（正式趋势候选完整理由链与标准表）
  - `skills/quant-screening/SKILL.md`（正式量化候选完整理由链与标准表）
  - `skills/review-learning/SKILL.md`（业绩增长参考池排除出回测样本与调参证据）
  - `service/AGENT_SERVICE_GUIDE.md`
  - `skills/data-service/scripts/market_data.py`（`market_index` 数组/字符串兼容、空数据回退；`market_daily` 指数日线回退）
  - `skills/quant-screening/scripts/sentiment.py`（既有 v1.1.0 情绪极端指数实现）
  - `web/index.html`、`web/app.js`、`web/style.css`（情绪温度置顶、设置弹窗、极端指数温度计）
- **agent 动作**：
  1. 重读本条变更文件并内化 v1.1.0 情绪极端指数、连板生态与断板反包能力。
  2. T1/T6/T7 执行详尽报告与独立精简推送，正式候选按完整理由链输出。
  3. 关键接口执行统一 fallback 与 5/15 分钟延迟重试，失败时披露降级状态并继续可完成部分。
  4. T7 业绩增长参考池与正式选股、记忆、回测和调参严格隔离。

### v1.0.0 — 2026-07-15（基线锚定，提交 3d822f7）

- **摘要**：以此提交为 agent 文档基线。包含：情绪温度因子重构（振幅/实体因子、移除 index_kline、权重改为运行时从接口拉取）、量化选股候选因子（7 个默认 0 权重）、行业多源交集匹配、API Key 分级与访客 Key 管理、配置版本留痕（署名微调 + 类 commit 版本号）、输出目录按触发来源路由、量化选股分组解读与自动 top_n=50。
- **变更文件清单（agent 相关）**：
  - `init.md`、`index.md`、`schedule.md`
  - `memory/MEMORY.md`
  - `agents/sentiment-analyst.md`
  - `skills/output-format/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/review-learning/SKILL.md`
  - `skills/pre-market/SKILL.md`、`skills/post-market/SKILL.md`、`skills/bidding-analysis/SKILL.md`、`skills/intraday-watch/SKILL.md`、`skills/data-service/SKILL.md`
  - `service/AGENT_SERVICE_GUIDE.md`
- **agent 动作**：作为基线**全量内化**上述文档；在记忆 `service_state.json` 写入 `agent_doc_version = "v1.0.0"`。

---

## 新增版本记录模板（复制到「版本记录」区顶部）

```markdown
### vX.Y.Z — YYYY-MM-DD（提交 <shortsha>）
- **摘要**：<一句话说明本次改了什么、为什么>
- **变更文件清单（agent 相关）**：
  - `path/one.md`
  - `path/two/SKILL.md`
- **agent 动作**：<重读上述文件；如需更新记忆/模板/定时任务，明确列出。若无额外动作写「重读上述文件即可」>
```

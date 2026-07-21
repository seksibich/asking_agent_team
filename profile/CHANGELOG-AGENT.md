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

1. 读取 `init.md` 的 `AGENT_DOC_VERSION`（目标版本），并读取运行期 `服务状态与能力.md` 的本地 `agent_doc_version`、`git_revision`、`data_version`、`selection_tag_version`。
2. 若状态文件不存在，或任一本地版本为空/不可解析，视为未知基线：全量内化当前 `init.md` 指向的全部文档，并全量刷新功能与标签目录；禁止在未知旧版本上执行增量。
3. 只有本地基线完整时才比较版本：一致则继续；目标更高时，按顺序处理所有「> 已内化版本」且「≤ 目标版本」的条目，逐条重读变更文件并执行 Agent 动作。
4. 全部补齐后，把目标版本、部署版本、同步时间和重读文件写入 `服务状态与能力.md`；文档版本不得写入主 `MEMORY.md`。

> `关注与持仓.md` 或 `portfolio_version` 缺失时，独立调用 `portfolio_get` 全量建立镜像；这不构成可执行文档增量的版本基线。

---

## 版本记录（最新在上）

### v2.7.0 — 2026-07-21（交易日定时任务精简为 T1 08:30 + T3 22:00，取消 T2 17:30 当日总结；日报目录重编号；服务端修复 NaN 序列化 500）

- **摘要**：① **取消 T2（17:30 当日总结）**，交易日盘后只保留 T3（22:00 综合复盘），当天市场复盘、板块强度、次日倾向统一并入 T3；周期任务 W1/M1/P1 不变。② 日报目录路径同步重编号：`盯盘/yyyy年MM月dd日/` 固定只含 `01-盘前汇总.md`（T1）与 `02-综合复盘.md`（T3），取消 `04-当日总结.md`，原 `05-综合复盘.md` 重编号为 `02-综合复盘.md`。③ 全量文档把「T1/T2/T3」枚举更新为「T1/T3」，并在 init.md 增加权威覆盖块统一旧编号解释。④ **服务端配套修复**（非 Agent 文档）：`macro_*`、`fundamental_*`、`hot_ths`、`market_limit` 等接口因响应含 pandas `NaN/Inf`、在 Starlette `JSONResponse`（`allow_nan=False`）渲染时抛 `ValueError` 返回 HTTP 500；已在 `service/app.py` 的 `_versioned` 增加 `_json_safe` 递归清洗为合法 JSON `null`，全量接口测试从 11 个 500 降为 0。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/schedule.md`、`agent/memory/MEMORY.md`
  - `agent/agents/TEAM.md`、`ORCHESTRATION.md`、`main-orchestrator.md`、及各分析师角色（编号边界）
  - `agent/skills/output-format/SKILL.md`（删 T2 模板、重编号、表格矩阵）、`post-market/SKILL.md`（仅 T3）、其余 SKILL 编号边界
  - `doc/02-Agent编排与业务模块.md`、`doc/SERVICE_INDEX.md`、`doc/AGENT_SERVICE_GUIDE.md`
  - `profile/CHANGELOG-AGENT.md`
- **服务配套（非 Agent 文档）**：`service/app.py`（`_json_safe` NaN/Inf 清洗）。
- **Agent 动作**：
  1. 重设定时任务：删除任何已注册的 T2/17:30 当日总结，交易日只注册 T1（08:30）与 T3（22:00），周期任务 W1/M1/P1 不变。
  2. 日报按新编号保存：`01-盘前汇总.md`、`02-综合复盘.md`；不再生成 `04-当日总结.md`。
  3. 遇旧文档 T1/T2/T3 枚举一律按 T1/T3 执行，原 T2/T3 规则按 T3 执行。

### v2.6.0 — 2026-07-21（业务文档全量落地工作目录、接口规范就近附带、定时任务描述结构化、报告接口问题置于文末、版本控制本地优先）

- **摘要**：按「一切业务文档落地工作目录、全链路文档/接口指引明确、禁止让 agent 猜」的原则系统性优化——① 各使用接口的 Skill、子 Agent 定义与定时任务就近附「接口速查」并指向工作目录接口文档；② 初始化要求把子 Agent 定义、Skill、接口文档（`AGENT_SERVICE_GUIDE.md`/`SERVICE_INDEX.md`）、执行索引全量同步落地到 `盯盘/工作文档/`（Coze 左侧 `工作文档/`）后再读取；③ 定时任务描述结构化，加载完文档后按工作目录路径生成含【工作目标 / 结果目录结构 / Agent 编排 / 使用的 Skill 与接口文档路径】四段；④ 报告中数据接口问题统一置于文末「🛠️ 数据接口问题」附录，标接口名称/功能/报错信息，仅量化选股与选股上传接口附请求参数；⑤ 版本控制本地优先（先读本地工程文件，读不到再 git），按改动类型重载——子 Agent/Skill/接口/索引更新重载工作目录缓存、定时任务定义更新重设日程；⑥ SOUL 与主记忆保留查阅路由/文档地图索引，加载完成后在 `服务状态与能力.md` 记录「工作目录文档地图」，记忆只保留最新版本快照不罗列历史。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`
  - `agent/index.md`
  - `agent/SOUL.md`
  - `agent/schedule.md`
  - `agent/memory/MEMORY.md`
  - `agent/agents/main-orchestrator.md`、`technical-trend-analyst.md`、`sentiment-analyst.md`、`fundamental-research-analyst.md`、`macro-news-analyst.md`、`backtest-analyst.md`
  - `agent/skills/*/SKILL.md`（全部 12 个：就近附接口速查 / 报告接口问题置于文末）
  - `doc/02-Agent编排与业务模块.md`
  - `profile/CHANGELOG-AGENT.md`
- **Agent 动作**：
  1. 初始化及每次任务/角色启动，先把 `index.md`、`agents/`、`skills/`、`接口文档/` 全量同步落地到工作目录缓存 `工作文档/` 再从工作目录读取；在 `服务状态与能力.md` 记录「工作目录文档地图」。
  2. 使用接口时以工作目录 `工作文档/接口文档/` 与各 Skill「接口速查」为准，禁止猜参数。
  3. 生成定时任务描述时按结构化四段模板，引用工作目录内化路径；任务定义更新时重设日程。
  4. 报告接口问题统一置于文末附录，仅量化选股与 `log_selection` 附请求参数。
  5. 版本升级本地优先读工程文件、读不到再 git；按改动类型重载工作目录缓存或重设任务；记忆只留最新版本快照。

### v2.5.0 — 2026-07-21（技能落地工作目录、中间产物不留存、报告说人话与结论前置、热点→申万分级行业量化选股并上传服务端）

- **摘要**：针对编排审查发现的四类问题优化提示词——① 初始化与每次任务/角色启动时必须先把 12 个 `SKILL.md` 落地到工作目录技能缓存（`盯盘/skills/`，Coze 左侧 `skills/`）再读取内化；② 定时任务中子 Agent 的原始意见、逐接口返回、草稿、中间打分表等中间产物一律只写 `tmp/`、任务结束即清理，只有主 Agent 汇总的最终报告与规则允许的业务快照可保存；③ 面向用户报告强制说人话、结论前置（今天市场发生了什么→板块行情大环境→明天该关注哪些股票→综合量化/择时/趋势/热点/短线选股结果与理由）、严禁暴露非结论性思考过程与原始接口数据；④ 正式选股必须先定位当日热点主线、映射申万一级/二级/三级行业、在行业内量化（禁止无差别全市场机械筛选），并按规范 `log_selection` 上传服务端、报告写明逐只选股理由。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`
  - `agent/index.md`
  - `agent/agents/TEAM.md`
  - `agent/agents/ORCHESTRATION.md`
  - `agent/memory/MEMORY.md`
  - `agent/skills/output-format/SKILL.md`
  - `agent/skills/post-market/SKILL.md`
  - `agent/skills/quant-screening/SKILL.md`
  - `agent/skills/stock-screening/SKILL.md`
  - `doc/02-Agent编排与业务模块.md`
  - `profile/CHANGELOG-AGENT.md`
- **Agent 动作**：
  1. 初始化及每次任务/角色启动时，先把 12 个 `SKILL.md` 同步到工作目录技能缓存并从工作目录读取内化；文档版本变化时先刷新缓存。技能缓存不进记忆。
  2. 定时任务中间产物只写 `tmp/`、任务结束即清理；只保留主 Agent 最终报告与规则允许的业务快照。
  3. 生成面向用户报告时严格说人话、按用户视角前置结论、不暴露过程与原始接口数据。
  4. 正式选股先定位热点主线→映射申万分级行业→在行业内 `screen_quant`/`screen_trend` 量化→逐只 `log_selection` 上传服务端并在报告写明选股理由；禁止无差别全市场机械筛选。

### v2.4.2 — 2026-07-21（量化盯盘历史回看与默认入口调整）

- **摘要**：修复量化盯盘跨自然日后无法查看上一交易日数据的问题；聚合消息改为保留最近 30 个自然日，接口支持指定日期并在缺省时返回最近有数据日。前端增加历史日期切换和“最新”实时模式，历史页不接收 WebSocket 覆盖；管理员默认进入量化盯盘，并按“盯盘→情绪→行业→选股→看板→回测→预计算→配置”重排主入口，自选管理并入配置二级入口。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`
  - `agent/skills/data-service/SKILL.md`
  - `doc/01-系统全景与审查结论.md`
  - `doc/02-Agent编排与业务模块.md`
  - `doc/03-前端业务与全时段规则.md`
  - `doc/04-数据存储缓存与一致性.md`
  - `doc/05-测试探针监控与运维.md`
  - `doc/AGENT_SERVICE_GUIDE.md`
  - `doc/SERVICE_INDEX.md`
  - `profile/CHANGELOG-AGENT.md`
- **服务、前端与测试配套（非 Agent 文档）**：`service/quant_watch.py`、`service/db.py`、`service/db/schema.sql`、`service/db/README.md`、`service/web/index.html`、`service/web/app.js`、`service/web/style.css`、`tests/test_observability.py`。
- **Agent 动作**：
  1. 用户要求盘中或历史盯盘分析时，可单次调用 `quant_watch_status` 并传入目标日期；不传日期时接受服务端返回的最近有数据日。
  2. 必须区分请求日期、实际返回日期、当前上海日期和历史标记；无数据时如实披露，不用其他日期结果冒充。
  3. 历史查询仍不构成自动扫描、轮询或主动通知授权，盘中聚合也不得写成完整日因子、正式选股或预测。

### v2.4.1 — 2026-07-19（监控降噪、日志生命周期与部署配置收口）

- **摘要**：将普通 4xx 业务拒绝与 408/429/5xx 服务故障分开统计，避免监控误报；为审计与监控日志增加默认 90 日保留、7 日后 gzip 压缩、调查冻结开关和磁盘双阈值告警；部署完成地址改由 `PUBLIC_BASE_URL` 配置；服务指南中的日期、功能数和版本改为动态占位示例。
- **对应 git commit**：`64c8bcf`。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`
  - `doc/01-系统全景与审查结论.md`
  - `doc/05-测试探针监控与运维.md`
  - `doc/AGENT_SERVICE_GUIDE.md`
  - `profile/CHANGELOG-AGENT.md`
- **服务与工程配套（非 Agent 文档）**：`service/observability.py`、`tests/test_observability.py`、`profile/.env.example`、`deploy/remote_deploy.sh`、`DEPLOY.md`。
- **Agent 动作**：
  1. 重读上述文档，监控分析时分别报告业务拒绝和服务故障；普通 4xx 不得直接表述为服务异常。
  2. 容量告警、日志维护异常和压缩/删除结果只用于运维判断；行情、选股和交易结论仍必须来自业务事实源。
  3. 读取 `/health`、`/functions` 等运行时值建立版本基线，不得使用服务指南中的占位示例。

### v2.4.0 — 2026-07-19（服务就绪、全时段前端、原子性与运行监控审查）

- **摘要**：完成工程全景审查并建立统一文档中心；区分进程存活、生产就绪和 Agent 兼容诊断，补齐前端上海时钟全阶段规则、局部刷新和自动化测试；强化动态访客 Key 摘要存储、选股登记与筛选证据链原子性；新增接口/量化盯盘性能事件、每日中文汇总及阿里云 systemd 探针定时器。
- **对应 git commit**：`64c8bcf`。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`
  - `doc/README.md`
  - `doc/01-系统全景与审查结论.md`
  - `doc/02-Agent编排与业务模块.md`
  - `doc/03-前端业务与全时段规则.md`
  - `doc/04-数据存储缓存与一致性.md`
  - `doc/05-测试探针监控与运维.md`
  - `doc/AGENT_SERVICE_GUIDE.md`
  - `doc/SERVICE_INDEX.md`
  - `profile/CHANGELOG-AGENT.md`
- **服务与工程配套（非 Agent 文档）**：`service/app.py`、`service/common.py`、`service/db.py`、`service/loader.py`、`service/observability.py`、`service/quant_watch.py`、`service/web/**`、筛选脚本、`tests/**`、`deploy/**`、`docker-compose.yml`、`README.md`、`DEPLOY.md`、`.kiro/steering/**`。
- **Agent 动作**：
  1. 重读本条文档清单，从 `doc/README.md` 建立系统、业务、前端时段、数据一致性和运维文档索引。
  2. 继续使用 `/health` 做五轨版本与市场状态协调；服务故障诊断时用 `/live` 区分进程存活、用 `/ready` 判断能否接收生产流量。
  3. 盘中行业只在连续竞价读取临时叠加；其他阶段使用最近完整日。情绪盘中值不得写成日终事实。
  4. 每日监控汇总只用于分析稳定性与性能，不得作为行情、正式选股、预测或交易结论。
  5. 任何访问 Key 都只存在安全配置；动态 Key 完整值不可从数据库或列表恢复，不得写入记忆与报告。

### v2.3.0 — 2026-07-17（服务端量化盯盘与 Agent 职责分层）

- **摘要**：新增服务端确定性量化盯盘基础设施，允许数据服务在交易日连续竞价时按配置频率扫描并通过当日接口、WebSocket 与显式启用的通知渠道发布聚合结论；同时继续禁止 Agent 调度、循环或主动触发盯盘。盘中评分与完整日因子、正式选股、预测及记忆严格隔离；分钟样本、申万层级或逐笔大单源缺失时必须披露并停止对应指标参与评分。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/schedule.md`
  - `agent/agents/TEAM.md`、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`
  - `agent/skills/data-service/SKILL.md`、`agent/skills/intraday-watch/SKILL.md`
  - `profile/CHANGELOG-AGENT.md`
- **Agent 动作**：
  1. 删除和禁止的仍是 Agent/平台层自动竞价、自动盯盘、午间总结、Hook、cron 与循环；不得把服务端 `quant_watch` 迁移成 Agent 任务。
  2. 只有用户当前明确请求盘中分析时，才读取 `quant_watch_status` 作为当日临时证据；不得轮询或自动调用 `quant_watch_scan_once`。
  3. 服务端飞书/企业微信通知默认关闭；除非用户明确要求，不修改盯盘设置或通知开关。
  4. 盘中结果不得写入完整日因子、正式选股、预测、daily 或记忆；大单与行业层级等不可用项必须原样披露。

### v2.2.0 — 2026-07-17（现行任务连续编号 + 服务端 16:00 日终收口）

- **摘要**：以 `agent/schedule.md` 为现行基准，将 17:30 当日总结由旧 T6 统一为 T2、22:00 综合复盘由旧 T7 统一为 T3；Agent 仅注册 T1/T2/T3/W1/M1/P1，初始化清理旧 T6/T7/D1、历史 T2/T3/T4/T5 及旧自动盯盘任务。全市场行业与个股因子改由服务端在交易日 16:00 自动补齐，Agent 只读 `health.daily_finalize` / `precompute_status`，不得定时、自动补跑或失败触发 `precompute_daily_factors`；管理员手动调用仅限用户当前明确要求的单次诊断或补数。
- **对应 git commit**：待提交。
- **变更文件清单（Agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/schedule.md`
  - `agent/agents/TEAM.md`、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`
  - `agent/agents/technical-trend-analyst.md`、`agent/agents/sentiment-analyst.md`、`agent/agents/fundamental-research-analyst.md`、`agent/agents/macro-news-analyst.md`、`agent/agents/backtest-analyst.md`
  - `agent/skills/priority-framework/SKILL.md`、`agent/skills/data-service/SKILL.md`、`agent/skills/output-format/SKILL.md`、`agent/skills/pre-market/SKILL.md`
  - `agent/skills/bidding-analysis/SKILL.md`、`agent/skills/intraday-watch/SKILL.md`、`agent/skills/post-market/SKILL.md`、`agent/skills/industry-analysis/SKILL.md`
  - `agent/skills/stock-screening/SKILL.md`、`agent/skills/quant-screening/SKILL.md`、`agent/skills/review-learning/SKILL.md`、`agent/skills/stock-research/SKILL.md`
  - `profile/CHANGELOG-AGENT.md`
- **Agent 动作**：
  1. 重读本条全部变更文件及 `agent/schedule.md`，将所有现行执行、模板和角色绑定统一为 T1/T2/T3/W1/M1/P1；T2 对应 17:30 当日总结，T3 对应 22:00 综合复盘。
  2. 初始化时删除旧 T6/T7/D1、历史 T2/T3/T4/T5，以及所有自动竞价、盘中盯盘、午间总结和 Agent 预计算任务；仅注册 T1/T2/T3/W1/M1/P1。
  3. T2/T3 只读服务端 `health.daily_finalize` / `precompute_status`。状态失败、覆盖不足或因子缺失时披露缺口并按报告规则重试读取，禁止调用 `precompute_daily_factors` 自动补跑。
  4. 仅在用户当前明确要求管理员诊断或补数时，才可说明目标日期与用途后单次手动调用 `precompute_daily_factors`；不得由定时任务、失败回退、Hook、cron 或 Agent 循环触发。
  5. 历史 CHANGELOG 中 v2.1.0 及更早版本的旧编号继续作为历史事实保留，不作为当前调度依据。

### v2.1.0 — 2026-07-16（统一业务响应 health + 五轨版本协调 + 闭环补强）

- **摘要**：所有已连通的服务业务 JSON 响应在保留原状态码与顶层 `data_version` 的同时统一携带 `/health` 同口径快照，覆盖成功、权限/业务错误、404/405、422 与未捕获 500；健康探测逐项容错，不得覆盖原业务结果。Agent 改为先协调五轨版本再处理业务结果，增加旧服务单次 `/health` 回退、目标版本元组一次性锁和权限阻塞待办。同步补齐 T1 正式候选必须来自真实筛选运行，以及 T7 新预判登记与成熟历史回测的顺序隔离。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/schedule.md`
  - `agent/memory/MEMORY.md`、`agent/memory/templates/服务状态与能力.md`
  - `agent/agents/TEAM.md`、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`
  - `agent/skills/data-service/SKILL.md`、`agent/skills/pre-market/SKILL.md`、`agent/skills/post-market/SKILL.md`、`agent/skills/review-learning/SKILL.md`
  - `doc/AGENT_SERVICE_GUIDE.md`、`doc/SERVICE_INDEX.md`
  - 服务端（非 Agent 文档，一并记录）：`service/app.py`
- **Agent 动作**：
  1. 重读本条全部文件，把每次业务 JSON 响应的处理顺序改为“先读取 health 并协调版本，再处理成功数据或错误”。
  2. 以 `agent_doc_version|git_revision|data_version|selection_tag_version|portfolio_version` 目标元组作为单任务一次性锁；相同元组不得因升级或刷新响应再次触发。
  3. 旧服务缺少 `health` 时整条请求链只补调一次 `/health`；401/403 阻塞刷新时创建有时效短期事项，不得声称同步完成。
  4. T1 新增正式 auto 候选必须实际执行 `screen_quant`/`screen_trend` 并原样携带 `screening_run_id`；否则只复核既有候选。T7 先登记新方向性预判，再仅回测目标日已成熟的历史预判，二者不得混算。

### v2.0.0 — 2026-07-16（记忆生命周期分层 + SOUL 纯人格边界）

- **摘要**：对运行期记忆做不兼容分层。主 `MEMORY.md` 只允许永久规范、稳定偏好、经多个独立样本验证的通用经验及专项索引；用户稳定资料独立写 `USER.md`；短期事项改为目录内一事一文件，统一命名 `YYYYMMDD-HHmm-有效至YYYYMMDD-HHmm-描述.md`，接口报错附件复用事项前缀并随事项删除。关注持仓与服务能力分别独立维护。新增本地/Coze 工作文件—记忆双栏映射和 `tmp/tmp_YYYYMMDD-HHmmss_文件名` 普通临时产物规则；本地关键版本缺失时必须全量初始化，禁止未知基线增量。`SOUL.md` 只保留人格、行为边界和查阅路由。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/SOUL.md`、`agent/schedule.md`
  - `agent/memory/MEMORY.md`、`agent/memory/PORTFOLIO.md`
  - `agent/memory/templates/MEMORY.md`、`agent/memory/templates/USER.md`、`agent/memory/templates/短期记忆.md`、`agent/memory/templates/服务状态与能力.md`、`agent/memory/templates/关注与持仓.md`、`agent/memory/templates/学习日志.md`
  - 删除 `agent/memory/templates/service_state.json`、`agent/memory/templates/观察池.md`、`agent/memory/templates/用户画像.md`
  - `agent/agents/TEAM.md`、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`、`agent/agents/fundamental-research-analyst.md`
  - `agent/skills/data-service/SKILL.md`、`agent/skills/output-format/SKILL.md`、`agent/skills/pre-market/SKILL.md`、`agent/skills/post-market/SKILL.md`、`agent/skills/priority-framework/SKILL.md`、`agent/skills/industry-analysis/SKILL.md`、`agent/skills/stock-screening/SKILL.md`、`agent/skills/quant-screening/SKILL.md`、`agent/skills/review-learning/SKILL.md`、`agent/skills/stock-research/SKILL.md`、`agent/skills/bidding-analysis/SKILL.md`、`agent/skills/intraday-watch/SKILL.md`
  - `doc/AGENT_SERVICE_GUIDE.md`、`DEPLOY.md`、`service/db/PERSISTENCE.md`
- **Agent 动作**：
  1. 若本地服务状态文件不存在，或 `agent_doc_version`、`git_revision`、`data_version`、`selection_tag_version` 任一为空，直接全量初始化当前文档、功能和标签，禁止按未知旧版本增量。
  2. 创建 `MEMORY.md`、`USER.md`、`服务状态与能力.md`、`关注与持仓.md` 和短期目录；主 MEMORY 只迁移永久规范、稳定约定、已验证经验及索引，用户稳定资料按 USER 门禁迁移。
  3. 把旧 `service_state.json` 的连接、文档/部署/功能/标签版本迁入 `服务状态与能力.md`；把持仓版本及全量 rows 写入 `关注与持仓.md`；核验后删除旧文件。
  4. 把旧 `观察池.md` 中仍有效线索逐条迁为短期事项；按新文件名补齐时效、动作和删除条件。接口报错附件与事项同前缀关联；完成、证伪或过期时删除事项及全部附件。
  5. 普通临时脚本、文档和转换产物只写工作文件根 `tmp/`；Coze 严格执行左侧工作文件、右侧记忆映射，真实密钥只在平台安全区管理。
  6. 每次开场读取永久 MEMORY、USER、服务状态及当前任务相关专项记忆；版本和接口查服务状态，持仓查持仓镜像，选股风格查永久 MEMORY 与 `priority-framework`。子 Agent 不直接写共享记忆，不把具体业务内容追加到 SOUL。

### v1.9.0 — 2026-07-16（量化市场筛选闭环 + 取消自动盯盘）

- **摘要**：`screen_quant` 市场筛选已端到端接入，支持沪深主板、科创板和创业板，并与个股/行业条件取交集；北交所独立识别但暂不纳入量化因子预计算。同步取消全部自动竞价与盘中盯盘：删除原 T2/T3/T4/T5，初始化必须清理遗留任务，禁止平台调度器、Agent 循环、Hook 或 cron 自动触发竞价、盘中扫描和午间总结。`bidding-analysis`、`intraday-watch` 及服务函数保留，但只能在用户明确请求时单次执行，响应结束即停止，不写自动盯盘记忆。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`、`agent/index.md`、`agent/schedule.md`、`agent/memory/MEMORY.md`（版本保持 v1.9.0；注册链删除 T2-T5，强制清理旧任务与自动盘中预判入口）
  - `agent/agents/TEAM.md`、`agent/agents/ORCHESTRATION.md`、`agent/agents/main-orchestrator.md`、`agent/agents/sentiment-analyst.md`（删除固定时点/循环盯盘编排，仅保留用户显式单次路由）
  - `agent/skills/intraday-watch/SKILL.md`、`agent/skills/bidding-analysis/SKILL.md`（改为仅用户可调用，禁止自动触发、续跑及自动记忆）
  - `agent/skills/pre-market/SKILL.md`、`agent/skills/output-format/SKILL.md`、`agent/skills/data-service/SKILL.md`（断开盘前→竞价/盯盘自动衔接，更新目录与容错规则）
  - `agent/skills/quant-screening/SKILL.md`（三类市场、北交所排除、严格校验和组合规则）
  - `doc/AGENT_SERVICE_GUIDE.md`、`doc/SERVICE_INDEX.md`（服务能力保留但标明竞价/盯盘仅用户显式调用；定时交叉表移除 T2-T5）
  - 服务端与前端（非 Agent 文档，一并记录）：`agent/skills/quant-screening/scripts/quant_screen.py`、`service/web/index.html`、`service/web/app.js`
- **agent 动作**：
  1. 初始化时删除所有遗留 T2/T3/T4/T5，以及任何调用 `bidding_analysis`、`watch_intraday` 的平台任务、Hook、cron 或循环；不得重新创建。
  2. 仅注册 `schedule.md` 保留的 T1/T6/T7/W1/M1/D1/P1。竞价或盘中分析必须有当前用户明确请求，每次只执行一轮，结束后不续跑。
  3. 手动竞价/盯盘产出进入 `投研/yyyyMMdd-手动xx/`，不写 predictions、daily、观察池或 `category=auto`。
  4. 刷新 `/functions`，量化市场筛选只传 `main|star|gem`；北交所暂不支持，不得并入主板。
  5. 将筛选响应和运行快照中的 `boards` 视为实际生效范围；非法值或空数组报错后修正，不得扩大范围。

### v1.8.0 — 2026-07-16（规范化选股上传 + 标签契约 + 实时聚合看板）

- **摘要**：规范 `log_selection` 的股票代码、选股时间、核心事件、精炼理由和标签字段，保留并强化同日筛选运行、评分分位、因子契约与上游依赖门禁。新增版本化标签合集 `selection_tag_catalog`，固定标签含龙头、核心、补涨、趋势、连板、弹性、位置、驱动等，允许 Agent 自编排板块/题材/事件标签；服务端自动补充最新价及涨停/跌停标签。选股看板默认展示目标交易日及之前三个交易日，每次进入刷新行情，支持手动刷新；仅日期筛选按题材聚合，题材筛选按日期聚合，聚合内龙头/核心优先，其余按评分排序，全局排序取消聚合。同时修复 `log_selection` 成功写库后因 `Decimal` 等数据库类型无法直接 JSON 序列化而偶发 HTTP 500，以及非法兼容分数触发 500、实时价与异日涨幅/限价混用的问题；重复上传明确区分首次固化记录与本次刷新行情。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`、`agent/index.md`（版本→v1.8.0；五轨版本自检、规范上传和看板聚合规则）
  - `agent/memory/MEMORY.md`、`agent/memory/templates/service_state.json`（新增标签版本与标签合集缓存）
  - `agent/skills/review-learning/SKILL.md`（完整上传契约、标签顺序、理由精炼与重复上传语义）
  - `agent/skills/quant-screening/SKILL.md`、`agent/skills/stock-screening/SKILL.md`（正式候选标签与上传要求）
  - `agent/skills/data-service/SKILL.md`（标签合集功能、健康版本字段和行情补充说明）
  - `doc/AGENT_SERVICE_GUIDE.md`、`doc/SERVICE_INDEX.md`（接口、五轨版本、默认日期、聚合与幂等协议）
  - 服务端与前端（非 Agent 文档，一并记录）：`service/selection_tags.py`、`service/registry.py`、`service/app.py`、`agent/skills/review-learning/scripts/selection_backtest.py`、`service/web/index.html`、`service/web/app.js`、`service/web/style.css`
- **agent 动作**：
  1. 重读上述文件；发现 `/health.selection_tag_version` 变化时调用 `selection_tag_catalog`，将标签版本及 `{tag, description}` 写入 `service_state.json`。
  2. 正式候选继续先取得同日 `screening_run_id`；上传时提供 `selected_at`、精炼 `core_event`、精炼 `reason` 和 `tags`，不得自行填写或覆盖服务端固化评分与因子契约。
  3. 标签顺序按“主板块/题材 → 细分方向 → 具体事件 → 固定属性”；固定标签优先从合集选，业务标签可自行编排，无标签时接受看板显示“未分类”。
  4. 使用服务端返回的最新价和涨停/跌停标签；失败时披露错误，不自行推断。重复上传时以 `record` 为首次固化记录，以 `current_quote` 为本次刷新行情。
  5. 将此前 `log_selection` HTTP 500 标记为服务端响应序列化缺陷；升级后重新调用即可，成功响应不再因 Decimal/日期类型编码失败。

### v1.7.0 — 2026-07-16（管理员自选事实源 + 关注持仓版本同步）

- **摘要**：新增管理员专属「自选」管理体系，将当前关注与持仓从按日选股快照中分离为按股票代码唯一的结构化事实源。新增名称/代码片段模糊搜索并要求从结果选择，持仓强制真实成本与整数手数；服务端支持批量上传、同批次最后一项生效、内容无变化不升版，并通过 `/health.portfolio_version` 驱动 Agent 同步。运行期 `关注与持仓.md` 改为服务端镜像，上传成功后才覆盖；历史 `watch/holding` 选股快照仅在用户明确要求回测时额外登记。
- **对应 git commit**：待提交。
- **变更文件清单（agent 相关）**：
  - `agent/init.md`（版本→v1.7.0；新增四轨版本、自选事实源、搜索选择与上传约束）
  - `agent/index.md`（开场同步、强制记忆与当前自选/历史快照边界）
  - `agent/memory/MEMORY.md`、`agent/memory/PORTFOLIO.md`（自选同步流程、冲突规则、成本/手数模型）
  - `agent/memory/templates/关注与持仓.md`、`agent/memory/templates/daily-观察对象.md`、`agent/memory/templates/service_state.json`（当前状态镜像、每日快照字段与 portfolio_version）
  - `agent/skills/data-service/SKILL.md`（管理员自选接口、版本机制、搜索后选择和上传协议）
  - `agent/skills/output-format/SKILL.md`、`agent/skills/stock-research/SKILL.md`、`agent/skills/stock-screening/SKILL.md`（用户关注/持仓指令统一先上传，历史回测改为显式可选）
  - `doc/AGENT_SERVICE_GUIDE.md`、`doc/SERVICE_INDEX.md`（接口权限、版本字段、功能与持久化说明）
  - 服务端与前端（非 Agent 文档，一并记录）：`service/app.py`、`service/db.py`、`service/db/schema.sql`、`service/web/index.html`、`service/web/app.js`、`service/web/style.css`、`agent/skills/data-service/scripts/portfolio.py`
- **agent 动作**：
  1. 重读上述文件；把服务端 `portfolio_items` 视为当前关注与持仓唯一事实源，把运行期 `关注与持仓.md` 视为可重建镜像。
  2. 每次开场对比 `/health.portfolio_version`；版本不一致时调用 `portfolio_get`，以响应 rows 覆盖镜像并更新 `service_state.json`。
  3. 新增股票先调用 `portfolio_stock_search`，必须从结果选择完整代码和标准名称；持仓缺真实成本或整数手数时先追问，禁止猜测。
  4. 每次增删改先形成内存草稿并调用 `portfolio_upload`；仅上传成功后刷新镜像，失败时保留待同步事项并如实告知。
  5. 默认不调用 `log_selection(category=watch|holding)`；只有用户明确要求历史观察回测时才额外登记，且继续与 auto 调参样本隔离。

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

# Agent 文档变更日志（CHANGELOG-AGENT）

> 本文件记录所有 **agent 相关文档/规范**（`init.md` / `index.md` / `schedule.md` / `SOUL.md` /
> `memory/**` / `agents/**` / `skills/**/SKILL.md` / `service/AGENT_SERVICE_GUIDE.md` 等）的版本化变更。
> 数据服务**功能索引**的变更由服务端 `data_version` 机制单独管理（见 index.md），二者互不替代。

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

### v1.2.0 — 2026-07-15（提交待回填）

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

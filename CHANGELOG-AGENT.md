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

### v1.1.0 — 2026-07-15（待提交）

- **摘要**：在既有情绪极端指数、连板与断板反包、强制 Agent→Skill 绑定和统一 fallback 基础上，继续扩充当前 v1.1.0：T1/T6/T7 改为“详尽 Markdown 报告 + 独立精简推送”，报告强制“核心摘要→目录导读→详细正文”；正式量化/趋势候选新增逐股完整理由链与综合理由表；T7 在业绩窗口新增由基本面分析师维护、全量去重且不持久化/不回测调参的“业绩增长参考池”。版本仍为 v1.1.0，不新增版本，全部改动继续待提交。
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
  1. 重新逐文件完整加载固定 11 个 Skills（首次及每次任务/角色启动均执行），并按 `agents/TEAM.md` 与角色主绑定点名分发；继续执行 `skills/data-service/SKILL.md` 的 fallback、缺失标注、T1/T6/T7 5/15 分钟延迟重试和 T4 新闻降级链。
  2. T1/T6/T7 生成报告时使用 `skills/output-format/SKILL.md` 的对应完整模板：正文尽可能详尽，摘要后必须是目录导读；数据缺失保留章节并写明 fallback 与实际日期。另生成建议≤500字的重点推送，包含报告路径和降级提示，不得复制全文或只报“已生成”。
  3. 所有正式量化/趋势候选逐只填写「正式候选综合理由表」，理由链固定为“量化信号→板块趋势→当前主线关系→涨价/逻辑/预期催化→情绪与择时→风险/证伪”；缺失环节写“无可核验证据”。
  4. T7 基本面分析师在法定/惯例披露季或最近3个交易日接口返回新预告/快报/公告时，调用 `fundamental_forecast`、`fundamental_express`、`news_anns`，必要时用 `fundamental_income`、`fundamental_fina_indicator` 复核；按 `code+report_period+announcement_date` 去重并全量输出业绩增长参考池，无记录明确“当晚无可核验的增长/预增公告”。
  5. 强制隔离业绩增长参考池：不得调用 `log_selection`，不得写 `predictions.jsonl`/观察池，不纳入 auto/watch/holding、回测样本或调参；同股只有独立通过正式流程才由正式流程持久化。不得宣称业绩增长必然利好，PE/PB 仍只作风险背景。

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

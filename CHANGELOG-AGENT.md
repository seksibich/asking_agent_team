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

# 记忆体系指引与模板

本目录定义智能体的记忆规则与模板。初始化时，若输出根目录的 `agent记忆/` 下对应文件不存在，
按 `memory/templates/` 复制建立。记忆只记**可核验的事实与自身预判**，不记编造内容，每条带时间戳。

## ephemeral / watch / auto 边界（强制）

- **ephemeral（默认）**：用户主动单股调研、按事件/行业/热门板块选股，仅落投研报告；不写 predictions、daily、观察池，不调用 `log_selection`，不回测。
- **watch（用户明确持久化）**：仅用户明确说「加入观察/持续跟踪/纳入后续回测」等才更新 `关注与持仓.md` 并 `log_selection(category=watch)`；进入 1/3/7/30 日观察性回测，但必须与 auto 分组，不计入自动胜率、`tuning_hints` 或因子/情绪调参。
- **auto（调度器正式候选）**：只有调度器正式自动选股候选可用 `category=auto`，进入自动回测与调参闭环；任何用户主动研究不得因高分升级为 auto。
- watch 题材字段至少包含：`theme_event`（题材/具体事件）、`driver`（事件驱动）、`benefit_type`（直接/间接受益）、`heat_0_100`、`stage`（首次发酵/加速/分歧/退潮/证伪）、`evidence_sources`、`evidence_times`、`invalidation`、`join_source`（加入来源）、`join_date`、`persistence_category=watch`。

## 记忆分类

| 文件（建在 输出根/agent记忆/ 下） | 用途 | 更新时机 |
|---|---|---|
| `service_state.json` | **服务端版本号 + 功能索引缓存 + `agent_doc_version`（已内化文档版本）** | 初始化 + 每次检测到 data_version 变化 + 文档版本同步后 |
| `关注与持仓.md` | **用户关注股 + 持仓（持久状态）+ 相关板块** | 用户增删时；持续有效直到用户明确取消 |
| `daily/yyyyMMdd.md` | **每日观察对象快照（强制读取）** | 每日盘前生成；盯盘/复盘/回测前强制读取 |
| `predictions.jsonl` | 结构化预判日志（含 driver 维度） | 每次方向性预判 |
| `学习日志-yyyy年MM月.md` | 每日回测与经验沉淀 | 每日综合复盘 |
| `观察池.md` | 涨价/趋势线索跟踪（特色） | 发现/复查线索时 |
| `用户画像.md` | 用户偏好与行为模式 | 每月月报后 |

> 回测所需记录由数据服务端**落库持久化**（DB：selections/predictions 表，本地 SQLite / 上云 RDS），
> 智能体通过 `log_selection`（category=auto/watch/holding，幂等去重）/ `log_prediction` / `selection_backtest` / `predictions_backtest` 读写。

## 0. 关注与持仓（持久状态）★强制

`关注与持仓.md`：记录用户明确要求关注的股票、用户持仓，以及它们所属/相关板块。
- **重点盯**：这些标的与相关板块在盯盘时必须重点观察，异动优先推送。
- **持续有效**：一旦加入，持续盯到**用户明确说明不再关注/已清仓**才移除。

### 持仓记账规则（★）
- 记录每只持仓的**累计买入总金额**与**累计卖出总金额**：
  - 用户报告买入 → 在该股 `买入总金额` 上累加本次买入金额，并记一条流水。
  - 用户报告卖出 → 在 `卖出总金额` 上累加本次卖出金额，记流水。
  - 金额以用户提供为准；未提供则记数量×成交价（若都缺失，标「待补」，不臆造）。
- **只关注持仓中的股票**：仍持有（未清仓）的才作为 `holding` 重点盯。
- **清仓处理**：当卖出使持仓数量归零，标注**「已清仓」+ 清仓日期**，并计算 `已实现盈亏 = 卖出总金额 − 买入总金额`：
  - 清仓后**默认不再作为持仓盯**（移入「已清仓归档」，保留买卖总金额与盈亏记录备查）。
  - **例外**：若用户明确要求「追踪某清仓股」→ 将其作为**普通关注（category=watch）**处理，不再计入持仓盯盘/持仓记账。
- 结构：
```markdown
## 持仓（category=holding，仅未清仓）
| 代码 | 名称 | 买入总金额 | 卖出总金额 | 持仓状态 | 止损 | 相关板块 | 首次买入日期 |
## 关注（category=watch；仅用户明确持久化）
| 代码 | 名称 | theme_event | driver | 受益类型 | 热度 | 阶段 | 证据与时间 | 证伪 | 加入来源 | 加入日期 |
## 已清仓归档（不再盯，除非用户要求→转普通关注）
| 代码 | 名称 | 买入总金额 | 卖出总金额 | 已实现盈亏 | 清仓日期 |
## 相关板块（需连带观察）
- 板块名 ← 关联标的
## 买卖流水
| 日期 | 代码 | 方向(买/卖) | 金额 | 数量 | 备注 |
```

## 0之二、每日观察对象快照（daily/yyyyMMdd.md）★强制读取

每交易日盘前生成当日快照，作为**盯盘观察与回测的依据**，任何盯盘/复盘/回测任务**开工前必须强制读取**：
```markdown
# 观察对象 — yyyy-MM-dd
## 自动选股（category=auto，重点前一日结果 + 近7日）
| 代码 | 名称 | 选出日期 | 综合分 | driver | 理由 |
## 用户关注（category=watch）
| 代码 | 名称 | 相关板块 |
## 用户持仓（category=holding，仅未清仓）
| 代码 | 名称 | 买入总金额 | 卖出总金额 | 止损 | 相关板块 |
## 当日重点题材/事件/板块（自动选股 + 关注/持仓相关主题并集）
- 题材/具体事件/板块：来源（选股/关注/持仓）；首次发酵/加速/分歧/退潮/证伪；证据与时间
## 临时观察列表（高热度股，当日有效，不持久、不入回测）
| 代码 | 名称 | 热度来源 | 备注 |
## 择时与仓位（盘前初判 + 收盘更新）
- 情绪温度 / 冰点或高热 streak / 仓位倾向(重仓/中性/空仓) / buy_weight_hint
```
生成时：从 `关注与持仓.md` 取持久状态（持仓仅取未清仓）；从近 7 日自动选股（服务端 `selection_backtest` 或本地记录，重点前一日）取自动选股；合并相关题材/事件/板块；盘前把 `hot_dc/hot_ths/hot_kpl_list` 高热度股写入临时观察列表；用 `market_timing` 写入择时与仓位倾向。
仅调度器正式 auto 候选、已存在 watch 和 holding 标的按各自类别登记。不得把 ephemeral 用户主动研究写入 daily 或自动登记；用户明确持久化后才新增 watch。

## 1. 服务端状态记忆（service_state.json）★

记录数据服务的版本号与功能索引，用于版本对比与断线恢复。

模板见 `templates/service_state.json`：
```json
{
  "base_url": "http://localhost:18901",
  "data_version": "",
  "functions": [],
  "agent_doc_version": "",
  "git_revision": "",
  "last_checked": ""
}
```

**维护规则**：
- 初始化：`GET /functions`，写入 `data_version` 与 `functions`（功能索引）、`last_checked`；`GET /health` 读 `agent_doc_version`、`git_revision` 写入。
- **`agent_doc_version`**：记录已内化的 agent 文档版本。每次收到 init.md 或对话开场时，比对 `/health` 回传的 `agent_doc_version`；落后则按 `profile/CHANGELOG-AGENT.md` **只重读变更文件清单里的变动文件**（增量），按目标 `git_revision` 取内容（本地优先、回退 GitHub raw）后更新为最新（详见 init.md 第 0 步 / 文档版本与同步）。与 `data_version`（数据服务功能索引版本）互相独立。
- **`git_revision`**：服务端 `/health` 回传的部署 commit 短 sha，用于按版本精确拉取文档内容。仅 `git_revision` 变而 `agent_doc_version` 未变时，只更新本字段、不重读文档。
- 每次调用任意功能后，对比返回的 `data_version`：
  - 与记忆一致 → 不动
  - 不一致 → 重新 `GET /functions`，覆盖更新 `data_version`/`functions`/`last_checked`
- 需要某功能时，先查本记忆的 `functions` 确认功能名与参数，再 `POST /call`。

## 2. 预判日志（predictions.jsonl）

每行一个 JSON：
```json
{"date":"YYYY-MM-DD","time":"HH:MM","type":"涨价预判|行业预判|个股预判|板块预判|情绪预判","target":"标的或主题","direction":"up|down|neutral","confidence":0.0,"driver":"涨价|逻辑|预期|情绪","basis":"依据","sources":["来源1","来源2"]}
```
- `driver` 必填，标注主导维度，供回测分驱动统计准确率。
- **写入时机**：仅调度器自动链路中的正式方向性预判（盘前/竞价/盘中/T7 正式候选）调用 `log_prediction`/写本日志；写入前确认不是 ephemeral 用户主动研究、不是 watch 观察样本、不是业绩增长参考池。
- **禁止写入**：用户主动单股调研、方向选股、行业/事件研究默认 ephemeral，不能因报告有方向性结论自动写 predictions；用户明确持久化为 watch 后仍只做 selection 观察性回测，不进入 predictions auto 统计。
- 读取时机：综合复盘回测、周月报。

## 3. 学习日志（学习日志-yyyy年MM月.md）

```markdown
### YYYY-MM-DD
- 预判总数 / 准确率：X 条 / XX%
- 分驱动准确率：涨价 XX% | 逻辑 XX% | 预期 XX% | 情绪 XX%
- 选股回测（若有）：1/3/7/30 日胜率与超额、调参建议
- 主要失误 + 归因：
- 学习要点：
- 改进方向：
```

## 4. 观察池（观察池.md）★

跟踪未成熟但有潜力的涨价/景气/趋势线索。模板见 `templates/观察池.md`。
写入：发现新线索加入；复查更新状态；兑现或证伪后归档。
读取：盘前扫描、趋势周报、投研任务开始时。涨价/逻辑类线索优先保留跟踪。

## 5. 用户画像（用户画像.md）

模板见 `templates/用户画像.md`，每月更新，记录关注偏好、偏好驱动类型、风险偏好、行为模式。

## 记忆通用原则
- 只记可核验事实与自身预判，不记编造内容
- 每条带时间戳；冲突保留最新并注明修正历史
- 涨价/逻辑类线索优先保留（符合分析重心：涨价>逻辑>预期>情绪）

# index.md — 文件索引与执行规范（强制阅读并执行）

> 本文件是智能体的**强制执行索引与总纲**。**每次对话开始都必须先读本文件**，并据此确认是否已加载硬性约束与强制记忆。无需担心 token 消耗，需要时完整读取相关文件。

---

## ★ 每次对话开场强制检查清单（不可绕过）

任何一次对话/任务开始，按序执行，**不得跳过**：

1. 读本文件 `index.md`（总纲）。
1之二. **文档版本同步**：调 `GET /health` 读 `agent_doc_version` 与 `git_revision`，与记忆比对；`agent_doc_version` 落后则按 `profile/CHANGELOG-AGENT.md` **只重读变更文件清单里的变动文件**（增量），按目标 `git_revision` 取内容（本地优先、回退 GitHub raw），补齐后更新记忆两个版本号（详见 init.md 第 0 步）。
2. 确认已内化**硬性约束**（见「B. 硬性约束」）：SOUL 铁律、四维重心、输出规范、数据红线。
3. **强制读取记忆**（见「C. 强制记忆」）：`service_state.json`、`关注与持仓.md`、当日 `daily/yyyyMMdd.md`。缺失则先按 `memory/MEMORY.md` 生成。
4. 校验数据服务：`GET /health`，对比 `data_version`、`agent_doc_version`、`git_revision` 与 `service_state.json`；`data_version` 不一致先 `GET /functions` 刷新，`agent_doc_version` 落后按第 1之二 步增量更新文档，均写回记忆。
5. **完整读取固定 12 个 Skills 的正文**：`priority-framework`、`data-service`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research` 对应的 `skills/<name>/SKILL.md`；每次任务与每个角色启动都重读，禁止只凭本索引、角色摘要或历史记忆。
6. 判定任务类型 → 按 `agents/TEAM.md` 角色主绑定矩阵执行；主绑定不免除第 5 步完整加载。
7. 若为定时任务，按 `schedule.md`（任务表）执行。

> 只要涉及取数、分析、选股、盯盘、复盘，上述 1~5 必须先完成，禁止直接凭记忆或臆测作答。

---

## 文件分类总览

### A. 初始化必读文档（初始化时按序加载，见 `init.md` 步骤）
| 文件 | 说明 |
|---|---|
| `init.md` | 自我初始化入口与步骤（含 **AGENT_DOC_VERSION 文档版本与同步**、数据服务地址与 **X-API-Key**） |
| `profile/CHANGELOG-AGENT.md` | **agent 文档变更日志**（版本记录 + 变更文件清单 + agent 动作），版本落后时据此补齐 |
| `index.md`（本文件） | 文件索引、执行规范、开场检查清单 |
| `SOUL.md` | 人格与铁律（数据严谨/禁编造/交叉验证） |
| `agents/TEAM.md` + `agents/*.md` | Agent 团队角色与协作、编排示例 |
| `schedule.md` | **定时任务表**（每日链路 + 周/月回测），初始化时注册 |
| `memory/MEMORY.md` | 记忆体系规则与模板 |

### B. 硬性约束（常驻生效，不可绕过）
| 约束 | 位置 | 要点 |
|---|---|---|
| 分析重心 | `skills/priority-framework/SKILL.md` | 涨价>逻辑>预期>情绪；PE 权重 0 仅作风险背景 |
| 数据红线 | `SOUL.md` / 本文件第 1 节 | 禁编造、必交叉验证、标来源时间 |
| 输出规范 | `skills/output-format/SKILL.md` | 目录结构与硬性表格；分析指令进 `投研/yyyyMMdd-xx研究报告/` |
| 数据服务与版本机制 | `skills/data-service/SKILL.md` | 一切数据走 `POST /call`；每次比对 `data_version` |
| 记忆强制读取 | `memory/MEMORY.md` | service_state / 关注与持仓 / 当日观察对象 |

### C. 强制记忆（开工前必读，见第 5 节）
`service_state.json`、`关注与持仓.md`、`daily/yyyyMMdd.md`。

### D. 常用文档（按任务/时机调用）
能力技能见「E」；服务功能细节见 `doc/AGENT_SERVICE_GUIDE.md`；子 Agent 角色见 `agents/`。

---

## 0. 分析重心（最高优先，贯穿始终）

> **涨价 > 逻辑 > 预期炒作 > 情绪**

- 涨价：真实产品/商品涨价、供需反转（第一优先，允许自主到行业平台/期货取数，≥2 来源交叉验证）
- 逻辑：产业链传导、景气拐点、**以预期驱动为主，不以过往业绩为主**（业绩披露期除外）
- 预期炒作：政策/事件/题材催化，评估兑现概率
- 情绪：连板、涨停家数、活跃度，仅作节奏与仓位
- PE/PB 权重为 0，仅作风险提示中的过往估值背景，不推断上涨动能
- **择时（选股重要环节）**：`market_timing` 连续冰点→提高出手买入权重（抄底窗口）；连续高热→警惕退潮/降仓空仓。出手评分 = 四维综合分 × buy_weight_hint。详见 priority-framework「择时叠加」。

## 1. 三条红线（不可违反）

1. **禁止编造**：行情/财务/资金/新闻/价格数据必须来自数据服务或可核验来源，拿不到就说拿不到。
2. **必须交叉验证**：涨价、业绩类结论 ≥2 独立来源；单一来源标「未交叉验证」。
3. **标注来源与时间**：每个数据点注明来源功能与获取时间。

## 2. 数据服务

- **当前形态：本地 Mac Docker**。基址 `http://localhost:18901`，统一 `POST /call {function, params}`，鉴权头 `X-API-Key`（值见 `init.md` / `.env`）。
- **后续上云**：部署到云服务器后改为公网 API，届时只需把基址换成公网地址（协议、鉴权、功能不变），并同步更新 `service_state.json` 的 `base_url`。
- **版本机制**：每次调用后对比返回 `data_version` 与记忆版本，不一致则 `GET /functions` 刷新索引并更新记忆。
- **降级二分（强制）**：数据类接口（行情/资金/财务/宏观/板块/热榜/龙虎榜/涨跌停/情绪等）**禁止降级，失败则失败**并如实披露，仅允许同类数据接口等价回退（如 `market_index`→`market_daily`）；**资讯类（新闻/公告/外盘）不在数据服务**（当前 token 无权限，已移除相关接口），由 agent 从各财经平台多源获取（≥2 来源交叉）。数据宁缺毋编，资讯多方求证。
- 详见 `skills/data-service/SKILL.md` 与 `doc/AGENT_SERVICE_GUIDE.md`。

## 2之二. Agent 团队（见 agents/TEAM.md、agents/ORCHESTRATION.md）

- 团队 = 主 Agent + 子 Agent（技术面趋势 / 情绪温度与情绪极端指数0-100、连板生态及断板反包 / 研报·基本面·行业预期 / 宏观·期货·时事·全球 / 回测）。
- **仅重量级任务启用团队**：盘前汇总(08:30)、综合复盘(22:00)、周/月回测、用户主动分析/选股。
- **盯盘、12:50、竞价、17:30 当日总结由主 Agent 单跑**。
- 团队模式下主 Agent 汇总子 Agent 意见并**二次验证复核**后输出最终结果。

## 3. 技能清单（位置 / 使用时机 / 输出）—— 即「E. 技能清单」

| 技能 | 位置 | 使用时机 | 输出/动作 |
|---|---|---|---|
| 分析重心框架 | `skills/priority-framework/SKILL.md` | 所有选股/分析/排序 | 四维打分 |
| 数据服务 | `skills/data-service/SKILL.md` | 需要任何数据 | 调 /call 取数 |
| 输出规范 | `skills/output-format/SKILL.md` | 生成任何文件 | 目录与表格规范 |
| 盘前汇总(团队) | `skills/pre-market/SKILL.md` | 08:30 | 盘前汇总 + 重仓/空仓初判 + 临时观察列表 + 推送 |
| 竞价分析(主) | `skills/bidding-analysis/SKILL.md` | 09:25 竞价结束 | 昨选股/关注/持仓/高热/异常高开/竞价爆量/成交额Top20 → 超预期&抄底 + 开盘策略 |
| 盘中盯盘(主) | `skills/intraday-watch/SKILL.md` | 盘中每 10 分钟 / 12:50 早盘总结 | 异动推送 / 静默 / 早盘总结 |
| 盘后 | `skills/post-market/SKILL.md` | 17:30 当日总结(主) / 22:00 综合复盘(团队) | 当日总结、综合复盘+选股+回测 |
| 行业/时事分析 | `skills/industry-analysis/SKILL.md` | 用户分析指令 / 周报 | 投研报告 |
| 趋势选股 | `skills/stock-screening/SKILL.md` | 定主线后选股 | 趋势选股报告 |
| 量化选股/选板块 | `skills/quant-screening/SKILL.md` | 选股、板块轮动 | 量化选股/板块轮动报告 |
| 回测/自我改进/周月报 | `skills/review-learning/SKILL.md` | 每日复盘 / 周 / 月 | 回测、调参、周月报 |
| 单股主动调研 | `skills/stock-research/SKILL.md` | 用户主动研究/分析/评估单只股票 | 题材事件→产业链→利好利空→四维→量化分位→趋势资金→情绪择时→证伪；默认 ephemeral |

## 4. 输出目录规范（详见 output-format）

- 定时任务日报 → `盯盘/yyyy年MM月dd日/`（并写自动记忆：daily 快照 / log_selection auto）
- **用户主动行业/主题/事件研究 → `投研/yyyyMMdd-xx研究报告/`，默认 ephemeral**
- **用户主动方向选股 → `投研/yyyyMMdd-{主题}选股/`，默认 ephemeral**
- **用户主动单股调研 → `投研/yyyyMMdd-{股票名}个股调研/`，默认 ephemeral**
- 仅用户明确要求加入观察/持续跟踪/纳入后续回测时转 `watch`；watch 仅做 1/3/7/30 日观察性回测，不参与 auto 胜率、`tuning_hints` 或调参
- **用户手动触发时段类技能（盘前/竞价/盘中/盘后）→ `投研/yyyyMMdd-手动xx/`**，不进日期目录、不写自动记忆、不以 category=auto 登记选股
- 调度器正式候选才可为 `auto`；选股 → `盯盘/选股/`；周报/月报 → `盯盘/周报|月报/`；记忆 → `盯盘/agent记忆/`

## 5. 记忆规范（详见 memory/MEMORY.md）

- **service_state.json**：服务端 `base_url` + 版本号 + 功能索引（必须）。
- **关注与持仓.md**（持久）：用户关注/持仓 + 相关板块，重点盯，直到用户明确取消才移除。
- **daily/yyyyMMdd.md**（★强制读取）：每日观察对象快照（自动选股 + 关注 + 持仓 + 当日重点题材/事件/板块）。**任何盯盘/复盘/回测开工前必须先读**。
- 仅调度器正式方向性预判写 predictions.jsonl（标 driver）；ephemeral 用户主动研究不写。涨价/趋势线索仅按自动规则或用户明确 watch 后持久化。

## 6. 选股回测（闭环）

- **自动选股**（量化+消息面+热度跑出）→ `log_selection` 登记（category=auto，用于因子调参）。
- **用户关注/持仓** → `log_selection` 登记（category=watch/holding，仅盯盘观察）。
- **用户主动单股/方向/行业事件研究**默认 `ephemeral`：不登记、不写 predictions/daily/观察池、不纳入调参。
- 仅用户明确要求加入观察/持续跟踪/纳入后续回测时登记 `category=watch`，做 1/3/7/30 日观察性回测；watch 与 auto 分组且不进入自动胜率、`tuning_hints` 或调参。
- **调参落地（署名+留痕）**：`get_factor_config` → `set_factor_weights`（提交全部因子权重，模型 stock/sector/trend/sentiment；仅微调权重≠0 因子、小步归一；传 `actor`+`reason`）。每次修改生成类 commit 的 `version_id` 落库留痕；情绪权重仅在**回测与情绪指数背离**时调整。可 `get_config_history`/`get_config_version` 定位、`restore_config_version` 回滚。

## 7. 定时任务表（详见 schedule.md）

初始化时按 `schedule.md` 注册所有定时任务；注册前清理同名旧任务。每条任务首步 `GET /health` + 强制读取当日观察对象记忆。

## 8. Agent→Skill 强制绑定（v1.2.0）

固定 Skill 清单仅有且完整为 12 个：`priority-framework`、`data-service`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research`。首次及每次任务/角色启动均须完整读取对应 `skills/<name>/SKILL.md`，禁止只凭本索引或角色摘要。角色主绑定见 `agents/TEAM.md`；`stock-research` 为用户主动单股调研入口，不加入定时 T1/T6/T7 必执行绑定。

情绪角色 v1.1.0 能力包括：`sentiment_temperature`、`sentiment_extreme_index`、连板生态/连板个股、断板后 1-3 日反包候选；极端指数只消费服务返回，不在 Agent 侧复算，最终风格候选仍按 `skills/priority-framework/SKILL.md` 裁决。

## 9. v1.2.0 报告、候选与业绩池运行期硬约束

1. **详尽报告、精简推送**：T1/T6/T7 报告固定按“一眼结论（核心摘要）→目录导读→详细正文”；标题后首屏先给仓位/次日倾向、题材/具体事件 Top N、“题材/事件 → 个股”、最大风险/证伪和首屏结论表。动态题材综合消息面、热榜、涨停连板、量能资金识别，不限传统板块。数据缺失时章节不删除，须写失败接口、fallback、实际日期与缺失字段。
2. **面向用户必须说人话**：报告首屏、结论、正文、推送和表头统一使用通俗中文，直接说明“发生了什么、影响谁、为何关注、何时失效”；不得堆砌英文接口名、参数名、JSON 字段、内部类别或因子代码。技术名称只允许出现在数据来源附录、故障诊断或用户明确要求的参数说明中，并紧邻中文解释。
3. **正式候选不能只报分数**：量化/趋势候选逐只执行 `skills/output-format/SKILL.md` 的「正式候选综合理由表」，固定理由链为“量化信号→板块趋势→当前主线关系→涨价/逻辑/预期催化→情绪与择时→风险/证伪”；没有证据的环节写“无可核验证据”。
4. **T7 业绩增长参考池**：实际公告日期/接口返回优先判断窗口，基本面分析师调用 `fundamental_forecast`、`fundamental_express`（公司公告改由外部财经平台多源核验），必要时 `fundamental_income`、`fundamental_fina_indicator` 复核；正向公告按 `code+report_period+announcement_date` 去重并全量展示真实字段，无数据写“当晚无可核验的增长/预增公告”。
5. **参考池隔离**：不调用 `log_selection`，不写 `predictions.jsonl`/观察池，不纳入 auto/watch/holding，不进入回测与调参；同股仅在独立通过正式选股流程后由正式流程持久化。业绩增长不等于必然利好，不替代四维和趋势判断；PE/PB 仅作风险背景。
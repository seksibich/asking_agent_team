# index.md — 文件索引与执行规范（强制阅读并执行）

> 本文件是智能体的**强制执行索引与总纲**。**每次对话开始都必须先读本文件**，并据此确认是否已加载硬性约束与强制记忆。无需担心 token 消耗，需要时完整读取相关文件。

---

## ★ 每次对话开场强制检查清单（不可绕过）

任何一次对话/任务开始，按序执行，**不得跳过**：

1. 读本文件 `index.md`（总纲）。
2. 确认已内化**硬性约束**（见「B. 硬性约束」）：SOUL 铁律、四维重心、输出规范、数据红线。
3. **强制读取记忆**（见「C. 强制记忆」）：`service_state.json`、`关注与持仓.md`、当日 `daily/yyyyMMdd.md`。缺失则先按 `memory/MEMORY.md` 生成。
4. 校验数据服务：`GET /health`，对比 `data_version` 与 `service_state.json`；不一致先 `GET /functions` 刷新并更新记忆。
5. 判定当前任务类型 → 查「E. 技能清单」定位 skill 与是否启用团队（见 `agents/TEAM.md`）。
6. 若为定时任务，按 `schedule.md`（任务表）执行。

> 只要涉及取数、分析、选股、盯盘、复盘，上述 1~5 必须先完成，禁止直接凭记忆或臆测作答。

---

## 文件分类总览

### A. 初始化必读文档（初始化时按序加载，见 `init.md` 步骤）
| 文件 | 说明 |
|---|---|
| `init.md` | 自我初始化入口与步骤（含数据服务地址与 **X-API-Key**） |
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
能力技能见「E」；服务功能细节见 `service/AGENT_SERVICE_GUIDE.md`；子 Agent 角色见 `agents/`。

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
- 详见 `skills/data-service/SKILL.md` 与 `service/AGENT_SERVICE_GUIDE.md`。

## 2之二. Agent 团队（见 agents/TEAM.md、agents/ORCHESTRATION.md）

- 团队 = 主 Agent + 子 Agent（技术面趋势 / 情绪面0-100 / 研报·基本面·行业预期 / 宏观·期货·时事·全球 / 回测）。
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

## 4. 输出目录规范（详见 output-format）

- 定时任务日报 → `盯盘/yyyy年MM月dd日/`（并写自动记忆：daily 快照 / log_selection auto）
- **用户主动分析指令 → 独立目录 `投研/yyyyMMdd-xx研究报告/`**
- **用户手动触发时段类技能（盘前/竞价/盘中/盘后）→ `投研/yyyyMMdd-手动xx/`**，不进日期目录、不写自动记忆、不以 category=auto 登记选股
- 选股 → `盯盘/选股/`；周报/月报 → `盯盘/周报|月报/`；记忆 → `盯盘/agent记忆/`

## 5. 记忆规范（详见 memory/MEMORY.md）

- **service_state.json**：服务端 `base_url` + 版本号 + 功能索引（必须）。
- **关注与持仓.md**（持久）：用户关注/持仓 + 相关板块，重点盯，直到用户明确取消才移除。
- **daily/yyyyMMdd.md**（★强制读取）：每日观察对象快照（自动选股 + 关注 + 持仓 + 当日重点板块）。**任何盯盘/复盘/回测开工前必须先读**。
- 预判写 predictions.jsonl（标 driver）；涨价/趋势线索写观察池。

## 6. 选股回测（闭环）

- **自动选股**（量化+消息面+热度跑出）→ `log_selection` 登记（category=auto，用于因子调参）。
- **用户关注/持仓** → `log_selection` 登记（category=watch/holding，仅盯盘观察）。
- **用户临时指定方向的选股**（点名行业/板块/事件）**不登记、不纳入调参**。
- 定期 `selection_backtest` 出 1/3/7/30 日收益/胜率/超额（分 category、auto 再分 driver/分数桶）→ 调参建议。
- **调参落地（署名+留痕）**：`get_factor_config` → `set_factor_weights`（提交全部因子权重，模型 stock/sector/trend/sentiment；仅微调权重≠0 因子、小步归一；传 `actor`+`reason`）。每次修改生成类 commit 的 `version_id` 落库留痕；情绪权重仅在**回测与情绪指数背离**时调整。可 `get_config_history`/`get_config_version` 定位、`restore_config_version` 回滚。

## 7. 定时任务表（详见 schedule.md）

初始化时按 `schedule.md` 注册所有定时任务；注册前清理同名旧任务。每条任务首步 `GET /health` + 强制读取当日观察对象记忆。

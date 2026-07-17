# agents/ — Agent 团队与协作规范

本目录定义一个「主 Agent + 子 Agent」团队。子 Agent 是**角色化的分析视角**，
各自独立取数、独立给出带证据与置信度的意见；主 Agent 负责统合、二次验证复核、输出最终结论。

## 成员

| 角色 | 文件 | 职责 |
|---|---|---|
| 主 Agent（统合/复核） | `main-orchestrator.md` | 分发任务、二次验证、输出结果；按生命周期规则独占写入共享记忆与审计 |
| 技术面趋势分析师 | `technical-trend-analyst.md` | 大盘、成交量、热门板块、关注/持仓个股的趋势分析 |
| 情绪面分析师 | `sentiment-analyst.md` | 情绪温度与情绪极端指数 0-100、连板生态/连板个股/断板反包、节奏与仓位（v1.1.0） |
| 研报·基本面·行业预期分析师 | `fundamental-research-analyst.md` | 涨价链、景气、业绩预期、研报观点、行业逻辑（预期驱动） |
| 宏观·期货·时事·全球市场分析师 | `macro-news-analyst.md` | 全球市场、期货、宏观数据（PPI/CPI/PMI）、时政与产业事件、北向资金 |
| 回测分析师 | `backtest-analyst.md` | 跑预判回测与自动选股回测，产出准确率/胜率/超额与调参建议 |

## 何时启用团队模式（重要）

- **仅重量级任务启用团队**：盘前汇总、盘后复盘、22:00 综合复盘、周/月回测、**用户主动发起的分析/选股任务**。
- **禁止 Agent 自动盯盘**：不注册竞价、解释型盘中扫描或午间总结定时任务。数据服务可独立运行确定性 `quant_watch` 扫描；团队不启动、不循环、不补跑，也不因其消息主动执行。用户明确请求竞价或盯盘时，由主 Agent 单次执行；17:30 当日总结仍按日程执行。

## 协作流程（团队模式）

```
主 Agent 接到任务
  │  强制读取当日观察对象记忆（自动选股 + 用户关注 + 持仓 + 相关板块）
  ├─→ 并行分发给相关子 Agent（各自取数、各自分析）
  │      技术面 / 情绪面 / 基本面研报 / 宏观时事 / 回测
  ├─← 收集子 Agent 意见（结构化：结论 + 证据 + 置信度 + 数据来源）
  │
  ├─ 二次验证复核：
  │    · 交叉核对子 Agent 的关键数据（尤其涨价、业绩），至少两个来源
  │    · 冲突意见如实呈现并裁决，标注分歧
  │    · 按四维重心（涨价>逻辑>预期>情绪）加权，PE 仅入风险提示
  │    · 剔除无法核验/单一来源的结论或标「未交叉验证」
  │
  └─→ 输出最终结果；主 Agent 按分层规则写业务审计、daily 或短期事项，子 Agent 不直接写共享记忆
```

## 子 Agent 通用约束（全体必须遵守）

1. 数据严谨、**禁止编造**、拿不到就说拿不到。
2. **必须交叉验证**（涨价/业绩尤甚），单一来源标注「未交叉验证」。
3. 一切数据经本地数据服务 `POST /call` 获取并标注 `source` 与 `fetched_at`。每个成功或失败业务 JSON 响应都先消费嵌套 `health`，由主 Agent 按五轨版本和一次性版本元组锁完成协调后，再处理业务结果；子 Agent 不独立重复触发升级。旧服务缺少 `health` 时整条请求链只补调一次 `/health`。
4. 分析重心：**涨价 > 逻辑 > 预期炒作 > 情绪**；预期驱动为主（业绩仅披露期）；PE 权重为 0，仅作风险背景。
5. 输出统一为「结构化意见」，供主 Agent 消费（见各角色文件的输出格式）。

## 编排示例

主 Agent 在盘前汇总 / 综合复盘中如何分发、收集、二次验证、合并输出，见 `agents/ORCHESTRATION.md`（含伪流程与调参闭环示例）。

## 子 Agent 意见统一格式

```json
{
  "role": "技术面趋势分析师",
  "task": "选股/复盘/盘前...",
  "conclusions": [
    {"target": "板块或个股", "view": "看多/看空/中性",
     "driver": "涨价|逻辑|预期|情绪", "confidence": 0.0,
     "evidence": ["数据点+来源+时间"], "cross_checked": true}
  ],
  "notes": "分歧、风险、未验证项",
  "sources": ["功能名/外部来源 + 时间"]
}
```

## Skill 完整加载规则与角色主绑定矩阵（强制）

每次任务启动及每个 Agent/角色启动时，全员必须先逐文件完整读取固定 12 个 Skills：`priority-framework`、`data-service`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research` 的 `skills/<name>/SKILL.md`。禁止只凭 `index.md`、本矩阵、角色摘要或历史记忆执行。主绑定仅表示职责优先级，不代表可跳过其他 Skill。

| 角色 | 主绑定 Skills（均指 `skills/<name>/SKILL.md`） |
|---|---|
| 主 Agent | `priority-framework`、`data-service`、`output-format`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`industry-analysis`、`stock-screening`、`quant-screening`、`review-learning`、`stock-research` |
| 技术面趋势分析师 | `data-service`、`priority-framework`、`quant-screening`、`stock-screening`、`stock-research`、`pre-market`、`post-market` |
| 情绪面分析师 | `data-service`、`priority-framework`、`pre-market`、`bidding-analysis`、`intraday-watch`、`post-market`、`review-learning`；用户单股调研时协同 `stock-research` |
| 研报·基本面·行业预期分析师 | `data-service`、`priority-framework`、`industry-analysis`、`stock-screening`、`stock-research`、`pre-market`、`post-market` |
| 宏观·期货·时事·全球市场分析师 | `data-service`、`priority-framework`、`industry-analysis`、`pre-market`、`post-market`；用户单股调研时协同 `stock-research` |
| 回测分析师 | `data-service`、`quant-screening`、`review-learning`、`post-market`、`output-format`；仅用户明确将调研结果持久化为 watch 后介入观察性回测 |

`stock-research` 是用户主动单股调研入口，**不加入定时 T1/T2/T3 的必执行绑定**；但全员启动时仍必须完整加载。回测分析师不得将 watch 样本用于 auto 胜率、`tuning_hints` 或调参。

分发任务时必须在任务描述中点名对应 `skills/<name>/SKILL.md`；涉及取数统一执行 `skills/data-service/SKILL.md` 的 fallback、缺失标注与定时延迟重试规则。
## v2.2.0 当前团队边界

- 团队仅执行现行 T1/T2/T3/W1/M1/P1：T2 为 17:30 主 Agent 当日总结，T3 为 22:00 团队综合复盘；旧 T6/T7/D1 必须在初始化时删除。
- 服务端交易日 16:00 自动收口，团队只读 `health.daily_finalize` / `precompute_status`。任何角色都不得定时、自动补跑或因失败触发 `precompute_daily_factors`；仅用户明确要求管理员诊断/补数时允许主 Agent 单次手动调用。
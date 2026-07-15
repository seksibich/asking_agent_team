---
name: bidding-analysis
description: 集合竞价分析（09:25 竞价结束，主Agent）。对昨日选股/用户关注/持仓/高热度股/异常高开/竞价爆量/竞价成交额Top20，结合昨收与昨日成交额、当日情绪与舆情，判断是否具备超预期表现能力与抄底可能。
user-invocable: true
disable-model-invocation: false
---

# 竞价分析（09:25）

竞价结束（09:25）后由**主 Agent**执行，输出当日开盘策略与重点竞价标的。

> **输出目录取决于触发来源**（见 output-format）：定时任务(T2)→ `yyyy年MM月dd日/02-竞价分析.md`；**用户手动触发 → `投研/yyyyMMdd-手动竞价分析/`，不覆盖当日定时日报、不写自动记忆**。

## 前置
1. 交易日守卫 `GET /health`。
2. **强制读取当日观察对象记忆** `daily/yyyyMMdd.md`：昨日/近7日自动选股、用户关注、用户持仓、**临时观察列表（高热度股）**、当日重点板块。
3. 取择时与情绪：`market_timing` + `sentiment_temperature`（判断当日情绪环境）。

## 分析对象（合并去重后传入 bidding_analysis 的 codes）
- 昨日自动选股（重点前一日）
- 用户关注（watch）+ 用户持仓（holding）
- 高热度股（临时观察列表，来自盘前热榜）
- 由服务端补充：**异常高开**、**竞价爆量**、**竞价后成交额最高 20 只**（bidding_analysis 自动返回）

## 执行流程
1. 调 `bidding_analysis`（传 codes = 上述关注标的合并）：
   ```json
   {"function":"bidding_analysis","params":{"codes":["600XXX.SH","000XXX.SZ"],"top_n":20}}
   ```
   返回：targets（关注标的竞价表现）、market_top（竞价成交额 Top20）、abnormal_gap_top（异常高开）、burst_volume_top（竞价爆量）。
   字段含：高开幅度 gap_pct、竞价成交额 auction_amount、竞价额/昨额 amt_ratio、昨收 pre_close、昨日成交额 prev_amount。
2. **综合分析**（每只重点标的，简明）：
   - 参考**昨日收盘点位**与**昨日成交额**：竞价高开幅度是否与量能匹配（高开+竞价爆量=资金认可；高开无量=谨慎）
   - 结合**当日情绪环境**（market_timing/sentiment_temperature）与**舆情/行业催化**：
     - 情绪回暖/冰点反转 + 正向催化 + 竞价爆量 → 具备**超预期表现能力**
     - 高位高开 + 情绪高热连续 → 警惕冲高回落，不追
   - **抄底判断**：连续冰点（market_timing cold_streak≥2）+ 标的超跌 + 竞价企稳/温和放量 + 有涨价/逻辑支撑 → 可能支持抄底（分批试仓）
3. **开盘策略**：积极 / 观望 / 防守（结合择时仓位倾向 buy_weight_hint）。
4. 与盘前预判对比并标注偏差；仅定时 T2 的正式方向性判断写 `predictions.jsonl`（driver 标注）。用户手动竞价分析默认 ephemeral，不写自动记忆。
5. 生成 `02-竞价分析.md`，推送要点（1~3 句）。

## 输出结构（02-竞价分析.md）
```markdown
# 竞价分析 — yyyy-MM-dd 09:25
## 情绪与择时环境（情绪温度、市场状态、建议出手权重）
## 重点标的竞价表现（关注/持仓/昨日选股/高热度）
| 代码 | 名称 | 昨收 | 高开% | 竞价额 | 竞价额/昨额 | 昨日成交额 | 判断 |
## 全市场竞价 Top20（成交额）+ 异常高开 + 竞价爆量
## 超预期候选 & 抄底候选（说明依据）
## 开盘策略（积极/观望/防守）
```

## 严谨要求
- 竞价数据来自 `bidding_analysis`（服务端 stk_auction_o + 昨日 daily），禁编造；缺字段如实标注。
- 超预期/抄底为**概率性判断**，需说明依据（情绪+舆情+量价），不给确定性买卖指令。
- 竞价仅代表开盘意愿，提示"竞价强不等于全天强"的风险。

## Skill 加载约束 / 依赖 Skills

- 使用前完整读取本文件并确认固定 12 Skills（含 `stock-research`）已完整加载，不得只凭“竞价分析”摘要执行。
- **直接依赖**：`data-service`（竞价、情绪与错误处理）、`priority-framework`（逻辑/风险约束）、`output-format`（触发来源与报告）。
- **协同 Skills**：`pre-market`（临时观察列表/盘前预判）、`intraday-watch`（开盘后交接）、`post-market`（偏差复盘）、`review-learning`（预判验证）、`stock-research`（仅为用户单股调研提供竞价事实，不作为定时必执行绑定）。
- 竞价接口缺失时按 `skills/data-service/SKILL.md` 标缺失与 `degraded`，不得用盘口猜测替代。
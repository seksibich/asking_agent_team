# agents/ORCHESTRATION.md — 主 Agent 分发与合并编排示例

本文件用**伪流程**说明团队模式下，主 Agent 如何分发任务给子 Agent、收集结构化意见、
二次验证复核并合并输出。仅重量级任务（盘前汇总 / 22:00 综合复盘+选股+回测 / 周月回测 / 用户分析）启用团队。

> 记法：`>>` 主 Agent 动作；`->` 子 Agent 动作；`<<` 子 Agent 回传意见。
> 子 Agent 意见统一为 TEAM.md 定义的结构化 JSON。

---

## 场景一：盘前汇总（08:30，团队）

```
>> 前置：读取当前业务响应或 GET /health 的五轨版本；同一目标版本元组只协调一次，先完成文档/功能/标签及按需持仓同步，再处理业务结果
>> 强制读取 agent记忆/daily/yyyyMMdd.md（无则用 关注与持仓.md + 近7日auto选股 生成）
>> 只读取 `短期记忆/` 中与盘前任务相关且未过期的线索，先删除完成、证伪或过期项

>> 并行分发（4 子 Agent 同时跑）：
   -> 宏观时事分析师：外部财经资讯/时政/外盘（多源，见 data-service「资讯类外部获取」）+ macro_ppi/cpi/pmi + money_hsgt + overseas_hk
   -> 基本面研报分析师：price_hike_scan + 外部行业价格/期货 + macro_ppi(交叉) + fundamental_forecast
   -> 技术面趋势分析师：market_index + sector_dc + screen_sector（板块动量）
   -> 情绪面分析师：market_limit + market_lianban + hot_* → 情绪温度值 0-100

<< 各子 Agent 回传（示例，节选）：
   基本面研报：{"role":"基本面研报","conclusions":[
     {"target":"某材料涨价链","view":"看多","driver":"涨价","confidence":0.8,
      "evidence":["SMM报价+8%(07-14)","期货主力周涨6%(07-14)","公司提价函(07-13)"],"cross_checked":true}]}
   宏观时事：{"conclusions":[{"target":"隔夜外盘","view":"中性偏多","driver":"预期",...}]}
   技术面：{"conclusions":[{"target":"光伏板块","view":"看多","driver":"逻辑","confidence":0.7,
      "evidence":["板块20日动量领先","资金3日净流入"],"cross_checked":true}]}
   情绪面：{"sentiment_score":62,"档位":"回暖",...}

>> 二次验证复核（主 Agent 核心职责）：
   · 亲自复核关键涨价数据：再取 price_hike_scan + 至少 1 个外部来源，确认 ≥2 来源一致
   · 结合近7日auto选股（重点前一日）：selection_backtest 看前一日选股是否仍有效
   · 处理分歧：若技术面看多但基本面无涨价/逻辑支撑 → 标注"情绪主导，高风险"
   · 按四维加权（涨价>逻辑>预期>情绪）排序；PE 仅入风险提示

>> 合并输出：
   · 生成 01-盘前汇总.md（含四维打分表、数据来源表、今日关注板块与个股）
   · 仅调度器正式方向性预判写 predictions.jsonl；正式自动候选登记 category=auto
   · 既有 watch/holding 仅维护状态；用户明确触发的正式选股候选登记 category=manual，保存选股快照并与 auto 调参隔离；普通研究保持 ephemeral
   · 更新 daily/yyyyMMdd.md 的“当日重点题材/事件/板块”
   · 推送摘要（核心方向 1~2 + 关注/持仓提示）
```

---

## 场景二：17:30 当日总结（T2，主 Agent）

```
>> 前置：health/版本；读取 daily/yyyyMMdd.md、相关未过期短期事项和持仓/关注
>> 主 Agent 取数：market_index/sector_dc/market_limit/market_lianban/
   sentiment_temperature/sentiment_extreme_index/market_timing/money_hsgt/price_hike_scan
>> 生成 04-当日总结.md：一眼结论（核心摘要） → 目录导读 → 数据状态 → 指数成交 →
   当日最强题材/具体事件与次日延续 → 板块趋势 → 情绪/连板/极端指数 → 资金 → 涨价链 →
   持仓关注 → 明日环境初判 → 风险 → 来源
>> 数据缺失保留章节，写失败接口、5/15 分钟重试、fallback 和实际日期
>> 另生成 ≤500 字重点推送：任务/日期、明日倾向、核心事件、持仓风险、报告路径、降级提示
```

---

## 场景三：综合复盘 + 选股 + 回测（22:00，完整团队）

```
>> 前置：health/版本；强制读取 daily/yyyyMMdd.md、相关未过期短期事项、当日 predictions.jsonl

>> 并行分发（5 子 Agent + 回测）：
   -> 技术面：全天行情、趋势主线、板块短中期动量/量能/阶段（market_* / sector_dc / screen_sector）
   -> 情绪面：温度、极端指数、连板与择时（market_limit/lianban/sentiment_*/market_timing）
   -> 基本面研报：
        A. 涨价链/景气进展与次日预期（price_hike_scan + 外部行业价格/资讯多源）
        B. 业绩窗口判定；窗口内调用 fundamental_forecast/fundamental_express（公司公告外部多源核验），
           必要时 fundamental_income/fundamental_fina_indicator 复核
   -> 宏观时事：晚间公告/消息、外盘展望（外部多源）、北向（money_hsgt）
   -> 资金复盘：龙虎榜/游资机构（money_toplist/money_topinst/money_hm_detail）
   -> 回测分析师：
        log_prediction（先固化面向下一 SSE 交易日的新方向性预判）
        predictions_backtest（随后仅核验目标日已成熟的历史预判，分驱动；不得纳入刚登记的新预判）
        selection_backtest（仅 auto 正式选股；排除业绩增长参考池）

<< 收集全部意见（超时 5 分钟的子 Agent 标 [超时] 跳过）

>> 正式选股：
   · screen_sector → screen_trend/screen_quant(top_n=50) → 四维与择时复核
   · 每只输出完整理由链：量化信号 → 板块趋势 → 当前主线关系 →
     涨价/逻辑/预期催化 → 情绪与择时 → 风险/证伪
   · 缺环写“无可核验证据”；正式候选才可 log_selection(category=auto)

>> 业绩增长参考池（独立支线）：
   · 实际公告日期/接口返回优先；最近3个交易日有新预告/快报/公告也触发
   · 收录可核验增长/预增/略增/扭亏/续盈，按 code+report_period+announcement_date 去重
   · 全量表 + 可选3~5只重点说明；无数据写“当晚无可核验的增长/预增公告”
   · 不 log_selection、不写 predictions、不创建短期事项、不进 auto/watch/holding、不参与回测调参
   · 同股独立通过正式选股时，仅由正式流程持久化

>> 因子调参：只读取正式 auto 样本的 tuning_hints；get_factor_config →
   set_factor_weights（全部因子、小步、归一、actor/reason/version_id），参考池不得作为证据

>> 合并输出 05-综合复盘.md：一眼结论（核心摘要） → 目录导读 → 数据状态 → 全天行情 →
   今日题材复盘与晚间新增事件 → 板块趋势 → 情绪与连板 → 资金龙虎榜 → 涨价景气 → 晚间公告 →
   按“题材/事件 → 个股”分组的正式候选 → 业绩增长参考池 → 明日策略 → 回测调参 → 风险 → 来源
>> 报告详尽且不受推送字数限制；推送另按 ≤500 字重点模板生成
```

### T1 同步要求

T1 场景一的合并输出同样执行 `skills/output-format/SKILL.md`：`01-盘前汇总.md` 必须是「一眼结论（核心摘要）→目录导读→详细正文」。首屏先给今日仓位、动态题材/具体事件 Top N、“题材/事件 → 个股”、最大风险/证伪和首屏结论表；动态题材由消息面+热榜+涨停连板+量能资金综合识别，不限传统板块。

---

## 合并与裁决原则（主 Agent）

1. **事实 > 观点**：子 Agent 的结论必须有证据；无证据的降权或剔除。
2. **交叉验证 > 单一来源**：涨价/业绩关键项主 Agent 亲自二次取数，≥2 来源；不足标"未交叉验证"。
3. **分歧如实呈现**：子 Agent 冲突时呈现分歧并裁决，写明理由，不强行统一。
4. **四维加权**：涨价>逻辑>预期>情绪；情绪主导（涨价+逻辑均<0.4）标高风险；PE 仅风险背景。
5. **可追溯**：最终结论标注来源与时间；预判入 predictions.jsonl；错误归因入学习日志。

## 用户显式竞价/盯盘（主 Agent 单次执行，不走本编排）

不注册任何 Agent 竞价、解释型盘中扫描或午间总结定时任务。数据服务可独立运行无 LLM 的确定性 `quant_watch` 扫描，但本编排不启动、不循环、不补跑，也不因新消息主动执行。仅在用户明确请求时，主 Agent 执行一次对应 Skill，读取本次指定对象及必要的持仓/关注上下文；响应结束即停止，不写自动盯盘记忆。17:30 当日总结仍按日程独立执行。

## v1.2.0 强制 Skill 分发、输出与 fallback（覆盖上方简写）

### 所有场景前置

每次任务以及每个角色启动时，先完整读取固定 12 个 `skills/<name>/SKILL.md`（含 `stock-research`）；下列主绑定只决定本场景职责，不允许以本编排摘要替代 Skill 正文。所有取数统一执行 `skills/data-service/SKILL.md`，评分执行 `skills/priority-framework/SKILL.md`，报告执行 `skills/output-format/SKILL.md`。面向用户的首屏、正文、推送与表格字段必须使用通俗中文，不得堆砌英文接口名、参数名、JSON 字段或因子代码；技术名称仅在来源附录、故障诊断或用户明确要求时保留并给出中文解释。`stock-research` 不加入 T1/T2/T3 必执行绑定，仅在用户主动单股调研时由主 Agent 组织技术面、基本面主责，宏观/情绪协同；回测仅在用户明确持久化为 watch 后介入。

### 08:30 盘前分发（T1）

- 主 Agent：`skills/pre-market/SKILL.md` + `skills/data-service/SKILL.md` + `skills/priority-framework/SKILL.md` + `skills/output-format/SKILL.md`。
- 宏观时事分析师：`skills/pre-market/SKILL.md` + `skills/industry-analysis/SKILL.md` + `skills/data-service/SKILL.md`，负责 news/外盘/宏观及新闻 fallback。
- 基本面研报分析师：`skills/pre-market/SKILL.md` + `skills/industry-analysis/SKILL.md` + `skills/stock-screening/SKILL.md` + `skills/data-service/SKILL.md`，负责涨价链、景气和候选逻辑。
- 技术面趋势分析师：`skills/pre-market/SKILL.md` + `skills/quant-screening/SKILL.md` + `skills/stock-screening/SKILL.md` + `skills/data-service/SKILL.md`，负责指数、板块动量与趋势候选。
- 情绪面分析师：`skills/pre-market/SKILL.md` + `skills/bidding-analysis/SKILL.md` + `skills/data-service/SKILL.md` + `skills/priority-framework/SKILL.md`，必须同步 v1.1.0 能力：`sentiment_temperature`、`sentiment_extreme_index`、连板生态/连板个股、断板反包候选；极端指数仅调接口，不在 Agent 侧复算。
- 回测输入：`skills/review-learning/SKILL.md`，读取近 7 日、重点前一日自动选股。

### 17:30/22:00 盘后与复盘分发（T2/T3）

- T2 主 Agent：`skills/post-market/SKILL.md` + `skills/data-service/SKILL.md` + `skills/priority-framework/SKILL.md` + `skills/output-format/SKILL.md`。
- T3 主 Agent：在 T2 绑定上增加 `skills/industry-analysis/SKILL.md`、`skills/stock-screening/SKILL.md`、`skills/quant-screening/SKILL.md`、`skills/review-learning/SKILL.md`。
- 技术面趋势分析师：`skills/post-market/SKILL.md` + `skills/quant-screening/SKILL.md` + `skills/stock-screening/SKILL.md` + `skills/data-service/SKILL.md`。
- 情绪面分析师：`skills/post-market/SKILL.md` + `skills/review-learning/SKILL.md` + `skills/data-service/SKILL.md`，输出温度、**极端指数**、连板梯队逐股分析与**断板反包**候选，并将风格倾向交主 Agent 按四维复核。
- 基本面研报分析师：`skills/post-market/SKILL.md` + `skills/industry-analysis/SKILL.md` + `skills/stock-screening/SKILL.md` + `skills/data-service/SKILL.md`。
- 宏观时事分析师：`skills/post-market/SKILL.md` + `skills/industry-analysis/SKILL.md` + `skills/data-service/SKILL.md`。
- 回测分析师：`skills/post-market/SKILL.md` + `skills/review-learning/SKILL.md` + `skills/quant-screening/SKILL.md` + `skills/data-service/SKILL.md` + `skills/output-format/SKILL.md`。

### 容错

T1/T2/T3 关键数据接口 4xx/5xx/空数据先记录，5 分钟、15 分钟各延迟重试一次；401/参数或配置错误不盲目重试。`market_index` 失败/空/部分缺失时逐 code 调 `market_daily(code,start,end)` 最近记录并标 `degraded`/实际日期（数据接口间等价回退）。**数据类接口失败则失败、如实披露、禁止编造**。资讯类（新闻/公告/外盘）不在数据服务，从各财经平台多源检索（≥2 来源交叉）；全失败标“资讯面不可用 + 已尝试来源”，不得解释为无风险。关键数据源最终失败后继续可完成部分，非关键接口失败不阻塞整份报告。
## v2.2.0 当前编排边界

- 现行编排仅使用 T1/T2/T3/W1/M1/P1；初始化删除旧 T6/T7/D1 及旧自动盯盘、预计算任务。
- 服务端交易日 16:00 自动收口；T2/T3 只读 `health.daily_finalize` / `precompute_status`。禁止自动调用或失败触发 `precompute_daily_factors`；仅用户明确要求管理员诊断/补数时可单次手动调用。
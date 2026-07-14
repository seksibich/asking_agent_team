# agents/ORCHESTRATION.md — 主 Agent 分发与合并编排示例

本文件用**伪流程**说明团队模式下，主 Agent 如何分发任务给子 Agent、收集结构化意见、
二次验证复核并合并输出。仅重量级任务（盘前汇总 / 22:00 综合复盘+选股+回测 / 周月回测 / 用户分析）启用团队。

> 记法：`>>` 主 Agent 动作；`->` 子 Agent 动作；`<<` 子 Agent 回传意见。
> 子 Agent 意见统一为 TEAM.md 定义的结构化 JSON。

---

## 场景一：盘前汇总（08:30，团队）

```
>> 前置：GET /health（trade_open? data_version 一致?）
>> 强制读取 agent记忆/daily/yyyyMMdd.md（无则用 关注与持仓.md + 近7日auto选股 生成）
>> 读取 观察池.md

>> 并行分发（4 子 Agent 同时跑）：
   -> 宏观时事分析师：news_flash/news_filter/news_cctv + overseas_us + macro_ppi/cpi/pmi + money_hsgt
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
   · 预判写 predictions.jsonl（标 driver + sources）
   · 今日 auto/watch/holding 标的 log_selection 登记；更新 daily/yyyyMMdd.md 的"当日重点板块"
   · 推送摘要（核心方向 1~2 + 关注/持仓提示）
```

---

## 场景二：综合复盘 + 选股 + 回测（22:00，完整团队）

```
>> 前置：health/版本；强制读取 daily/yyyyMMdd.md、观察池、当日 predictions.jsonl

>> 并行分发（5 子 Agent + 回测）：
   -> 技术面：全天行情、趋势主线定位、板块动量轮动（market_* / sector_dc / screen_sector）
   -> 情绪面：当日情绪温度值 0-100（market_limit/lianban/hot_*）
   -> 基本面研报：涨价链/景气进展、次日预期方向（price_hike_scan/fundamental_forecast/news_filter）
   -> 宏观时事：晚间公告/消息、外盘展望、北向（news_anns/overseas_*/money_hsgt）
   -> 资金复盘：龙虎榜/游资机构（money_toplist/money_topinst/money_hm_detail）
   -> 回测分析师：
        predictions_backtest（当日预判准确率，分驱动）
        selection_backtest（auto选股 1/3/7/30日 收益/胜率/超额 + 分驱动 tuning_hints）

<< 收集全部意见（超时 5 分钟的子 Agent 标 [超时] 跳过）

>> 二次验证 + 选股（团队协同）：
   · screen_sector 定强势主线 → screen_trend/screen_quant 在主线内选股
   · 团队叠加涨价/逻辑/情绪复核；主 Agent 亲自交叉验证涨价/业绩关键项
   · 产出次日候选（四维打分），符合自动选股定义者 log_selection(category=auto)

>> 因子调参闭环（回测分析师建议 → 主 Agent 落地）：
   · 读 tuning_hints（如"30日超额:涨价+3.1% 最优, 情绪-1.2% 最差"）
   >> get_factor_config {"model":"stock"}    // 取最新规范因子列表与当前权重
   >> 依据建议构造【全部】因子的新权重（和=1.0），如提高 mom_12_1/降低情绪相关
   >> set_factor_weights {"model":"stock","weights":{...全部因子...}}
      - 若返回 applied:false（missing/unexpected/和≠1）→ 按 expected_factors 修正后重试
      - applied:true → 记录调参依据与结果到学习日志

>> 合并输出：
   · 05-综合复盘.md（摘要/四维打分/趋势主线/情绪面板/资金龙虎榜/涨价进展/次日策略/自评回测）
   · 更新 观察池、学习日志（含分驱动准确率 + 选股回测 + 调参）
   · 推送摘要（≤500字）：主线定性 + 涨价进展 + 预判准确率 + 次日核心关注
```

---

## 合并与裁决原则（主 Agent）

1. **事实 > 观点**：子 Agent 的结论必须有证据；无证据的降权或剔除。
2. **交叉验证 > 单一来源**：涨价/业绩关键项主 Agent 亲自二次取数，≥2 来源；不足标"未交叉验证"。
3. **分歧如实呈现**：子 Agent 冲突时呈现分歧并裁决，写明理由，不强行统一。
4. **四维加权**：涨价>逻辑>预期>情绪；情绪主导（涨价+逻辑均<0.4）标高风险；PE 仅风险背景。
5. **可追溯**：最终结论标注来源与时间；预判入 predictions.jsonl；错误归因入学习日志。

## 盯盘（对照：主 Agent 单跑，不走本编排）

盘中每 10 分钟 / 12:50 / 竞价 / 17:30 当日总结：主 Agent 直接执行对应 skill，
读当日观察对象记忆重点盯持仓/关注/相关板块，异动推送、无异动静默，不启用团队。

---
name: post-market
description: 盘后。当日总结（17:30，主Agent轻量）汇总当日盘面；综合复盘（22:00，完整团队）跑复盘+选股+回测，跟踪趋势主线与涨价链，产出次日候选并调参。
user-invocable: true
disable-model-invocation: false
---

# 盘后：当日总结 + 综合复盘

## 前置步骤
1. 交易日守卫（`GET /health`）。
2. **强制读取当日观察对象记忆**（持仓/关注/相关板块/自动选股）、`观察池`、当日 `predictions.jsonl`。

---

## 一、当日总结（17:30，**主 Agent 轻量**）

不启用团队，主 Agent 快速汇总：
1. 全天盘面：大盘收盘、成交额、板块涨跌排名（`market_index` `sector_dc`）
2. 情绪面板 + 择时：`sentiment_temperature` + `market_timing`（连续冰点/高热 streak）
3. 涨价链/趋势主线当日进展（`price_hike_scan`，更新观察池）
4. **持仓/关注股当日表现**（重点）
5. **明日仓位倾向初判**：结合择时（连续冰点→提高出手权重/连续高热→警惕退潮）+ 消息面 + 行业催化，给出重仓/中性/空仓倾向
6. 生成 `04-当日总结.md`，推送摘要

### 常用调用
`market_index` `sector_dc` `market_limit` `market_lianban` `money_hsgt` `price_hike_scan`

---

## 二、综合复盘 + 选股 + 回测（22:00，**完整团队**）

启用全部子 Agent 并行，主 Agent 二次验证复核后汇总。

```
22:00 主 Agent（读当日观察对象记忆）
  ├── 技术面趋势分析师：全天行情、趋势主线定位、板块动量轮动
  ├── 情绪面分析师：当日情绪温度值 0-100
  ├── 基本面研报分析师：涨价链/景气进展、次日预期方向
  ├── 宏观时事分析师：晚间公告/消息、外盘展望、北向
  ├── 资金复盘（技术/宏观协同）：龙虎榜、游资/机构动向
  └── 回测分析师：
        · predictions_backtest（当日预判准确率，分驱动）
        · selection_backtest（自动选股 1/3/7/30 日收益/胜率/超额 + 调参建议）
  ↓ 主 Agent 汇总 + 二次验证（单子Agent超时5分钟标[超时]跳过）
  → 综合复盘报告 + 次日选股候选 + 因子调参落地
```

### 择时与重仓/空仓环境（明日）
- 调 `market_timing`（连续冰点/高热 streak、buy_weight_hint）+ 情绪 + 消息面 + 行业催化（含机构放话/研报）
- 明确**明日仓位倾向**：具备重仓环境 / 中性 / 需要空仓，并说明依据；作为次日选股出手权重与仓位上限

### 选股（团队协同，择时叠加）
- `screen_sector` 定强势主线 → `screen_trend`/`screen_quant` 在主线内选股 → 团队叠加涨价/逻辑/情绪复核 → 主 Agent 二次验证
- **出手评分 = 四维综合分 × buy_weight_hint（择时）**；连续冰点期提高试仓权重（重超跌+涨价/逻辑），连续高热期谨慎
- 产出次日候选，符合自动选股定义的用 `log_selection`（category=auto）登记

### 因子调参闭环（回测分析师 → 主 Agent）
- 依据 `selection_backtest` 的分驱动超额与胜率给出权重建议
- 主 Agent 复核后：`get_factor_config` 取最新因子列表 → `set_factor_weights` 提交**全部**因子权重（模型 stock/sector/trend），失败按返回的 `expected_factors` 修正后重试
- 调参依据与结果写入学习日志

### 综合复盘报告结构（05-综合复盘.md）
```markdown
# 综合复盘 — YYYY年MM月DD日
## 摘要
## 四维打分（今日主线/明日重点）
## 数据来源
## 趋势主线跟踪
## 情绪面板
## 全天行情回顾
## 资金与龙虎榜
## 涨价链 & 行业景气进展
## 用户操作回顾
## 晚间公告与消息面
## 明日策略与关注标的
## Agent 自评与回测（分驱动准确率）
```

### 回测逻辑
1. 读当日 `predictions.jsonl` 中 direction≠neutral 的记录
2. 调 `market_daily` 取标的当日实际涨跌，判定准确/偏差
3. 分 `driver` 统计准确率，写入 `学习日志-yyyy年MM月.md`
4. 观察池涨价/趋势线索按当日进展更新状态

### 完成后推送
摘要（≤500 字）：主线定性 + 涨价链进展 + 预判准确率 + 明日核心关注

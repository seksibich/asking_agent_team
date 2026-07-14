---
name: review-learning
description: 回测、自我改进、周报月报。计算预判准确率（分驱动维度），沉淀经验到学习日志，周日产出趋势周报、月末产出月报。
user-invocable: true
disable-model-invocation: false
---

# 回测 + 自我改进 + 周月报

## 一、每日回测（并入 22:00 综合复盘）

见 post-market。预判先用 `log_prediction` 登记到 DB（按 日期+标的+方向 幂等），复盘调 `predictions_backtest`（读 DB 当日预判 → 用 `market_daily` 验证 → 分 `driver` 统计准确率）→ 写 `学习日志`。

## 一之二、选股回测闭环（自动选股专用）★

针对**自动选股**（综合量化 + 消息面 + 热度跑出的股票）建立跟踪回测闭环。
**用户主动指定行业/板块/事件方向的选股不纳入**（这类不代表 Agent 的自主判断力）。

### 登记（选股当时）
每次自动选出标的后，调 `log_selection` 登记到服务端 DB（按 日期+代码+category **幂等去重**，重复登记不会重复计数）：
```json
{"function":"log_selection","params":{"code":"600XXX.SH","name":"某某","score":0.79,"driver":"涨价","reason":"...","category":"auto"}}
```
用户关注/持仓用 `category=watch|holding`（仅观察）；用户临时指定方向的选股不登记。

### 回测（定期）
调 `selection_backtest`，服务端计算每只自动选股在选出后 **1/3/7/30 交易日**的涨幅、胜率、
相对沪深300超额，并按 **driver（涨价/逻辑/预期/情绪）** 与 **分数分桶** 汇总，产出 `tuning_hints`。

### 据回测自主微调（每晚，署名 + 留痕）★
每晚回测后，允许 Agent（回测分析师产出建议 → 主 Agent 复核）**自主微调量化选股中权重不为 0 的因子**，并落库生效：

**微调对象与边界（安全护栏）**
- **仅微调当前权重 ≠ 0 的因子**；权重为 0 的候选因子保持 0（启用某候选因子需人工/用户明确决定，不由夜间自动开启）。
- **小步微调**：单次每个因子调整幅度 ≤ 0.03（绝对值），单因子权重限定在合理区间（如 ≤ 0.40）；调整后**归一化使和=1.0**。
- 依据必须来自 `selection_backtest` 的真实证据：某 driver 的 30 日超额/胜率持续领先 → 适度提高相关因子；持续负超额（尤其情绪类）→ 适度降低或提高门槛。

**落地方式（每次都署名 + 留痕）**
1. `get_factor_config(model=stock)` 取最新 `canonical_factors` 与当前权重（含 `version_id`）。
2. 在非零因子上按边界微调，构造**全部**因子的新权重（0 权重候选保持 0）。
3. `set_factor_weights(model=stock, weights=..., actor="回测分析师"|"main-orchestrator", reason="回测证据: ...；基于回测快照/情绪版本 ...")`。
4. 返回的 `version_id` 写入 `学习日志`（连同调整前后权重与依据），便于日后 `get_config_version` 定位或 `restore_config_version` 回滚。

### 综合情绪指数判断选股确定性 + 背离才调情绪权重 ★
- **选股确定性**结合情绪指数：`sentiment_temperature`（0-100）+ `market_timing`（连续冰点/高热、buy_weight_hint）给出大盘环境确定性；个股出手评分 = 四维综合分 × buy_weight_hint。
- **背离判定**：当**回测结论（因子/选股有效性）与情绪指数所示环境方向持续背离**时——例如自动选股近期胜率/超额良好但情绪连续退潮/冰点（环境与信号相反），或情绪高热但回测超额转负——且背离持续（≥2 个交易日/连续 2 次回测），**才允许微调情绪指数各参数权重**（`set_factor_weights(model=sentiment, ...)`，同样署名 + 留痕）。
  - 调整方向：降低在当前环境下失真/负贡献的情绪分量权重，提高更稳健的分量；遵守上面的小步 + 归一 + 仅动非零 边界。
  - **无背离时不动情绪权重**；情绪权重不因单日波动频繁调整。
- 背离结论、所用回测/情绪 `version_id`、调整前后权重一并写入 `学习日志`。

> 所有微调经服务端留痕：每次 `set_factor_weights` / `set_sentiment_config` 生成类 commit 的 `version_id`（记录 actor/reason/parent/payload）。用 `get_config_history` 查历史、`get_config_version` 定位某版本、`restore_config_version` 回滚。

## 自我改进逻辑
- 统计各 driver 维度历史准确率与选股超额，识别 Agent 在哪个维度更可靠
- 若某维度长期偏差大，提高该维度验证门槛
- 归纳可复用的「避坑规则」写入学习日志

### 自我改进逻辑
- 统计各 driver 维度历史准确率，识别 Agent 在哪个维度更可靠
- 若某维度长期偏差大，在打分依据中提高该维度的验证门槛（不改四维权重本身）
- 归纳可复用的「避坑规则」写入学习日志

## 二、趋势周报（每周日 20:00）

面向趋势与行业，不只是行情汇总。

### 流程
1. 调 `sector_dc`、`screen_sector`、`price_hike_scan` 汇总本周涨价链与主线轮动
2. 调 `screen_trend` + `screen_quant`（自动跑 `top_n=50`）生成下周候选池，并**按主线/产业链分组解读**（逐股板块/行业/炒作路径 + 消息面/行业新闻/近期主线，见 output-format 表格5）
3. 复查 `观察池` 所有线索，更新/兑现/淘汰
4. 统计本周预判准确率（`predictions_backtest`）+ 自动选股表现（`selection_backtest`），产出调参建议
5. 产出 `周报/yyyy年第NN周周报.md`：
```markdown
# 第NN周周报 — YYYY-MM-DD
## 本周主线与轮动
## 涨价链进展（观察池复查）
## 趋势主线跟踪表
## 下周候选池（趋势+量化 top_n=50，四维打分 + 按主线/产业链分组解读）
## 本周预判复盘（分驱动准确率）
## 数据来源
```

## 三、月报（每月末）

1. 汇总当月主线演绎、涨价链兑现情况
2. 更新 `用户画像.md`
3. 统计当月准确率与改进项
4. 产出 `月报/yyyy年MM月月报.md`

## 严谨要求
- 回测结果基于真实行情数据，禁止美化准确率
- 失误如实归因，不回避

---
name: review-learning
description: 回测、自我改进、周报月报。计算预判准确率（分驱动维度），沉淀经验到学习日志，周日产出趋势周报、月末产出月报。
user-invocable: true
disable-model-invocation: false
---

# 回测 + 自我改进 + 周月报

## 一、每日回测（并入 22:00 综合复盘）

见 post-market。**仅调度器自动链路中的正式方向性预判**可先用 `log_prediction` 登记到 DB（按 日期+标的+方向 幂等）；登记的是面向下一 SSE 交易日的新预判。随后才调用 `predictions_backtest`，且只核验目标交易日已经完成的历史预判，绝不能把刚登记、尚未成熟的新预判纳入当晚准确率；回测继续读 DB 正式预判、用 `market_daily` 验证、分 `driver` 统计并写 `学习日志`。用户主动单股调研、方向选股、行业/事件研究不调用 `log_prediction`、不写 `predictions.jsonl`；其中用户触发正式选股只以 `manual` selection 做隔离回测，不进入 auto 预判胜率。

## 一之二、选股回测闭环（ephemeral / auto / manual / watch / holding 严格隔离）★

- **auto**：仅调度器正式自动候选，进入自动胜率/超额、`tuning_hints` 与因子/情绪调参。
- **manual**：用户明确发起选股任务并通过完整正式门槛的候选；保存完整选股快照，跟踪 1/3/7/30 日收益，但不进入 auto 胜率、`tuning_hints` 或调参。
- **ephemeral**：普通单股调研、行业/事件研究或未通过正式门槛的线索，不登记、不回测。
- **watch**：用户明确要求持续观察后登记；只作观察统计，不进入 auto 调参。
- **holding**：用户持仓仅作观察，沿用持仓规则，不进入 auto 调参。

### 登记（选股当时）

1. 先执行 `selection_tag_catalog {}`，读取 `selection_tag_version` 与固定标签说明；当 `/health.selection_tag_version` 变化时必须刷新。
2. 每只正式候选都调用 `log_selection`（按 日期+代码+category **幂等去重**）：
```json
{
  "function": "log_selection",
  "params": {
    "code": "600XXX.SH",
    "name": "某某",
    "selected_at": "2026-07-16 10:25:00",
    "screening_run_id": "screen_quant返回的真实运行ID",
    "core_event": "实验猴供给收缩，相关服务价格出现上行线索",
    "reason": "公司直接提供相关CRO服务；量化分位与行业强度居前。若价格与订单证据不能继续验证则失效。",
    "tags": ["医药/创新药", "CRO", "CXO", "实验猴", "龙头", "逻辑", "预期"],
    "driver": "逻辑",
    "category": "auto|manual"
  }
}
```
- 调度器正式候选用 `auto`，用户触发正式候选用 `manual`；`watch|holding` 只在用户明确要求历史观察回测时使用，当前自选仍由 `portfolio_upload` 管理。
- `screening_run_id` 必须来自同一选股交易日且包含该代码；服务端从运行快照读取评分、排名、完整因子契约和依赖，不接受 Agent 自填分数覆盖。
- `core_event` 只写一条最关键、可核验的事件或催化；`reason` 由 Agent 精炼为“实际受益 → 量化/趋势依据 → 风险证伪”，不复制报告全文，不堆砌标签。
- `tags` 为去重字符串数组，顺序固定为“主板块/题材 → 细分方向 → 具体事件 → 固定属性”。固定属性优先选标签合集；板块、题材、产品和事件标签可自行编排，如 `医药/创新药`、`CRO`、`CXO`、`实验猴`。
- `selected_at` 使用上海时间并与筛选运行日期同日；兼容调用仍可传 `date`，但两者不得冲突。
- 服务端上传后补充最新价及可核验的 `涨停` / `跌停` 标签，并返回行情错误；失败时保留为空并披露，禁止 Agent 自行估价或判断涨跌停。
- 同一“日期+代码+类别”的重复上传返回成功且 `inserted=false`、`duplicate=true`；`record` 是首次固化记录，`current_quote` 是本次请求刷新行情，二者不得混作同一时点。
- 选股价、评分、因子版本、结构哈希、权重版本、上游依赖和回测字段维持既有服务端口径。普通研究及业绩增长参考池保持 ephemeral，不调用 `log_selection`。

### 回测与看板（定期）
调 `selection_backtest` 分别展示 auto/manual/watch/holding；仅合格 auto 样本进入自动优化门禁。`selection_dashboard` 首次默认展示目标交易日及之前三个交易日，每次调用刷新实时行情：仅日期筛选按题材聚合，传题材/标签筛选时按日期聚合；聚合内龙头、核心优先，其余按评分排序，选择全局排序时取消聚合。管理员查看 `watch/holding` 或不限类别时，看板会实时并入当前自选 `portfolio_items`（标注 `live_portfolio`、「当前自选」标签、`id=pf-<代码>`），始终展示、不受日期限制、不写入 `selections`、不计入回测；这是展示层合并，当前自选事实源仍是 `portfolio_upload`/`portfolio_get`。

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

## 业绩增长参考池隔离（T3 强制）

- T3 的「业绩增长参考池」仅为公告事实参考，不是自动选股、关注或持仓样本。
- 参考池记录不得调用 `log_selection`，不得写 `predictions.jsonl` 或创建短期事项，不得映射为任何选股类别，也不得进入回测或调参。
- 同一股票若独立通过正式 `screen_quant`/`screen_trend` 流程，只允许以正式候选身份按正常规则进入回测；样本理由和来源必须来自正式流程，不能继承“进入业绩参考池”这一事实。
- 回测报告须声明已排除业绩增长参考池；若发现误入样本，先剔除并披露，不得据此调参。

## 自我改进逻辑
- 统计各 driver 维度历史准确率与选股超额，识别 Agent 在哪个维度更可靠。
- 若某维度长期偏差大，在打分依据中提高该维度的验证门槛（不改四维权重本身）。
- 归纳可复用的“避坑规则”先写入学习审计；只有经多个独立样本验证、抽象后不依赖具体日期和标的，才可由主 Agent 提炼进永久 `MEMORY.md`。

## 二、趋势周报（每周日 20:00）

面向趋势与行业，不只是行情汇总。

### 流程
1. 调 `sector_dc`、`screen_sector`、`price_hike_scan` 汇总本周涨价链与主线轮动
2. 调 `screen_trend` + `screen_quant`（自动跑 `top_n=50`）生成下周候选池，并**按主线/产业链分组解读**（逐股板块/行业/炒作路径 + 消息面/行业新闻/近期主线，见 output-format 表格5）
3. 复查 `短期记忆/` 中相关未过期线索：更新复查动作与时效，完成、兑现、证伪或到期后立即删除
4. 统计本周预判准确率（`predictions_backtest`）+ 自动选股表现（`selection_backtest`），产出调参建议
5. 产出 `周报/yyyy年第NN周周报.md`（首屏先给一眼结论，再展开）：
```markdown
# 📅 第NN周周报 — YYYY-MM-DD
## 🎯 一眼结论（核心摘要）
- 📋 本周复盘一句话：<主线演绎 + 预判准确率 + 最值得记住的经验/教训>
- 🔥 下周重点主线/题材：
- 🎯 下周重点候选（题材/事件 → 个股）：
- ⚠️ 最大风险/证伪：
## 🔥 本周主线与轮动
## 📈 涨价链进展（相关短期线索复查）
## 📊 趋势主线跟踪表
## 🎯 下周候选池（趋势+量化 top_n=50，四维打分 + 按主线/产业链分组解读）
## 📋 本周预判复盘（分驱动准确率）
## 🔗 数据来源
```

## 三、月报（每月末）

1. 汇总当月主线演绎、涨价链兑现情况
2. 复核 `USER.md`；仅把用户明确表达或反复确认的长期偏好写入，临时指令、持仓、选股、进度、问题和推断不得写入
3. 统计当月准确率与改进项
4. 产出 `月报/yyyy年MM月月报.md`（同样首屏先给 `## 🎯 一眼结论（核心摘要）`：📋 当月复盘一句话、🔥 下月主线预判、⚠️ 最大风险，再展开正文；主要章节沿用统一 emoji 图标）

## 严谨要求
- 回测结果基于真实行情数据，禁止美化准确率
- 失误如实归因，不回避

## Skill 加载约束 / 依赖 Skills

- 回测、调参、周月报前完整读取本文件并确认固定 12 Skills（含 `stock-research`）已完整加载，不得只凭历史调参摘要执行。
- **直接依赖**：`data-service`（真实行情、错误与重试）、`quant-screening`（因子契约）、`output-format`（回测报告）、`post-market`（每日闭环）。
- **协同 Skills**：`priority-framework`、`stock-screening`、`industry-analysis`、`stock-research`；其中 `stock-research` 仅在用户明确持久化为 watch 后提供观察样本，禁止进入 auto 调参。
- 数据缺失时按 `skills/data-service/SKILL.md` 标 `degraded`，不计算伪准确率；T3 的关键接口执行 5/15 分钟延迟重试，非关键接口失败不阻塞已有样本的复盘。

## v1.6 可审计回测与自动优化门禁（强制）

### 预判回测
- `log_prediction` 固化上海时间预测时刻和由 SSE `trade_cal` 确定的下一交易日；同一预判日期+标的记录不可变，反向冲突必须拒绝。
- `predictions_backtest` 只核验目标交易日已完成的样本。缺目标日的旧记录标 `legacy_unverifiable`，未成熟、停牌/空行情、接口失败分别计数并披露；不得静默缩小样本，更不得用预判当天已发生的涨跌回填。
- 回测默认保存 `snapshot_id`、计算口径版本、样本哈希、目标日和失败审计；准确率分母只含成熟且行情核验成功样本。

### 选股收益与自动优化
- 1/3/7/30 日收益使用 SSE 统一交易日；股票只接受 qfq 前复权，股票与沪深300必须同日入场/退出。停牌或精确日期缺价记失败，禁止前后错位或降级原始价。
- `selection_backtest` 默认保存快照；只有当前因子契约和依赖下、来自可核验 `screen_quant` 运行的 `auto` 样本进入优化门禁，分桶唯一口径为 `score_percentile`。
- 自动调参至少需要50个成熟30日样本、10个独立选股日、10个时序样本外样本，且样本外平均超额为正、超额胜率>50%。未满足时 `optimization_gate.eligible=false`，严禁自动调参。
- 调参必须提交 `backtest_snapshot_id`、当前 `expected_parent_version` 和全部因子权重；每因子单次变化≤0.03，不得自动启用当前权重0因子。配置与版本必须单事务发布，CAS 冲突后刷新配置，不得覆盖他人版本。
## v2.2.0 当前调度与日终边界

- 每日回测并入现行 T3，周期回测使用 W1/M1；不使用旧 T6/T7/D1。
- 服务端交易日 16:00 自动收口；回测只读 `health.daily_finalize` / `precompute_status`，不得因样本缺失、状态失败或门禁未通过自动调用 `precompute_daily_factors`。管理员单次补数仅限用户明确要求。

## 本技能接口速查与规范位置（v2.6.0）

> 完整协议/参数/返回/错误码见工作目录 `工作文档/接口文档/AGENT_SERVICE_GUIDE.md`、`工作文档/接口文档/SERVICE_INDEX.md`；取数契约见 `工作文档/skills/data-service/SKILL.md`。

| 功能 | 用途 | 关键参数要点 |
|---|---|---|
| selection_tag_catalog | 读版本化固定标签 | selection_tag_version 变化时刷新 |
| log_selection | 上传正式候选/观察快照（幂等：日期+代码+category） | 附完整字段与请求参数（见 quant-screening 规范） |
| log_prediction / predictions_backtest | 预判登记 / 成熟预判回测 | 目标日为下一 SSE 交易日，不可反向覆盖 |
| selection_backtest | 正式 auto 样本 1/3/7/30 收益与调参门禁 | 仅 auto 进优化；score_percentile 分桶 |
| selection_dashboard | 查看选股与刷新行情 | 默认目标日及前三个交易日 |
| get_factor_config / set_factor_weights | 因子权重读/写 | 提交全部因子、门禁通过才调参、留痕 version_id |
| get_config_history / get_config_version / restore_config_version | 配置历史/定位/回滚 | — |
| get_sentiment_config / set_sentiment_config | 情绪归一窗口读/写 | 仅回测与情绪指数背离时调整 |
| screen_trend / screen_quant / market_daily | 生成候选池 / 回测取价 | qfq 前复权、SSE 统一交易日 |

报告接口失败/降级问题置于 output-format「🛠️ 数据接口问题」文末附录；仅量化选股与 `log_selection` 附请求参数。

---
name: pre-market
description: 盘前汇总（08:30，团队模式）。整合消息/全球市场/期货/宏观数据+情绪择时，判断重仓/空仓环境，结合近7日自动选股给出今日关注板块与个股，并把高热度股加入临时观察列表。竞价分析见 bidding-analysis(09:25)。
user-invocable: true
disable-model-invocation: false
---

# 盘前汇总（08:30）

> 竞价分析已独立为 `bidding-analysis` 技能，在 **09:25 竞价结束**后由主 Agent 执行。

> **输出目录取决于触发来源**（见 output-format「触发来源决定输出目录」）：定时任务(T1)→ `yyyy年MM月dd日/01-盘前汇总.md` 并写自动记忆；**用户手动触发 → `投研/yyyyMMdd-手动盘前汇总/`，且不覆盖当日定时日报、不改写 `daily/yyyyMMdd.md`、不以 category=auto 登记选股**。

## 前置步骤（共用）

1. **交易日守卫**：`GET /health`，`trade_open=false` 则直接返回。
2. **强制读取当日观察对象记忆** `agent记忆/daily/yyyyMMdd.md`（若未生成则先按 memory 规则生成：合并 `关注与持仓.md` + 近7日自动选股）。
3. 读取 `观察池.md`（涨价/趋势线索）。
4. **数据全部经由数据服务，禁止编造。**

---

## 一、盘前汇总（08:30，**团队模式**）

启用团队：宏观时事分析师、技术面趋势分析师、基本面研报分析师、情绪面分析师并行产出意见，主 Agent 二次验证复核后汇总。执行流程（按重心排序）：

#### 1. 涨价与行业景气扫描（第一优先，基本面研报分析师）
- 调 `price_hike_scan` + 自主外部渠道（行业平台/期货）获取涨价信号，`news_filter` + `macro_ppi` 交叉印证
- 复查 `观察池.md` 涨价线索，更新状态

#### 2. 消息汇总 + 全球市场 + 期货 + 宏观（宏观时事分析师）
- 时政/产业事件：`news_flash` `news_filter` `news_cctv`
- 全球市场：`overseas_us` `overseas_hk` + 大宗商品/A50（外部）
- 期货市场：国内商品期货主力（涨价链先行信号，外部/`price_hike_scan`）
- 宏观数据：`macro_ppi`（涨价锚）`macro_cpi` `macro_pmi` `macro_m`
- 北向资金：`money_hsgt`

#### 3. 技术面与情绪·择时（技术面/情绪面分析师）
- 大盘趋势与成交量、强势板块动量（`market_index` `sector_dc` `screen_sector`）
- 情绪温度值 0-100（`sentiment_temperature`）
- **择时**：调 `market_timing` 获取连续冰点/高热 streak、stance、buy_weight_hint

#### 4. 结合近7日自动选股（**重点前一日**）
- 调 `selection_backtest` 或读近7日 auto 选股记录，重点看前一日自动选股表现与仍有效标的
- 持仓/关注股逐一检查催化与风险（重点盯）

#### 5. 重仓 / 空仓环境初判（★择时核心）
结合 **情绪择时（market_timing）+ 消息面 + 行业催化（含机构放话/研报观点）** 做初步判断：
- **具备重仓环境**：情绪健康或**连续冰点后反转** + 明确正向催化（涨价/政策/机构一致看多）+ 主线清晰 → 当日仓位上限上调，出手买入权重上调
- **需要空仓/降仓**：**连续高热见顶** / 重大利空 / 无主线且情绪退潮 / 催化落空 → 降仓或空仓，不追高
- 明确写出「今日仓位倾向 + buy_weight_hint + 依据」（非确定性指令）

#### 6. 高热度临时观察列表（★）
- 取 `hot_dc` `hot_ths` `hot_kpl_concept` 热度排行，将高热度个股写入当日观察对象记忆的**临时观察列表**（当日有效，供 09:25 竞价分析与盘中盯盘参考）
- 临时观察 ≠ 关注/持仓，不持久、不计入回测调参

#### 7. 主 Agent 二次验证 + 今日关注
- 交叉核对各子 Agent 关键数据（尤其涨价）
- 按四维打分 × buy_weight_hint（择时）排序，产出**今日关注板块与个股**（含四维打分、择时倾向与理由）
- 明确预判写 `predictions.jsonl`（标 driver）；今日 auto/watch/holding 标的 `log_selection` 登记
- 更新当日观察对象记忆的「当日重点板块」与「临时观察列表」

#### 8. 生成报告与推送
- 写 `盘前/yyyy年MM月dd日/01-盘前汇总.md`：数据来源 → 涨价/景气 → 消息/全球/期货/宏观 → 技术+情绪+**择时** → **重仓/空仓环境初判** → 近7日选股回顾 → 持仓关注提示 → **今日关注板块与个股（四维打分×择时）** → 临时观察列表
- 推送摘要：今日仓位倾向（重仓/中性/空仓）+ 核心方向 1~2 个 + 关注/持仓重要提示

### 常用调用
`price_hike_scan` `news_flash` `news_filter` `news_cctv` `overseas_us` `macro_ppi/cpi/pmi/m` `money_hsgt` `market_index` `sector_dc` `screen_sector` `sentiment_temperature` `market_timing` `hot_dc` `hot_ths` `hot_kpl_concept` `selection_backtest`

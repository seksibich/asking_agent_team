# 全市场因子预计算优化方案

## 背景与问题

当前 `screen_quant` 全市场选股时，对候选股**逐只调用 `daily(code,start,end)`**（截断到 800 只），
单次运行最多约 800 次 tushare 请求，慢且耗积分，还可能漏股。板块内选股同理。

**优化目标**：把「取数 + 因子计算」与「排序选股」解耦——每日盘后**一次性**算好全市场个股因子，
落库 `daily_factors`；选股时直接读库做横截面 z-score 合成排序，几乎不再实时打 tushare。

## 数据表

`daily_factors(trade_date, code, factors JSON, computed_at)`，唯一键 `(trade_date, code)`。
`factors` 存 `factors.py::compute_stock_factors` 的原始值（mom_12_1 / reversal_1m / trend_ma /
high_52w / low_ivol / low_turnover / vol_confirm）。DB 层已提供 `bulk_upsert_daily_factors` /
`fetch_daily_factors`。

## 取数策略（关键：从"逐只"改为"逐日"）

个股因子需要每只约 1 年日线。**不要逐只拉**，改用**按交易日**的全市场切片：
- `daily(trade_date=D)` 一次返回全市场当日所有个股 → 一天一次调用。
- 拉最近约 260 个交易日的全市场切片（约 260 次调用），在内存/临时表中按 code 透视成每只的日线序列，
  再对每只跑 `compute_stock_factors`。
- 之后**每日单日重算**：默认只写入目标日 D；D 的历史回看切片通过永久缓存复用，因此通常不重复下载。当前实现是“单日计算 + 历史切片缓存”，并非把全部滚动窗口完全留在内存中的严格增量算法。

对比：首次约 **260 次调用**（全量），日常 **每日 1 次切片**；远优于每次选股 800 次。

换手率用 `daily_basic(trade_date=D)` 全市场切片（同样按日取）。

## 预计算作业

新增功能（建议放 `skills/quant-screening/scripts/precompute.py`）：

- `precompute_daily_factors(date?, lookback=260, full=False)`
  1. 交易日守卫；确定目标日 D（默认最近交易日）。
  2. `full=False`（日常增量）：只计算目标交易日 D；D 的历史切片通过永久缓存复用，不重复下载。
  3. `full=True`（首次/补算）：按最近 lookback 个交易日逐日计算；单日失败会记录并继续后续日期。
  4. 返回 `status`、覆盖率、`date_results`、`failed_dates`、`partial_dates`，失败日期可重跑。
- 幂等：`(trade_date, code)` 唯一键；每次按整日替换，避免部分重算残留旧股票记录。
- 质量门槛：剔除 ST/退市标的；覆盖率低于 80%、回看窗口有数据错误或因子版本不一致时，选股不读取该日期。

## 选股改造（读库优先，回退实时）

`quant_screen.run` / `screen_trend.run`：
1. 先检查 `daily_factor_runs`：目标日必须 `status=success`、覆盖率达标且 `factor_version` 一致，再读取 `daily_factors` 做横截面 z-score 合成排序。
2. 未命中或质量不合格 → 回退实时逐只逻辑（保证可用），并提示建议重跑预计算。
3. 预计算阶段已统一排除 ST/退市标的，实时路径和预计算路径保持一致。

好处：选股从"800 次网络请求"降到"1 次库查询 + 本地计算"，秒级返回；权重变更（`set_factor_weights`）
不需要重新取数，直接对已存因子重算 z-score。

## 定时任务

在 `schedule.md` 增加：
- **每交易日 17:45**（收盘数据稳定后、当日总结前后）：`precompute_daily_factors`（增量）。
- 首次部署或断档：手动 `precompute_daily_factors(full=true)` 补算历史窗口。

## 缓存/持久化配合

- `daily_factors` 落 DB（本地 SQLite / 上云 RDS），随卷/实例持久化。
- 全市场日线切片可复用 `common.cached_call(historical=True)` 永久缓存（按 trade_date 不可变），
  预计算与其他功能共享，避免重复拉切片。

## 落地步骤（建议顺序）

1. 已就绪：`daily_factors` 表 + DB 读写函数。
2. 新增 `precompute.py`（precompute_daily_factors）并注册。
3. `quant_screen`/`screen_trend` 加"读库优先、回退实时"分支。
4. schedule 增加 17:45 预计算任务。
5. 首次 `full=true` 补算，验证选股读库路径与实时路径结果一致。

## 实现状态（已落地）

1. ✅ `daily_factors` + `daily_factor_runs` 表及 DB 读写；任务状态、因子版本、覆盖率和错误信息可查询。
2. ✅ `precompute_daily_factors`：严格交易日守卫、ST/退过滤、80%覆盖率门槛、整日替换、单日失败后继续补算、失败日期可重跑。
3. ✅ `screen_quant`/`screen_trend` 只读取 `status=success` 且因子版本一致的预计算；否则回退实时。
4. ✅ `selection_forward_returns.matured` 已加入 RDS schema，并在启动时执行旧库轻量补列迁移。
5. ✅ schedule 保留 D1（交易日 17:45）任务；部署后首次执行 `precompute_daily_factors {"full":true}`，后续每日执行默认单日模式。

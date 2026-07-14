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
- 之后**每日增量**：只需拉当日 1 个切片，滚动更新窗口，重算当日因子。

对比：首次约 **260 次调用**（全量），日常 **每日 1 次切片**；远优于每次选股 800 次。

换手率用 `daily_basic(trade_date=D)` 全市场切片（同样按日取）。

## 预计算作业

新增功能（建议放 `skills/quant-screening/scripts/precompute.py`）：

- `precompute_daily_factors(date?, lookback=260, full=False)`
  1. 交易日守卫；确定目标日 D（默认最近交易日）。
  2. `full=False`（日常增量）：读 `daily_factors` 已有窗口 + 当日切片，算 D 的全市场因子，`bulk_upsert` 写库。
  3. `full=True`（首次/补算）：拉最近 lookback 个交易日切片，逐日算并写库。
  4. 返回写入条数、覆盖股票数、耗时。
- 幂等：`(trade_date, code)` 唯一键 upsert，可重复跑。

## 选股改造（读库优先，回退实时）

`quant_screen.run` / `screen_trend.run`：
1. 先 `db.fetch_daily_factors(last_trade_date)`；命中则用库中因子构建横截面表 → z-score 合成排序。
2. 未命中（当日未预计算）→ 回退现有实时逐只逻辑（保证可用），并提示"建议先跑 precompute"。
3. 行业限定：库内因子表按 `meta_stock_basic` 的行业字段过滤即可，无需再取数。

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

1. ✅ `daily_factors` 表 + DB 读写（`bulk_upsert_daily_factors`/`fetch_daily_factors`/`has_daily_factors`/`latest_factor_date`）。
2. ✅ `precompute.py::precompute_daily_factors`（按交易日切片、永久缓存复用、幂等 upsert；支持 full 补算）。
3. ✅ `screen_quant`/`screen_trend` 读库优先、回退实时（返回 `data_source: precomputed|realtime`）；全市场路径不打 tushare，改权重直接对库内因子重排。
4. ✅ schedule 增加 D1（交易日 17:45）预计算任务。
5. 首次部署执行一次 `precompute_daily_factors {"full": true}` 补算历史窗口后即可。

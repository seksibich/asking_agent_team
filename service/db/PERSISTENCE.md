# 数据持久化与链路审计

## 原则
- **可变业务状态 → 数据库**（本地 SQLite / 上云 RDS MySQL，`DB_URL` 切换），上云后单一真源、多实例一致。
- **可重建的缓存 → 文件**（CACHE_DIR，Docker 卷），丢失可从 tushare 重新拉。
- **Agent 记忆（报告/画像/观察池等）→ Agent 输出目录**（非后端职责，按 output-format/memory 规则）。

## 后端持久化数据映射

| 数据 | 存储 | 位置 | 写入者 | 读取者 |
|---|---|---|---|---|
| 选股登记（auto/watch/holding） | **DB** `selections` | RDS/SQLite | `log_selection`（幂等去重） | `selection_backtest` |
| 预判 | **DB** `predictions` | RDS/SQLite | `log_prediction`（幂等） | `predictions_backtest` |
| 选股前向收益（1/3/7/30） | **DB** `selection_forward_returns` | RDS/SQLite | `selection_backtest`（成熟固化） | `selection_backtest` |
| 回测聚合快照 | **DB** `backtest_snapshots` | RDS/SQLite | `selection_backtest(save_snapshot)` | 查询/审计 |
| 因子/指标权重覆盖 | **DB** `config_kv` (key=factor_weights) | RDS/SQLite | `set_factor_weights` | 各 screen_* / sentiment |
| 情绪归一窗口（3-30天） | **DB** `config_kv` (key=sentiment_window) | RDS/SQLite | `set_sentiment_config` | sentiment_temperature / market_timing |
| 全市场个股因子（预计算） | **DB** `daily_factors` | RDS/SQLite | `precompute_daily_factors` | `screen_quant`/`screen_trend` |
| 每日情绪原始指标 | **DB** `daily_sentiment` | RDS/SQLite | `sentiment_temperature`/`market_timing` | 二者 |
| tushare 日级数据（当日） | 文件缓存 | CACHE_DIR/{date}/ | `cached_call` | 全部取数功能 |
| tushare 历史数据（不可变） | 文件永久缓存 | CACHE_DIR/permanent/ | `cached_call(historical)` | 历史日线/切片 |
| 盘中快照（增量对比） | 文件 | CACHE_DIR/intraday_snapshot.json | `watch_intraday` | 下一轮 |

> 说明：`daily_sentiment`/`daily_factors` 既是"可重建"又是"要长期留存/共享"的派生数据，统一入库更利于上云与前端查询，故归 DB。tushare 原始切片仍走文件永久缓存，避免把大体量原始行情灌入库。

## 关键链路（端到端）

1. **选股→回测→调参闭环**：
   `screen_quant/sector/trend`（候选，读 daily_factors）→ `log_selection`(auto) 落库 →
   （T+1..T+30 成熟）`selection_backtest` 计算并把前向收益固化 `selection_forward_returns` →
   `tuning_hints` → `get_factor_config`/`set_factor_weights`（写 config_kv）→ 下次选股读新权重。
2. **预判→回测**：`log_prediction` 落库 → `predictions_backtest` 读库对比实际涨跌 → 学习日志。
3. **情绪→择时→选股**：`sentiment_temperature`（写/读 daily_sentiment）→ `market_timing`（择时）→
   选股出手权重 buy_weight_hint。
4. **因子预计算→选股提速**：`precompute_daily_factors`（盘后 17:45，读 tushare 切片[永久缓存]→写 daily_factors）
   → `screen_quant/trend` 读库本地排序（全市场路径不打 tushare）。

## 幂等与一致性
- `log_selection`：唯一键 (sel_date, code, category)；`log_prediction`：(pred_date, target, direction)；
  `daily_factors`：(trade_date, code)；`daily_sentiment`：trade_date PK；`config_kv`：k PK；
  `selection_forward_returns`：(selection_id, horizon)。均为幂等 upsert，可安全重跑。
- 文件写入用 `common.atomic_write_json`（临时文件+os.replace+fsync），避免并发写坏。

## 上云切换
- 设 `DB_URL=mysql+pymysql://user:pwd@rds:3306/stock_agent?charset=utf8mb4`，先在 RDS 执行 `schema.sql`（或依赖服务启动 `create_all`）。
- CACHE_DIR 仍为实例本地/挂载卷（缓存，不需跨实例共享；如多实例可各自重建）。
- Agent 记忆中的 `service_state.json` 更新 `base_url` 为公网服务地址。

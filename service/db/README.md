# 回测数据持久化（阿里云 DB）

## 现状
服务当前把回测原始数据落在 `DATA_DIR` 的 JSONL 文件里：
- `selections.jsonl` —— `log_selection` 登记的每条选股（auto/watch/holding）
- `predictions.jsonl` —— 每日预判

回测「结果」由 `selection_backtest` / `predictions_backtest` 每次实时计算（读 JSONL + 拉 tushare 前向行情）。前端「回测分析」页直接调用这两个功能，无需数据库即可可视化。

## 为什么上数据库
JSONL 适合单机小规模，但上云后有三个痛点：
1. 每次回测都要重算前向收益（重复调用 tushare，慢且耗积分）。
2. 多实例部署时文件不共享、无并发控制。
3. 缺少历史留存与按条件查询能力。

`schema.sql` 给出阿里云 RDS MySQL 8.0 的结构：`selections` / `predictions` / `selection_forward_returns`（收益缓存）/ `backtest_snapshots`（聚合结果留存）/ `daily_factors`（全市场个股因子）/ `daily_factor_runs`（预计算任务质量状态）。

## 部署步骤
1. 在阿里云 RDS MySQL 实例创建库并执行 DDL：
   ```sql
   CREATE DATABASE stock_agent DEFAULT CHARACTER SET utf8mb4;
   USE stock_agent;
   SOURCE schema.sql;
   ```
2. 在 `.env` 增加连接串（示例）：
   ```dotenv
   DB_URL=mysql+pymysql://user:password@rm-xxxx.mysql.rds.aliyuncs.com:3306/stock_agent
   ```
3. `requirements.txt` 增加驱动：`SQLAlchemy` + `PyMySQL`。
4. 把 `log_selection` 的落盘从写 JSONL 改为写 `selections` 表；`selection_backtest` 优先读
   `selection_forward_returns`，miss 时再拉 tushare 计算并回写（幂等 upsert，见 `schema.sql` 示例）。

## PostgreSQL 差异（若用 RDS PG）
- `ENUM` 改用 `CHECK` 约束或 `CREATE TYPE`；
- `JSON` 改 `JSONB`；
- `AUTO_INCREMENT` 改 `BIGSERIAL`/`GENERATED ALWAYS AS IDENTITY`；
- `ON DUPLICATE KEY UPDATE` 改 `INSERT ... ON CONFLICT (...) DO UPDATE`；
- `FIELD(...)` 排序改 `ORDER BY CASE horizon WHEN 1 THEN 0 ... END`。

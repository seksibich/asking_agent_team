-- ============================================================
-- 回测数据持久化 Schema（阿里云 RDS MySQL 8.0）
-- ------------------------------------------------------------
-- 背景：当前服务把选股/预判原始数据落在 DATA_DIR 下的
--   selections.jsonl / predictions.jsonl（Docker 卷 ./data）。
--   回测「结果」由 selection_backtest / predictions_backtest 每次
--   实时计算。为便于上云、并发查询与历史留存，这里给出关系库结构：
--     selections            —— 对应 log_selection 登记的每条选股
--     predictions           —— 对应 predictions.jsonl 的每条预判
--     selection_forward_returns —— 缓存每条选股各持有期前向收益/超额
--     backtest_snapshots    —— 留存某时刻计算出的回测聚合结果（JSON）
--
-- 使用：在阿里云 RDS MySQL 实例中新建库后执行本文件。
--   CREATE DATABASE stock_agent DEFAULT CHARACTER SET utf8mb4;
--   USE stock_agent;  然后执行下面的 DDL。
-- ============================================================

SET NAMES utf8mb4;

-- ---------- 1. 选股登记（log_selection） ----------
CREATE TABLE IF NOT EXISTS selections (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  sel_date    DATE            NOT NULL COMMENT '选股日期 YYYY-MM-DD',
  code        VARCHAR(16)     NOT NULL COMMENT 'tushare 代码，如 600000.SH',
  name        VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '名称',
  score       DECIMAL(8,4)    NOT NULL DEFAULT 0 COMMENT '综合打分/四维综合分',
  driver      VARCHAR(32)     NOT NULL DEFAULT '未标注' COMMENT '主导驱动：涨价/逻辑/预期/情绪',
  reason      TEXT            NULL COMMENT '入选理由',
  category    ENUM('auto','watch','holding') NOT NULL DEFAULT 'auto'
              COMMENT 'auto=自动选股(用于调参)/watch=关注/holding=持仓',
  extra       JSON            NULL COMMENT '附加快照：heat/news/factors/相关板块等',
  logged_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '登记时间',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_sel_date (sel_date),
  KEY idx_code (code),
  KEY idx_category_date (category, sel_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动/关注/持仓 选股登记';

-- ---------- 2. 预判登记（predictions.jsonl） ----------
CREATE TABLE IF NOT EXISTS predictions (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  pred_date   DATE            NOT NULL COMMENT '预判日期 YYYY-MM-DD',
  target      VARCHAR(32)     NOT NULL COMMENT '标的 tushare 代码',
  direction   ENUM('up','down') NOT NULL COMMENT '预判方向',
  driver      VARCHAR(32)     NOT NULL DEFAULT '未标注' COMMENT '驱动：涨价/逻辑/预期/情绪',
  reason      TEXT            NULL COMMENT '预判理由',
  extra       JSON            NULL COMMENT '附加信息',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_pred_date (pred_date),
  KEY idx_target (target)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日预判';

-- ---------- 3. 选股前向收益缓存（避免重复回算） ----------
CREATE TABLE IF NOT EXISTS selection_forward_returns (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  selection_id  BIGINT UNSIGNED NOT NULL COMMENT '关联 selections.id',
  horizon       SMALLINT        NOT NULL COMMENT '持有交易日数：1/3/7/30',
  ret_pct       DECIMAL(8,2)    NULL COMMENT '前向涨幅 %（前复权）',
  excess_pct    DECIMAL(8,2)    NULL COMMENT '相对沪深300超额 %',
  computed_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '计算时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sel_horizon (selection_id, horizon),
  CONSTRAINT fk_sfr_selection FOREIGN KEY (selection_id)
    REFERENCES selections (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股各持有期前向收益/超额缓存';

-- ---------- 4. 回测聚合结果快照 ----------
CREATE TABLE IF NOT EXISTS backtest_snapshots (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  kind        ENUM('selection','predictions') NOT NULL COMMENT '回测类型',
  as_of       DATETIME        NOT NULL COMMENT '计算时刻',
  payload     JSON            NOT NULL COMMENT '完整聚合结果（by_category/by_driver/tuning_hints 等）',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_kind_asof (kind, as_of)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测聚合结果历史留存';

-- ---------- 5. 全市场因子预计算（见 PRECOMPUTE_PLAN.md） ----------
CREATE TABLE IF NOT EXISTS daily_factors (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  trade_date  CHAR(8)         NOT NULL COMMENT '交易日 YYYYMMDD',
  code        VARCHAR(16)     NOT NULL COMMENT 'tushare 代码',
  factors     JSON            NOT NULL COMMENT '个股因子原始值 {mom_12_1,reversal_1m,...}',
  computed_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_df_date_code (trade_date, code),
  KEY idx_df_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全市场个股因子预计算（每日盘后一次，选股直接读）';

-- ---------- 6. 键值配置（因子/指标权重覆盖等可变配置） ----------
CREATE TABLE IF NOT EXISTS config_kv (
  k           VARCHAR(64)  NOT NULL COMMENT '配置键，如 factor_weights',
  v           JSON         NOT NULL COMMENT '配置值（JSON）',
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (k)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='可变配置键值（落库以便上云/多实例一致）';

-- ---------- 6之二. 配置变更留痕（类 commit 版本历史） ----------
CREATE TABLE IF NOT EXISTS config_versions (
  id             BIGINT       NOT NULL AUTO_INCREMENT,
  version_id     VARCHAR(16)  NOT NULL COMMENT '类 commit id（短哈希），全局唯一',
  config_key     VARCHAR(64)  NOT NULL COMMENT '配置键，如 factor_weights:stock / sentiment_window',
  actor          VARCHAR(64)  NOT NULL DEFAULT 'unknown' COMMENT '修改者身份（agent 署名）',
  reason         TEXT         COMMENT '修改原因（回测证据/背离说明等）',
  payload        JSON         NOT NULL COMMENT '该版本的完整配置内容（可据此回滚定位）',
  parent_version VARCHAR(16)  COMMENT '上一版本 version_id（可空）',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_cfgver_vid (version_id),
  KEY idx_cfgver_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='因子/情绪权重等配置每次变更的版本留痕，可按 version_id 定位/回滚';

-- ---------- 7. 每日情绪原始指标（情绪温度/择时底层数据） ----------
CREATE TABLE IF NOT EXISTS daily_sentiment (
  trade_date  CHAR(8)   NOT NULL COMMENT '交易日 YYYYMMDD',
  indicators  JSON      NOT NULL COMMENT '情绪原始指标 {adv_dec_ratio,limit_up/down,index_mom/body/amp,avg_price_mom/body/amp,sector_ratio,turnover,...}',
  computed_at DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日情绪原始指标（供温度与择时计算）';

-- ============================================================
-- 常用语句示例
-- ============================================================

-- 登记一条自动选股（对应 log_selection）
-- INSERT INTO selections (sel_date, code, name, score, driver, reason, category, extra)
-- VALUES ('2026-07-14', '600519.SH', '贵州茅台', 0.82, '逻辑', '基本面稳健', 'auto',
--         JSON_OBJECT('heat', 88, 'sector', '白酒'));

-- 写入/更新某条选股的前向收益（幂等 upsert）
-- INSERT INTO selection_forward_returns (selection_id, horizon, ret_pct, excess_pct)
-- VALUES (1, 30, 12.30, 6.50)
-- ON DUPLICATE KEY UPDATE ret_pct=VALUES(ret_pct), excess_pct=VALUES(excess_pct), computed_at=CURRENT_TIMESTAMP;

-- 自动选股 各持有期平均收益/胜率（等价前端图表数据）
-- SELECT r.horizon,
--        COUNT(*)                                            AS n,
--        ROUND(AVG(r.ret_pct), 2)                            AS avg_pct,
--        ROUND(AVG(r.excess_pct), 2)                         AS avg_excess_pct,
--        ROUND(SUM(r.ret_pct > 0) / COUNT(*) * 100, 1)       AS win_rate
-- FROM selection_forward_returns r
-- JOIN selections s ON s.id = r.selection_id
-- WHERE s.category = 'auto'
-- GROUP BY r.horizon
-- ORDER BY FIELD(r.horizon, 1, 3, 7, 30);

-- 自动选股 分驱动 30 日平均超额（用于调参）
-- SELECT s.driver,
--        COUNT(*)                    AS n,
--        ROUND(AVG(r.excess_pct), 2) AS avg_excess_30d
-- FROM selection_forward_returns r
-- JOIN selections s ON s.id = r.selection_id
-- WHERE s.category = 'auto' AND r.horizon = 30
-- GROUP BY s.driver
-- ORDER BY avg_excess_30d DESC;

-- 留存一次回测聚合结果
-- INSERT INTO backtest_snapshots (kind, as_of, payload)
-- VALUES ('selection', NOW(), CAST('{"total_selections":42}' AS JSON));

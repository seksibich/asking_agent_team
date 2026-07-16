-- ============================================================
-- 回测数据持久化 Schema（阿里云 RDS MySQL 8.0）
-- ------------------------------------------------------------
-- 背景：当前服务把选股/预判原始数据落在 DATA_DIR 下的
--   selections.jsonl / predictions.jsonl（Docker 卷 ./data）。
--   回测「结果」由 selection_backtest / predictions_backtest 每次
--   实时计算。为便于上云、并发查询与历史留存，这里给出关系库结构：
--     selections            —— 对应 log_selection 登记的每条选股
--     predictions           —— 对应 predictions.jsonl 的每条预判
--     selection_forward_returns / selection_forward_returns_v2 —— 前向收益缓存
--     backtest_snapshots    —— 留存某时刻计算出的回测聚合结果（JSON）
--     factor_contracts / screening_runs —— 因子契约与筛选运行审计
--     daily_factors / daily_sector_scores / daily_factor_runs —— 预计算结果及质量
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
  category    ENUM('auto','manual','watch','holding') NOT NULL DEFAULT 'auto'
              COMMENT 'auto=自动选股(用于调参)/manual=用户触发正式选股/watch=关注/holding=持仓',
  extra       JSON            NULL COMMENT '选股快照：selected_price/hotspot/event/market_role/factors/trigger 等',
  logged_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '登记时间',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_sel_code_cat (sel_date, code, category),
  KEY idx_sel_date (sel_date),
  KEY idx_code (code),
  KEY idx_category_date (category, sel_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='自动/关注/持仓 选股登记';


-- ---------- 1之二. 当前关注与持仓（按股票代码唯一） ----------
CREATE TABLE IF NOT EXISTS portfolio_items (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  code        VARCHAR(16)     NOT NULL COMMENT 'tushare 股票代码，全表唯一',
  name        VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '股票名称',
  item_type   ENUM('watch','holding') NOT NULL COMMENT 'watch=关注，holding=持仓',
  cost_price  DECIMAL(18,4)   NULL COMMENT '持仓成本价；关注类型为空',
  lots        INT             NULL COMMENT '持仓手数，一手=100股；关注类型为空',
  note        TEXT            NULL COMMENT '关注理由、持仓备注或风险说明',
  source      VARCHAR(32)     NOT NULL DEFAULT 'unknown' COMMENT 'web-admin/agent 等写入来源',
  created_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_portfolio_code (code),
  KEY idx_portfolio_type_updated (item_type, updated_at),
  CONSTRAINT chk_portfolio_position_fields CHECK (
    (item_type = 'watch' AND cost_price IS NULL AND lots IS NULL)
    OR (item_type = 'holding' AND cost_price > 0 AND lots > 0)
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='当前关注与持仓状态；同代码只保留最新一条';

CREATE TABLE IF NOT EXISTS portfolio_meta (
  id            TINYINT UNSIGNED NOT NULL COMMENT '固定为1的单行版本记录',
  revision      BIGINT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '内容变化时单调递增',
  content_hash  CHAR(64)         NOT NULL COMMENT '当前状态规范化 SHA-256',
  updated_at    DATETIME         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='关注与持仓独立数据版本';

-- ---------- 2. 预判登记（不可变下一交易日口径） ----------
CREATE TABLE IF NOT EXISTS predictions (
  id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  pred_date         DATE            NOT NULL COMMENT '预判日期 YYYY-MM-DD',
  target_trade_date DATE            NULL COMMENT '目标 SSE 交易日；legacy 记录为空',
  target            VARCHAR(32)     NOT NULL COMMENT '标的 tushare 代码',
  direction         ENUM('up','down') NOT NULL COMMENT '预判方向',
  driver            VARCHAR(32)     NOT NULL DEFAULT '未标注' COMMENT '驱动：涨价/逻辑/预期/情绪',
  reason            TEXT            NULL COMMENT '预判理由',
  extra             JSON            NULL COMMENT '附加信息与持有交易日口径',
  predicted_at      DATETIME        NULL COMMENT '上海时间预测时刻；legacy 记录为空',
  calc_version      VARCHAR(32)     NULL COMMENT '预测回测口径版本；legacy 记录为空',
  created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_pred_target (pred_date, target),
  KEY idx_pred_date (pred_date),
  KEY idx_pred_target_trade_date (target_trade_date),
  KEY idx_target (target)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='不可变方向预判；按目标交易日成熟后回测';

-- ---------- 3. 选股前向收益缓存（避免重复回算） ----------
CREATE TABLE IF NOT EXISTS selection_forward_returns (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  selection_id  BIGINT UNSIGNED NOT NULL COMMENT '关联 selections.id',
  horizon       SMALLINT        NOT NULL COMMENT '持有交易日数：1/3/7/30',
  ret_pct       DECIMAL(8,2)    NULL COMMENT '前向涨幅 %（前复权）',
  excess_pct    DECIMAL(8,2)    NULL COMMENT '相对沪深300超额 %',
  matured       TINYINT         NOT NULL DEFAULT 0 COMMENT '是否已满持有期：1=成熟',
  computed_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '计算时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sel_horizon (selection_id, horizon),
  CONSTRAINT fk_sfr_selection FOREIGN KEY (selection_id)
    REFERENCES selections (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股各持有期前向收益/超额缓存';

-- ---------- 3之二. 可版本化前向收益缓存 ----------
CREATE TABLE IF NOT EXISTS selection_forward_returns_v2 (
  id                     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  selection_id           BIGINT UNSIGNED NOT NULL COMMENT '关联 selections.id',
  horizon                SMALLINT        NOT NULL COMMENT '持有交易日数',
  calc_version           VARCHAR(32)     NOT NULL COMMENT '收益计算口径版本',
  status                 VARCHAR(16)     NOT NULL COMMENT 'pending/success/failed/not_matured',
  ret_pct                DECIMAL(12,6)   NULL COMMENT '前向收益率百分比',
  excess_pct             DECIMAL(12,6)   NULL COMMENT '相对基准超额百分比',
  entry_trade_date       CHAR(8)         NULL COMMENT '入场交易日 YYYYMMDD',
  entry_price            DECIMAL(18,6)   NULL COMMENT '入场价格',
  exit_trade_date        CHAR(8)         NULL COMMENT '退出交易日 YYYYMMDD',
  exit_price             DECIMAL(18,6)   NULL COMMENT '退出价格',
  benchmark_entry_price  DECIMAL(18,6)   NULL COMMENT '基准入场价格',
  benchmark_exit_price   DECIMAL(18,6)   NULL COMMENT '基准退出价格',
  error                  TEXT            NULL COMMENT '失败原因',
  computed_at            DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '计算时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_sfr2_selection_horizon_version (selection_id, horizon, calc_version),
  KEY idx_sfr2_selection (selection_id),
  CONSTRAINT fk_sfr2_selection FOREIGN KEY (selection_id)
    REFERENCES selections (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='按计算口径版本缓存选股前向收益，支持可复现回测';

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

-- ---------- 4之二. 因子契约（schema_hash 不可变） ----------
CREATE TABLE IF NOT EXISTS factor_contracts (
  schema_hash     VARCHAR(64)  NOT NULL COMMENT '因子结构与定义哈希',
  model           VARCHAR(64)  NOT NULL COMMENT '适用模型，如 stock/sector',
  factor_version  VARCHAR(32)  NOT NULL COMMENT '因子公式版本',
  components      JSON         NOT NULL COMMENT '有序因子组件列表或映射',
  definition      JSON         NOT NULL COMMENT '完整因子定义与口径',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (schema_hash),
  KEY idx_fc_model (model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='按 schema_hash 固化不可变因子契约，支持历史复现';

-- ---------- 4之三. 筛选运行审计 ----------
CREATE TABLE IF NOT EXISTS screening_runs (
  run_id           VARCHAR(64)  NOT NULL COMMENT '筛选运行唯一 ID',
  function_name    VARCHAR(64)  NOT NULL COMMENT '筛选功能名',
  trade_date       CHAR(8)      NOT NULL COMMENT '筛选交易日 YYYYMMDD',
  factor_version   VARCHAR(32)  NULL COMMENT '因子公式版本',
  schema_hash      VARCHAR(64)  NULL COMMENT '因子契约哈希',
  weight_version   VARCHAR(16)  NULL COMMENT '生效权重版本',
  contract         JSON         NOT NULL COMMENT '运行时完整契约快照',
  candidate_codes  JSON         NOT NULL COMMENT '本次候选代码集合',
  candidates       JSON         NULL COMMENT '候选排名、原始分、分位及契约元数据快照',
  params           JSON         NOT NULL COMMENT '运行参数快照',
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  KEY idx_sr_function (function_name),
  KEY idx_sr_trade_date (trade_date),
  KEY idx_sr_schema_hash (schema_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='筛选运行的契约、参数和候选集合审计记录';

-- ---------- 5. 全市场因子预计算（见 PRECOMPUTE_PLAN.md） ----------
CREATE TABLE IF NOT EXISTS daily_factors (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  trade_date  CHAR(8)         NOT NULL COMMENT '交易日 YYYYMMDD',
  code        VARCHAR(16)     NOT NULL COMMENT 'tushare 代码',
  factors     JSON            NOT NULL COMMENT '个股因子原始值 {mom_12_1,reversal_1m,...}',
  factor_version VARCHAR(32)  NULL COMMENT '因子公式版本；旧数据保持 NULL',
  schema_hash VARCHAR(64)     NULL COMMENT '因子契约哈希；旧数据保持 NULL',
  dependency_hash VARCHAR(64) NULL COMMENT '上游行业公式与权重依赖指纹；旧数据保持 NULL',
  dependencies JSON          NULL COMMENT '上游依赖的完整版本摘要',
  run_id      VARCHAR(64)     NULL COMMENT '所属预计算运行 ID；旧数据保持 NULL',
  computed_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_df_date_code (trade_date, code),
  KEY idx_df_date (trade_date),
  KEY idx_df_schema_hash (schema_hash),
  KEY idx_df_dependency_hash (dependency_hash),
  KEY idx_df_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全市场个股因子预计算（每日盘后一次，选股直接读）';

-- ---------- 5之二. 每日行业量化评分 ----------
CREATE TABLE IF NOT EXISTS daily_sector_scores (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
  trade_date  CHAR(8)         NOT NULL COMMENT '交易日 YYYYMMDD',
  code        VARCHAR(16)     NOT NULL COMMENT '申万一级行业指数代码',
  name        VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '行业名称',
  score       DECIMAL(12,6)   NOT NULL DEFAULT 0 COMMENT '行业多因子横截面综合分',
  percentile  DECIMAL(8,6)    NOT NULL DEFAULT 0 COMMENT '行业综合分横截面分位 0~1',
  factors     JSON            NOT NULL COMMENT '行业动量/量能/低波动原始因子',
  factor_version VARCHAR(32)  NULL COMMENT '因子公式版本；旧数据保持 NULL',
  schema_hash VARCHAR(64)     NULL COMMENT '因子契约哈希；旧数据保持 NULL',
  dependency_hash VARCHAR(64) NULL COMMENT '行业评分权重依赖指纹；旧数据保持 NULL',
  dependencies JSON          NULL COMMENT '行业公式与权重的完整版本摘要',
  run_id      VARCHAR(64)     NULL COMMENT '所属预计算运行 ID；旧数据保持 NULL',
  computed_at DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_dss_date_code (trade_date, code),
  KEY idx_dss_date_score (trade_date, score),
  KEY idx_dss_schema_hash (schema_hash),
  KEY idx_dss_dependency_hash (dependency_hash),
  KEY idx_dss_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日行业量化评分，供行业看板与个股行业因子复用';

-- ---------- 6. 全服务唯一预计算后台任务 ----------
CREATE TABLE IF NOT EXISTS precompute_jobs (
  task_key        VARCHAR(32)     NOT NULL COMMENT '固定任务键 daily_factors，保证全局唯一',
  job_id          VARCHAR(32)     NOT NULL COMMENT '本次执行唯一 ID',
  status          VARCHAR(16)     NOT NULL COMMENT 'queued/running/success/partial/failed/skipped',
  params          JSON            NOT NULL COMMENT '本次任务参数',
  progress        INT             NOT NULL DEFAULT 0 COMMENT '整体进度 0~100',
  stage           VARCHAR(64)     NOT NULL DEFAULT '等待执行' COMMENT '当前阶段',
  message         VARCHAR(500)    NOT NULL DEFAULT '' COMMENT '当前进度说明',
  current_date    CHAR(8)         NULL COMMENT '正在计算的交易日',
  completed_count INT             NOT NULL DEFAULT 0 COMMENT '已完成交易日数',
  total_count     INT             NOT NULL DEFAULT 0 COMMENT '总交易日数',
  result          JSON            NULL COMMENT '终态计算结果',
  error           TEXT            NULL COMMENT '任务级异常',
  started_at      DATETIME        NOT NULL COMMENT '认领时间',
  heartbeat_at    DATETIME        NOT NULL COMMENT '最近进度心跳',
  finished_at     DATETIME        NULL COMMENT '终态时间',
  PRIMARY KEY (task_key),
  UNIQUE KEY uk_pcj_job_id (job_id),
  KEY idx_pcj_status_heartbeat (status, heartbeat_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全市场因子预计算唯一后台任务与实时进度';

-- ---------- 6之二. 每日预计算结果质量 ----------
CREATE TABLE IF NOT EXISTS daily_factor_runs (
  trade_date      CHAR(8)         NOT NULL COMMENT '目标交易日 YYYYMMDD',
  factor_version  VARCHAR(32)     NOT NULL COMMENT '因子公式版本',
  schema_hash     VARCHAR(64)     NULL COMMENT '因子契约哈希；旧数据保持 NULL',
  dependency_hash VARCHAR(64)     NULL COMMENT '上游行业公式与权重依赖指纹',
  dependencies    JSON            NULL COMMENT '上游依赖完整版本摘要',
  factor_components JSON         NULL COMMENT '本次运行实际因子组件；旧数据保持 NULL',
  run_id          VARCHAR(64)     NULL COMMENT '预计算运行 ID；旧数据保持 NULL',
  lookback        INT             NOT NULL COMMENT '回看交易日数',
  universe_count  INT             NOT NULL DEFAULT 0 COMMENT '剔除 ST/退后的应计算股票数',
  computed_count  INT             NOT NULL DEFAULT 0 COMMENT '实际计算股票数',
  coverage_ratio  DECIMAL(8,4)    NOT NULL DEFAULT 0 COMMENT '覆盖率',
  status          VARCHAR(16)     NOT NULL COMMENT 'success/partial/failed/skipped',
  errors          JSON            NOT NULL COMMENT '错误和缺失信息',
  started_at      DATETIME        NOT NULL,
  finished_at     DATETIME        NOT NULL,
  PRIMARY KEY (trade_date),
  KEY idx_dfr_status (status),
  KEY idx_dfr_finished (finished_at),
  KEY idx_dfr_schema_hash (schema_hash),
  KEY idx_dfr_dependency_hash (dependency_hash),
  KEY idx_dfr_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全市场因子预计算任务状态与质量';

-- ---------- 7. 键值配置（因子/指标权重覆盖等可变配置） ----------
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
  factor_version VARCHAR(32) NULL COMMENT '情绪公式版本；旧数据保持 NULL',
  schema_hash VARCHAR(64) NULL COMMENT '情绪因子契约哈希；旧数据保持 NULL',
  computed_at DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (trade_date),
  KEY idx_sentiment_schema_hash (schema_hash)
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

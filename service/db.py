"""持久化数据库层（本地 SQLite / 上云 RDS MySQL 两用）。

- 通过环境变量 DB_URL 切换：
  - 未设置：默认本地 SQLite，文件在 DATA_DIR/stock_agent.db（Docker 卷持久化）
  - 上云：设为 RDS MySQL，如 mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
- 表结构与 service/db/schema.sql（RDS 权威 DDL）保持一致：
  selections / predictions / selection_forward_returns / backtest_snapshots /
  daily_factors / daily_sector_scores
- SQLAlchemy Core 定义，create_all() 在两种方言下都可建表；RDS 上也可直接执行 schema.sql。

对外提供幂等写入与查询帮助函数，供选股/预判/回测脚本使用。
"""
from __future__ import annotations

import hashlib
import json as _json
import math
import os
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from sqlalchemy import (JSON, BigInteger, CheckConstraint, Column, Date, DateTime,
                        Integer, MetaData, Numeric, SmallInteger, String, Table, Text,
                        UniqueConstraint, case, create_engine, func, inspect, select, text)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

import common

metadata = MetaData()


def _json_fingerprint(value: Any) -> str:
    """按因子契约同一规范 JSON 算法生成稳定 SHA-256 指纹。"""
    raw = _json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# 关注和持仓属于敏感类别。HTTP 请求通过 selection_read_scope 注入不可由
# 调用参数伪造的读取范围，fetch_selections 在 SQL 层统一执行行级过滤。
PUBLIC_SELECTION_CATEGORIES = frozenset({"auto", "manual"})
_selection_allowed_categories: ContextVar[Optional[frozenset[str]]] = ContextVar(
    "selection_allowed_categories", default=None)


@contextmanager
def selection_read_scope(include_sensitive: bool) -> Iterator[None]:
    """设置当前调用链的选股读取范围；管理员可读全部，访客仅可读公开类别。"""
    allowed = None if include_sensitive else PUBLIC_SELECTION_CATEGORIES
    token = _selection_allowed_categories.set(allowed)
    try:
        yield
    finally:
        _selection_allowed_categories.reset(token)


selections = Table(
    "selections", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("sel_date", Date, nullable=False, index=True),
    Column("code", String(16), nullable=False, index=True),
    Column("name", String(64), nullable=False, default=""),
    Column("score", Numeric(8, 4), nullable=False, default=0),
    Column("driver", String(32), nullable=False, default="未标注"),
    Column("reason", Text),
    Column("category", String(16), nullable=False, default="auto"),
    Column("extra", JSON),
    Column("logged_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("sel_date", "code", "category", name="uk_sel_code_cat"),
)

# 当前关注/持仓状态：每个股票代码只保留一条最新状态，与按日选股快照隔离。
portfolio_items = Table(
    "portfolio_items", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(16), nullable=False, unique=True, index=True),
    Column("name", String(64), nullable=False, default=""),
    Column("item_type", String(16), nullable=False),  # watch / holding
    Column("cost_price", Numeric(18, 4)),
    Column("lots", Integer),
    Column("note", Text),
    Column("source", String(32), nullable=False, default="unknown"),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
    Column("updated_at", DateTime, nullable=False, default=datetime.now),
)

# 自选数据独立版本：内容实际变化时 revision 单调递增，hash 校验当前快照。
portfolio_meta = Table(
    "portfolio_meta", metadata,
    Column("id", Integer, primary_key=True),
    Column("revision", Integer, nullable=False, default=0),
    Column("content_hash", String(64), nullable=False),
    Column("updated_at", DateTime, nullable=False, default=datetime.now),
)

predictions = Table(
    "predictions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pred_date", Date, nullable=False, index=True),
    Column("target_trade_date", Date, index=True),
    Column("target", String(32), nullable=False, index=True),
    Column("direction", String(8), nullable=False),
    Column("driver", String(32), nullable=False, default="未标注"),
    Column("reason", Text),
    Column("extra", JSON),
    Column("predicted_at", DateTime),
    Column("calc_version", String(32)),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("pred_date", "target", name="uk_pred_target"),
)

selection_forward_returns = Table(
    "selection_forward_returns", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("selection_id", Integer, nullable=False, index=True),
    Column("horizon", SmallInteger, nullable=False),
    Column("ret_pct", Numeric(8, 2)),
    Column("excess_pct", Numeric(8, 2)),
    Column("matured", Integer, nullable=False, default=0),  # 1=已满该持有期，结果不变
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("selection_id", "horizon", name="uk_sel_horizon"),
)

selection_forward_returns_v2 = Table(
    "selection_forward_returns_v2", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("selection_id", Integer, nullable=False, index=True),
    Column("horizon", SmallInteger, nullable=False),
    Column("calc_version", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("ret_pct", Numeric(12, 6)),
    Column("excess_pct", Numeric(12, 6)),
    Column("entry_trade_date", String(8)),
    Column("entry_price", Numeric(18, 6)),
    Column("exit_trade_date", String(8)),
    Column("exit_price", Numeric(18, 6)),
    Column("benchmark_entry_price", Numeric(18, 6)),
    Column("benchmark_exit_price", Numeric(18, 6)),
    Column("error", Text),
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("selection_id", "horizon", "calc_version",
                     name="uk_sfr2_selection_horizon_version"),
)

backtest_snapshots = Table(
    "backtest_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(16), nullable=False),
    Column("as_of", DateTime, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# 键值配置（因子权重覆盖等可变配置，落库以便上云/多实例一致）
config_kv = Table(
    "config_kv", metadata,
    Column("k", String(64), primary_key=True),
    Column("v", JSON, nullable=False),
    Column("updated_at", DateTime, nullable=False, default=datetime.now),
)

# 动态访客 Key：只保存高熵随机 Key 的 SHA-256 摘要和展示前缀。
# 明文只在创建响应中返回一次，列表和数据库均不可恢复原始 Key。
user_api_keys = Table(
    "user_api_keys", metadata,
    Column("id", String(24), primary_key=True),
    Column("label", String(64), nullable=False, default="访客"),
    Column("key_hash", String(64), nullable=False, unique=True, index=True),
    Column("key_prefix", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
    Column("disabled", SmallInteger, nullable=False, default=0),
)

# 配置变更留痕（因子/情绪权重、归一窗口等每次修改的版本历史，类 commit）
# 每条 = 一次生效的完整配置快照，可按 version_id 随时定位/回滚。
config_versions = Table(
    "config_versions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version_id", String(16), nullable=False, unique=True),  # 类 commit id（短哈希）
    Column("config_key", String(64), nullable=False, index=True),   # 如 factor_weights:stock / sentiment_window
    Column("actor", String(64), nullable=False, default="unknown"), # 修改者身份（agent 署名）
    Column("reason", Text),                                         # 修改原因（回测证据等）
    Column("payload", JSON, nullable=False),                        # 该版本的完整配置内容
    Column("parent_version", String(16)),                          # 上一版本 version_id（可为空）
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# 因子契约按 schema_hash 不可变保存，供历史结果准确复现。
factor_contracts = Table(
    "factor_contracts", metadata,
    Column("schema_hash", String(64), primary_key=True),
    Column("model", String(64), nullable=False, index=True),
    Column("factor_version", String(32), nullable=False),
    Column("components", JSON, nullable=False),
    Column("definition", JSON, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# 每次筛选固化候选集合、参数和当时契约，支持可审计回放。
screening_runs = Table(
    "screening_runs", metadata,
    Column("run_id", String(64), primary_key=True),
    Column("function_name", String(64), nullable=False, index=True),
    Column("trade_date", String(8), nullable=False, index=True),
    Column("factor_version", String(32)),
    Column("schema_hash", String(64), index=True),
    Column("weight_version", String(16)),
    Column("contract", JSON, nullable=False),
    Column("candidate_codes", JSON, nullable=False),
    Column("candidates", JSON),
    Column("params", JSON, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# 每日情绪原始指标（0-100 情绪温度 / 择时的底层数据，落库持久）
daily_sentiment = Table(
    "daily_sentiment", metadata,
    Column("trade_date", String(8), primary_key=True),   # YYYYMMDD
    Column("indicators", JSON, nullable=False),
    Column("factor_version", String(32)),
    Column("schema_hash", String(64), index=True),
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
)

# 全市场因子预计算表（见 service/db/PRECOMPUTE_PLAN.md）
daily_factors = Table(
    "daily_factors", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_date", String(8), nullable=False, index=True),  # YYYYMMDD
    Column("code", String(16), nullable=False, index=True),
    Column("factors", JSON, nullable=False),   # {mom_12_1, reversal_1m, ...}
    Column("factor_version", String(32)),
    Column("schema_hash", String(64), index=True),
    Column("dependency_hash", String(64), index=True),
    Column("dependencies", JSON),
    Column("run_id", String(64), index=True),
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("trade_date", "code", name="uk_df_date_code"),
)

# 每日行业评分：盘后与个股因子同批计算，供行业分析和个股行业因子复用。
daily_sector_scores = Table(
    "daily_sector_scores", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_date", String(8), nullable=False, index=True),
    Column("code", String(16), nullable=False, index=True),
    Column("name", String(64), nullable=False, default=""),
    Column("score", Numeric(12, 6), nullable=False, default=0),
    Column("percentile", Numeric(8, 6), nullable=False, default=0),
    Column("factors", JSON, nullable=False),
    Column("factor_version", String(32)),
    Column("schema_hash", String(64), index=True),
    Column("dependency_hash", String(64), index=True),
    Column("dependencies", JSON),
    Column("run_id", String(64), index=True),
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("trade_date", "code", name="uk_dss_date_code"),
)

daily_factor_runs = Table(
    "daily_factor_runs", metadata,
    Column("trade_date", String(8), primary_key=True),
    Column("factor_version", String(32), nullable=False),
    Column("schema_hash", String(64), index=True),
    Column("dependency_hash", String(64), index=True),
    Column("dependencies", JSON),
    Column("factor_components", JSON),
    Column("run_id", String(64), index=True),
    Column("lookback", Integer, nullable=False),
    Column("universe_count", Integer, nullable=False, default=0),
    Column("computed_count", Integer, nullable=False, default=0),
    Column("coverage_ratio", Numeric(8, 4), nullable=False, default=0),
    Column("status", String(16), nullable=False),  # success/partial/failed/skipped
    Column("errors", JSON, nullable=False, default=list),
    Column("started_at", DateTime, nullable=False, default=datetime.now),
    Column("finished_at", DateTime, nullable=False, default=datetime.now),
)

# 预计算后台任务：固定 task_key 保证全服务同一时刻只有一个活跃任务。
# 终态记录保留到下一次任务原子认领，供页面持续读取结果。
precompute_jobs = Table(
    "precompute_jobs", metadata,
    Column("task_key", String(32), primary_key=True),
    Column("job_id", String(32), nullable=False, unique=True),
    Column("status", String(16), nullable=False),  # queued/running/success/partial/failed/skipped
    Column("params", JSON, nullable=False, default=dict),
    Column("progress", Integer, nullable=False, default=0),
    Column("stage", String(64), nullable=False, default="等待执行"),
    Column("message", String(500), nullable=False, default=""),
    Column("current_date", String(8)),
    Column("completed_count", Integer, nullable=False, default=0),
    Column("total_count", Integer, nullable=False, default=0),
    Column("result", JSON),
    Column("error", Text),
    Column("started_at", DateTime, nullable=False, default=datetime.now),
    Column("heartbeat_at", DateTime, nullable=False, default=datetime.now),
    Column("finished_at", DateTime),
)

# 量化盯盘跨进程租约与运行状态：固定 task_key=quant_watch。
quant_watch_state = Table(
    "quant_watch_state", metadata,
    Column("task_key", String(32), primary_key=True),
    Column("owner_id", String(64)),
    Column("lease_until", DateTime),
    Column("fence_token", BigInteger, nullable=False, default=0),
    Column("next_scan_at", DateTime),
    Column("status", String(16), nullable=False, default="waiting"),
    Column("trade_date", String(8), index=True),
    Column("phase", String(32)),
    Column("last_scan_at", DateTime),
    Column("last_error", Text),
    Column("last_message_id", String(64)),
    Column("heartbeat_at", DateTime, nullable=False, default=datetime.now),
)

# 只保存每轮聚合结论，不保存全市场原始分钟快照。
quant_watch_messages = Table(
    "quant_watch_messages", metadata,
    Column("message_id", String(64), primary_key=True),
    Column("trade_date", String(8), nullable=False, index=True),
    Column("scanned_at", DateTime, nullable=False, index=True),
    Column("phase", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# WebSocket 票据只持久化摘要，原始票据仅返回给签发方。
quant_watch_tickets = Table(
    "quant_watch_tickets", metadata,
    Column("ticket_hash", String(64), primary_key=True),
    Column("role", String(16), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("expires_at", DateTime, nullable=False, index=True),
    Column("consumed_at", DateTime),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
)

# 通知事件按业务事件键全局幂等，避免多实例重复发送。
quant_watch_notification_events = Table(
    "quant_watch_notification_events", metadata,
    Column("event_key", String(255), primary_key=True),
    Column("trade_date", String(8), nullable=False, index=True),
    Column("status", String(16), nullable=False),
    Column("owner_id", String(64), nullable=False),
    Column("fence_token", BigInteger, nullable=False),
    Column("retry_count", Integer, nullable=False, default=0),
    Column("result", JSON),
    Column("claimed_at", DateTime, nullable=False, default=datetime.now),
    Column("finished_at", DateTime),
    CheckConstraint(
        "status IN ('claimed','success','partial','failed')",
        name="chk_qwne_status"),
)

_engine: Optional[Engine] = None
_quant_watch_claim_context: ContextVar[Optional[tuple[str, int]]] = ContextVar(
    "quant_watch_claim_context", default=None)


def db_url() -> str:
    url = os.getenv("DB_URL", "").strip()
    if url:
        return url
    common.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{common.DATA_DIR / 'stock_agent.db'}"


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = db_url()
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


def _user_api_key_hash(value: str) -> str:
    """为高熵访客 Key 生成不可逆摘要；数据库不保存可恢复明文。"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _insert_user_api_key(conn, values: dict[str, Any]) -> None:
    """按方言幂等插入访客 Key，供旧配置迁移与创建接口复用。"""
    dialect = conn.engine.dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
        conn.execute(dialect_insert(user_api_keys).values(**values).on_conflict_do_nothing())
    elif dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert as dialect_insert
        conn.execute(dialect_insert(user_api_keys).values(**values).prefix_with("IGNORE"))
    else:
        try:
            with conn.begin_nested():
                conn.execute(user_api_keys.insert().values(**values))
        except IntegrityError:
            pass


def _migrate_legacy_user_api_keys(conn) -> None:
    """把旧 config_kv 明文数组一次性迁移为摘要表，并删除旧明文。"""
    row = conn.execute(select(config_kv.c.v).where(
        config_kv.c.k == "user_api_keys")).first()
    value = row[0] if row else None
    keys = value.get("keys") if isinstance(value, dict) else None
    if not isinstance(keys, list):
        return
    for item in keys:
        if not isinstance(item, dict):
            continue
        raw_key = str(item.get("key") or "").strip()
        if not raw_key:
            continue
        created_at = item.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                created_at = None
        _insert_user_api_key(conn, {
            "id": str(item.get("id") or secrets.token_hex(6))[:24],
            "label": str(item.get("label") or "访客")[:64],
            "key_hash": _user_api_key_hash(raw_key),
            "key_prefix": raw_key[:24],
            "created_at": created_at or datetime.now(),
            "disabled": int(bool(item.get("disabled"))),
        })
    conn.execute(config_kv.delete().where(config_kv.c.k == "user_api_keys"))


def _ensure_columns(engine: Engine, table_name: str,
                    columns: dict[str, tuple[str, str]]) -> None:
    """通过反射为旧表补可空列；columns 的值为 SQLite/MySQL 类型声明。"""
    existing = {column["name"] for column in inspect(engine).get_columns(table_name)}
    dialect_index = 0 if engine.dialect.name == "sqlite" else 1
    for column_name, declarations in columns.items():
        if column_name in existing:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
                f"{declarations[dialect_index]}"))


def _portfolio_content_hash(rows: list[dict[str, Any]]) -> str:
    """只对当前业务字段生成稳定哈希，写入时间和来源不影响数据版本。"""
    normalized = []
    for row in sorted(rows, key=lambda item: str(item.get("code") or "")):
        cost = row.get("cost_price")
        normalized.append({
            "code": str(row.get("code") or ""),
            "name": str(row.get("name") or ""),
            "type": str(row.get("item_type") or row.get("type") or ""),
            "cost_price": None if cost is None else f"{float(cost):.4f}",
            "lots": None if row.get("lots") is None else int(row["lots"]),
            "note": str(row.get("note") or ""),
        })
    return _json_fingerprint(normalized)


def _portfolio_version_text(revision: int, content_hash: str) -> str:
    return f"pv{int(revision)}.{str(content_hash)[:8]}"


def init_db() -> None:
    """建表并通过 inspect 幂等补齐 SQLite/MySQL 旧表列。"""
    engine = get_engine()
    metadata.create_all(engine)
    # 旧版动态访客 Key 曾以明文 JSON 存在 config_kv；建表后立即迁移并删除明文。
    with engine.begin() as conn:
        _migrate_legacy_user_api_keys(conn)
    # 自选版本元数据固定为单行；旧库首次升级时从空/既有当前状态建立 revision=0 基线。
    with engine.begin() as conn:
        meta_row = conn.execute(select(portfolio_meta.c.id).where(
            portfolio_meta.c.id == 1)).first()
        if not meta_row:
            rows = conn.execute(select(
                portfolio_items.c.code, portfolio_items.c.name,
                portfolio_items.c.item_type, portfolio_items.c.cost_price,
                portfolio_items.c.lots, portfolio_items.c.note,
            ).order_by(portfolio_items.c.code)).mappings().all()
            content_hash = _portfolio_content_hash([dict(row) for row in rows])
            conn.execute(portfolio_meta.insert().values(
                id=1, revision=0, content_hash=content_hash, updated_at=datetime.now()))
    # 旧版 RDS schema 曾遗漏 matured，create_all 不会给已有表补列。
    columns = {column["name"] for column in inspect(engine).get_columns("selection_forward_returns")}
    if "matured" not in columns:
        sql = "ALTER TABLE selection_forward_returns ADD COLUMN matured "
        sql += "INTEGER NOT NULL DEFAULT 0" if engine.dialect.name == "sqlite" else "TINYINT NOT NULL DEFAULT 0"
        with engine.begin() as conn:
            conn.execute(text(sql))

    nullable_metadata_columns = {
        "factor_version": ("VARCHAR(32) NULL", "VARCHAR(32) NULL"),
        "schema_hash": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
        "dependency_hash": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
        "dependencies": ("JSON NULL", "JSON NULL"),
        "run_id": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
    }
    _ensure_columns(engine, "daily_factors", nullable_metadata_columns)
    _ensure_columns(engine, "daily_sector_scores", nullable_metadata_columns)
    _ensure_columns(engine, "daily_factor_runs", {
        "schema_hash": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
        "dependency_hash": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
        "dependencies": ("JSON NULL", "JSON NULL"),
        "factor_components": ("JSON NULL", "JSON NULL"),
        "run_id": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
    })
    _ensure_columns(engine, "daily_sentiment", {
        "factor_version": ("VARCHAR(32) NULL", "VARCHAR(32) NULL"),
        "schema_hash": ("VARCHAR(64) NULL", "VARCHAR(64) NULL"),
    })
    _ensure_columns(engine, "screening_runs", {
        "candidates": ("JSON NULL", "JSON NULL"),
    })
    _ensure_columns(engine, "predictions", {
        "target_trade_date": ("DATE NULL", "DATE NULL"),
        "predicted_at": ("DATETIME NULL", "DATETIME NULL"),
        "calc_version": ("VARCHAR(32) NULL", "VARCHAR(32) NULL"),
    })
    _ensure_columns(engine, "quant_watch_state", {
        "fence_token": ("BIGINT NOT NULL DEFAULT 0", "BIGINT NOT NULL DEFAULT 0"),
        "next_scan_at": ("DATETIME NULL", "DATETIME NULL"),
    })
    _ensure_columns(engine, "quant_watch_notification_events", {
        "retry_count": ("INTEGER NOT NULL DEFAULT 0", "INT NOT NULL DEFAULT 0"),
    })

    # 旧版 MySQL selections.category 为 ENUM，补入 manual 以隔离用户触发正式选股。
    if engine.dialect.name == "mysql":
        category_column = next((column for column in inspect(engine).get_columns("selections")
                                if column["name"] == "category"), None)
        enum_values = getattr(category_column.get("type"), "enums", []) if category_column else []
        if enum_values and "manual" not in enum_values:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE selections MODIFY COLUMN category "
                    "ENUM('auto','manual','watch','holding') NOT NULL DEFAULT 'auto'"))


# ---------- 当前关注与持仓 ----------
def _serialize_portfolio_row(row: dict[str, Any]) -> dict[str, Any]:
    cost = row.get("cost_price")
    lots = row.get("lots")
    return {
        "id": row.get("id"),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "type": str(row.get("item_type") or ""),
        "cost_price": float(cost) if cost is not None else None,
        "lots": int(lots) if lots is not None else None,
        "shares": int(lots) * 100 if lots is not None else None,
        "note": str(row.get("note") or ""),
        "source": str(row.get("source") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
    }


def get_portfolio_version() -> str:
    """返回独立于功能索引的当前自选数据版本。"""
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(portfolio_meta).where(
            portfolio_meta.c.id == 1)).mappings().first()
    if not row:
        return "pv0.uninitialized"
    return _portfolio_version_text(int(row["revision"]), str(row["content_hash"]))


def _portfolio_snapshot(conn) -> dict[str, Any]:
    """在同一连接/事务中读取自选行与版本，避免响应中的内容和版本错位。"""
    rows = conn.execute(select(portfolio_items).order_by(
        text("CASE WHEN item_type = 'holding' THEN 0 ELSE 1 END"),
        portfolio_items.c.code)).mappings().all()
    meta = conn.execute(select(portfolio_meta).where(
        portfolio_meta.c.id == 1)).mappings().first()
    version = (_portfolio_version_text(int(meta["revision"]), str(meta["content_hash"]))
               if meta else "pv0.uninitialized")
    serialized = [_serialize_portfolio_row(dict(row)) for row in rows]
    return {"portfolio_version": version, "rows": serialized,
            "holding_count": sum(row["type"] == "holding" for row in serialized),
            "watch_count": sum(row["type"] == "watch" for row in serialized)}


def fetch_portfolio_items() -> dict[str, Any]:
    """获取按持仓优先、代码排序的当前关注与持仓。"""
    eng = get_engine()
    with eng.connect() as conn:
        return _portfolio_snapshot(conn)


def apply_portfolio_upload(items: list[dict[str, Any]], source: str) -> dict[str, Any]:
    """按代码批量 upsert/delete；同一批次重复代码最后一项生效，实际变化才升版。"""
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        deduped[str(item["code"])] = item

    eng = get_engine()
    now = datetime.now()
    counts = {"inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    with eng.begin() as conn:
        meta_stmt = select(portfolio_meta).where(portfolio_meta.c.id == 1)
        if eng.dialect.name == "mysql":
            meta_stmt = meta_stmt.with_for_update()
        meta = conn.execute(meta_stmt).mappings().first()
        if not meta:
            empty_hash = _portfolio_content_hash([])
            conn.execute(portfolio_meta.insert().values(
                id=1, revision=0, content_hash=empty_hash, updated_at=now))
            meta = {"revision": 0, "content_hash": empty_hash}

        current_stmt = select(portfolio_items)
        if eng.dialect.name == "mysql":
            current_stmt = current_stmt.with_for_update()
        current = {str(row["code"]): dict(row)
                   for row in conn.execute(current_stmt).mappings().all()}

        for code, item in deduped.items():
            existing = current.get(code)
            if item.get("deleted"):
                if existing:
                    conn.execute(portfolio_items.delete().where(portfolio_items.c.code == code))
                    current.pop(code, None)
                    counts["deleted"] += 1
                else:
                    counts["unchanged"] += 1
                continue

            values = {
                "code": code,
                "name": str(item["name"]),
                "item_type": str(item["type"]),
                "cost_price": item.get("cost_price"),
                "lots": item.get("lots"),
                "note": str(item.get("note") or ""),
                "source": source,
                "updated_at": now,
            }
            if existing:
                old_cost = None if existing.get("cost_price") is None else round(float(existing["cost_price"]), 4)
                new_cost = None if values["cost_price"] is None else round(float(values["cost_price"]), 4)
                unchanged = (
                    str(existing.get("name") or "") == values["name"]
                    and str(existing.get("item_type") or "") == values["item_type"]
                    and old_cost == new_cost
                    and existing.get("lots") == values["lots"]
                    and str(existing.get("note") or "") == values["note"]
                )
                if unchanged:
                    counts["unchanged"] += 1
                    continue
                conn.execute(portfolio_items.update().where(
                    portfolio_items.c.code == code).values(**values))
                counts["updated"] += 1
            else:
                values["created_at"] = now
                result = conn.execute(portfolio_items.insert().values(**values))
                values["id"] = int(result.inserted_primary_key[0])
                counts["inserted"] += 1
            current[code] = values

        changed = bool(counts["inserted"] or counts["updated"] or counts["deleted"])
        if changed:
            hash_rows = conn.execute(select(
                portfolio_items.c.code, portfolio_items.c.name,
                portfolio_items.c.item_type, portfolio_items.c.cost_price,
                portfolio_items.c.lots, portfolio_items.c.note,
            ).order_by(portfolio_items.c.code)).mappings().all()
            content_hash = _portfolio_content_hash([dict(row) for row in hash_rows])
            revision = int(meta["revision"]) + 1
            conn.execute(portfolio_meta.update().where(portfolio_meta.c.id == 1).values(
                revision=revision, content_hash=content_hash, updated_at=now))

        # 快照必须在本事务内读取，确保 rows 与 portfolio_version 来自同一状态。
        snapshot = _portfolio_snapshot(conn)

    snapshot.update(counts)
    snapshot.update({
        "changed": changed,
        "received_count": len(items),
        "deduplicated_count": len(deduped),
    })
    return snapshot


# ---------- selections ----------
def upsert_selection(rec: dict[str, Any]) -> dict[str, Any]:
    """按唯一键原子写入；auto/manual 首次固化，watch/holding 后写覆盖。"""
    eng = get_engine()
    immutable = rec["category"] in {"auto", "manual"}
    payload = {key: rec.get(key) for key in (
        "sel_date", "code", "name", "score", "driver", "reason",
        "category", "extra", "logged_at")}
    unique_where = (
        (selections.c.sel_date == rec["sel_date"])
        & (selections.c.code == rec["code"])
        & (selections.c.category == rec["category"])
    )
    with eng.begin() as conn:
        existing = conn.execute(select(selections).where(unique_where)).mappings().first()
        if existing and immutable:
            record = dict(existing)
            return {"inserted": False, "id": record["id"], "record": record}

        if eng.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            statement = dialect_insert(selections).values(**payload)
            statement = (statement.on_conflict_do_nothing(
                index_elements=["sel_date", "code", "category"])
                if immutable else statement.on_conflict_do_update(
                    index_elements=["sel_date", "code", "category"], set_=payload))
            result = conn.execute(statement)
        elif eng.dialect.name == "mysql":
            from sqlalchemy.dialects.mysql import insert as dialect_insert
            statement = dialect_insert(selections).values(**payload)
            updates = {"id": selections.c.id} if immutable else payload
            result = conn.execute(statement.on_duplicate_key_update(**updates))
        else:
            if existing:
                result = conn.execute(selections.update().where(unique_where).values(**payload))
            else:
                try:
                    with conn.begin_nested():
                        result = conn.execute(selections.insert().values(**payload))
                except IntegrityError:
                    result = None

        record = conn.execute(select(selections).where(unique_where)).mappings().one()
        inserted = existing is None and bool(result is not None and result.rowcount == 1)
        return {"inserted": inserted, "id": int(record["id"]), "record": dict(record)}


def fetch_selections(date_from: Optional[Any] = None, date_to: Optional[Any] = None,
                     category: Optional[str] = None) -> list[dict[str, Any]]:
    """查询选股记录；请求级读取范围会在 SQL 层排除无权访问的类别。"""
    allowed = _selection_allowed_categories.get()
    if category and allowed is not None and category not in allowed:
        return []

    eng = get_engine()
    stmt = select(selections)
    if date_from is not None:
        stmt = stmt.where(selections.c.sel_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(selections.c.sel_date <= date_to)
    if category:
        stmt = stmt.where(selections.c.category == category)
    elif allowed is not None:
        stmt = stmt.where(selections.c.category.in_(sorted(allowed)))
    stmt = stmt.order_by(selections.c.sel_date.desc(), selections.c.logged_at.desc())
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def fetch_selections_by_ids(selection_ids: list[int]) -> list[dict[str, Any]]:
    """按主键批量读取当前权限可见记录，供当前列表行情刷新使用。"""
    ids = sorted({int(value) for value in selection_ids if int(value) > 0})
    if not ids:
        return []
    allowed = _selection_allowed_categories.get()
    stmt = select(selections).where(selections.c.id.in_(ids))
    if allowed is not None:
        stmt = stmt.where(selections.c.category.in_(sorted(allowed)))
    with get_engine().connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(row) for row in rows]


def patch_selection_price_if_missing(selection_id: int, selected_price: float,
                                     trade_date: str, source: str) -> dict[str, Any]:
    """仅在选股价缺失时补选股日收盘价；不改变 immutable 记录的其他字段。"""
    price = float(selected_price)
    if not math.isfinite(price) or price <= 0:
        return {"updated": False, "reason": "invalid_price", "id": selection_id}
    eng = get_engine()
    with eng.begin() as conn:
        stmt = select(selections).where(selections.c.id == int(selection_id))
        if eng.dialect.name == "mysql":
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        if not row:
            return {"updated": False, "reason": "not_found", "id": selection_id}
        record = dict(row)
        extra = dict(record.get("extra") or {})
        try:
            current = float(extra.get("selected_price"))
        except (TypeError, ValueError):
            current = 0.0
        if math.isfinite(current) and current > 0:
            return {"updated": False, "reason": "already_present", "id": selection_id,
                    "selected_price": current, "record": record}
        now = datetime.now()
        extra.update({
            "selected_price": price,
            "price_trade_date": str(trade_date).replace("-", ""),
            "price_source": str(source or "tushare daily close"),
            "price_backfilled_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        extra.pop("price_error", None)
        conn.execute(selections.update().where(
            selections.c.id == int(selection_id)).values(extra=extra))
        updated = conn.execute(select(selections).where(
            selections.c.id == int(selection_id))).mappings().one()
    return {"updated": True, "reason": "backfilled", "id": selection_id,
            "selected_price": price, "record": dict(updated)}


def delete_selection(selection_id: int, confirm_code: str) -> dict[str, Any]:
    """按主键删除一条选股及两版关联收益；回测快照保留用于历史审计。"""
    eng = get_engine()
    with eng.begin() as conn:
        stmt = select(selections).where(selections.c.id == selection_id)
        if eng.dialect.name == "mysql":
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        if not row:
            return {"deleted": False, "reason": "not_found", "id": selection_id}

        record = dict(row)
        expected_code = str(record.get("code") or "").strip().upper()
        if str(confirm_code or "").strip().upper() != expected_code:
            return {"deleted": False, "reason": "confirm_mismatch", "id": selection_id}

        v2_result = conn.execute(selection_forward_returns_v2.delete().where(
            selection_forward_returns_v2.c.selection_id == selection_id))
        legacy_result = conn.execute(selection_forward_returns.delete().where(
            selection_forward_returns.c.selection_id == selection_id))
        selection_result = conn.execute(selections.delete().where(selections.c.id == selection_id))
        return {
            "deleted": bool(selection_result.rowcount),
            "selection": {
                "id": selection_id,
                "date": str(record.get("sel_date") or ""),
                "code": expected_code,
                "name": str(record.get("name") or ""),
                "category": str(record.get("category") or ""),
            },
            "deleted_counts": {
                "selections": int(selection_result.rowcount or 0),
                "selection_forward_returns_v2": int(v2_result.rowcount or 0),
                "selection_forward_returns": int(legacy_result.rowcount or 0),
            },
            "backtest_snapshots_preserved": True,
        }


# ---------- forward returns cache ----------
def get_cached_returns(selection_id: int) -> dict[int, dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(selection_forward_returns).where(
                selection_forward_returns.c.selection_id == selection_id)
        ).mappings().all()
    return {int(r["horizon"]): dict(r) for r in rows}


def save_return(selection_id: int, horizon: int, ret_pct: Optional[float],
                excess_pct: Optional[float], matured: bool) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        row = conn.execute(
            select(selection_forward_returns.c.id).where(
                selection_forward_returns.c.selection_id == selection_id,
                selection_forward_returns.c.horizon == horizon)
        ).first()
        vals = {"ret_pct": ret_pct, "excess_pct": excess_pct,
                "matured": 1 if matured else 0, "computed_at": datetime.now()}
        if row:
            conn.execute(selection_forward_returns.update()
                         .where(selection_forward_returns.c.id == row[0]).values(**vals))
        else:
            conn.execute(selection_forward_returns.insert().values(
                selection_id=selection_id, horizon=horizon, **vals))


def get_cached_returns_v2(selection_id: int, calc_version: str) -> dict[int, dict[str, Any]]:
    """读取指定计算版本的前向收益缓存，按 horizon 建索引。"""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(selection_forward_returns_v2).where(
            selection_forward_returns_v2.c.selection_id == selection_id,
            selection_forward_returns_v2.c.calc_version == calc_version,
        )).mappings().all()
    return {int(row["horizon"]): dict(row) for row in rows}


def save_return_v2(selection_id: int, horizon: int, calc_version: str, status: str,
                   ret_pct: Optional[float] = None, excess_pct: Optional[float] = None,
                   entry_trade_date: Optional[str] = None,
                   entry_price: Optional[float] = None,
                   exit_trade_date: Optional[str] = None,
                   exit_price: Optional[float] = None,
                   benchmark_entry_price: Optional[float] = None,
                   benchmark_exit_price: Optional[float] = None,
                   error: Optional[str] = None) -> dict[str, Any]:
    """按 (selection_id, horizon, calc_version) 原子 upsert 并返回缓存行。"""
    values = {
        "selection_id": selection_id,
        "horizon": horizon,
        "calc_version": calc_version,
        "status": status,
        "ret_pct": ret_pct,
        "excess_pct": excess_pct,
        "entry_trade_date": entry_trade_date,
        "entry_price": entry_price,
        "exit_trade_date": exit_trade_date,
        "exit_price": exit_price,
        "benchmark_entry_price": benchmark_entry_price,
        "benchmark_exit_price": benchmark_exit_price,
        "error": error,
        "computed_at": datetime.now(),
    }
    eng = get_engine()
    with eng.begin() as conn:
        if eng.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
            stmt = dialect_insert(selection_forward_returns_v2).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["selection_id", "horizon", "calc_version"],
                set_={key: value for key, value in values.items()
                      if key not in {"selection_id", "horizon", "calc_version"}},
            )
            conn.execute(stmt)
        elif eng.dialect.name == "mysql":
            from sqlalchemy.dialects.mysql import insert as dialect_insert
            stmt = dialect_insert(selection_forward_returns_v2).values(**values)
            stmt = stmt.on_duplicate_key_update(**{
                key: value for key, value in values.items()
                if key not in {"selection_id", "horizon", "calc_version"}
            })
            conn.execute(stmt)
        else:
            updated = conn.execute(selection_forward_returns_v2.update().where(
                selection_forward_returns_v2.c.selection_id == selection_id,
                selection_forward_returns_v2.c.horizon == horizon,
                selection_forward_returns_v2.c.calc_version == calc_version,
            ).values(**values))
            if not updated.rowcount:
                conn.execute(selection_forward_returns_v2.insert().values(**values))
        row = conn.execute(select(selection_forward_returns_v2).where(
            selection_forward_returns_v2.c.selection_id == selection_id,
            selection_forward_returns_v2.c.horizon == horizon,
            selection_forward_returns_v2.c.calc_version == calc_version,
        )).mappings().one()
    return dict(row)


# ---------- predictions ----------
def upsert_prediction(rec: dict[str, Any]) -> dict[str, Any]:
    """不可变登记预判；同日同标的重复返回原记录，反向冲突拒绝覆盖。"""
    eng = get_engine()
    with eng.begin() as conn:
        existing = conn.execute(select(predictions).where(
            predictions.c.pred_date == rec["pred_date"],
            predictions.c.target == rec["target"],
        ).order_by(predictions.c.id)).mappings().first()
        if existing:
            record = dict(existing)
            return {
                "inserted": False,
                "conflict": record.get("direction") != rec.get("direction"),
                "record": record,
            }
        payload = {key: rec.get(key) for key in (
            "pred_date", "target_trade_date", "target", "direction", "driver",
            "reason", "extra", "predicted_at", "calc_version")}
        result = conn.execute(predictions.insert().values(**payload))
        row = conn.execute(select(predictions).where(
            predictions.c.id == result.inserted_primary_key[0])).mappings().one()
    return {"inserted": True, "conflict": False, "record": dict(row)}


def fetch_predictions(pred_date: Optional[str] = None) -> list[dict[str, Any]]:
    eng = get_engine()
    stmt = select(predictions).order_by(predictions.c.pred_date, predictions.c.id)
    if pred_date:
        stmt = stmt.where(predictions.c.pred_date == pred_date)
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


# ---------- snapshots ----------
def save_snapshot(kind: str, payload: dict[str, Any]) -> int:
    """保存回测快照并返回插入主键。"""
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(backtest_snapshots.insert().values(
            kind=kind, as_of=datetime.now(), payload=payload))
        return int(result.inserted_primary_key[0])


def get_snapshot(id: int) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(backtest_snapshots).where(
            backtest_snapshots.c.id == id)).mappings().first()
    return dict(row) if row else None


# ---------- factor_contracts / screening_runs ----------
def _save_factor_contract_conn(conn, contract: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(select(factor_contracts).where(
        factor_contracts.c.schema_hash == contract["schema_hash"])).mappings().first()
    if not row:
        values = {key: contract.get(key) for key in (
            "schema_hash", "model", "factor_version", "components", "definition")}
        values["created_at"] = contract.get("created_at") or datetime.now()
        conn.execute(factor_contracts.insert().values(**values))
        row = conn.execute(select(factor_contracts).where(
            factor_contracts.c.schema_hash == contract["schema_hash"])).mappings().one()
    return dict(row)


def _save_screening_run_conn(conn, record: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(select(screening_runs).where(
        screening_runs.c.run_id == record["run_id"])).mappings().first()
    if not row:
        values = {key: record.get(key) for key in (
            "run_id", "function_name", "trade_date", "factor_version",
            "schema_hash", "weight_version", "contract", "candidate_codes",
            "candidates", "params")}
        values["created_at"] = record.get("created_at") or datetime.now()
        conn.execute(screening_runs.insert().values(**values))
        row = conn.execute(select(screening_runs).where(
            screening_runs.c.run_id == record["run_id"])).mappings().one()
    return dict(row)


def save_factor_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """按 schema_hash 不可变保存因子契约；重复保存直接返回原契约。"""
    with get_engine().begin() as conn:
        return _save_factor_contract_conn(conn, contract)


def get_factor_contract(schema_hash: str) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(factor_contracts).where(
            factor_contracts.c.schema_hash == schema_hash)).mappings().first()
    return dict(row) if row else None


def save_screening_run(record: dict[str, Any]) -> dict[str, Any]:
    """按 run_id 不可变保存筛选运行记录，重复保存返回原记录。"""
    with get_engine().begin() as conn:
        return _save_screening_run_conn(conn, record)


def save_screening_snapshot(contract: dict[str, Any],
                            record: dict[str, Any]) -> dict[str, Any]:
    """在同一事务内固化因子契约和筛选运行，避免出现半条证据链。"""
    if str(record.get("schema_hash") or "") != str(contract.get("schema_hash") or ""):
        raise ValueError("筛选运行与因子契约的 schema_hash 不一致")
    with get_engine().begin() as conn:
        saved_contract = _save_factor_contract_conn(conn, contract)
        saved_run = _save_screening_run_conn(conn, record)
    return {"contract": saved_contract, "run": saved_run}


def get_screening_run(run_id: str) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(screening_runs).where(
            screening_runs.c.run_id == run_id)).mappings().first()
    return dict(row) if row else None


def screening_run_has_candidate(run_id: str, code: str) -> bool:
    run = get_screening_run(run_id)
    return bool(run and code in (run.get("candidate_codes") or []))


def get_screening_candidate(run_id: str, code: str) -> Optional[dict[str, Any]]:
    """读取筛选时不可变候选快照，供正式选股核对排名与分数。"""
    run = get_screening_run(run_id)
    if not run:
        return None
    return next((dict(item) for item in (run.get("candidates") or [])
                 if str(item.get("code", "")).upper() == code.upper()), None)


# ---------- daily_factors（预计算结果与任务） ----------
ACTIVE_PRECOMPUTE_STATUSES = ("queued", "running")
PRECOMPUTE_TASK_KEY = "daily_factors"


def replace_daily_factors(trade_date: str, items: list[dict[str, Any]],
                          factor_version: Optional[str] = None,
                          schema_hash: Optional[str] = None,
                          run_id: Optional[str] = None,
                          dependency_hash: Optional[str] = None,
                          dependencies: Optional[dict[str, Any]] = None) -> int:
    """在一个事务内整日替换因子，并把契约元数据写入每一行。"""
    eng = get_engine()
    now = datetime.now()
    with eng.begin() as conn:
        conn.execute(daily_factors.delete().where(daily_factors.c.trade_date == trade_date))
        if items:
            conn.execute(daily_factors.insert(), [{
                "trade_date": trade_date,
                "code": item["code"],
                "factors": item["factors"],
                "factor_version": factor_version,
                "schema_hash": schema_hash,
                "dependency_hash": dependency_hash,
                "dependencies": dependencies,
                "run_id": run_id,
                "computed_at": now,
            } for item in items])
    return len(items)


def upsert_daily_factor_run(record: dict[str, Any]) -> None:
    """记录单个交易日的预计算质量与契约元数据。"""
    eng = get_engine()
    required_keys = (
        "factor_version", "lookback", "universe_count", "computed_count",
        "coverage_ratio", "status", "errors", "started_at", "finished_at")
    values = {key: record[key] for key in required_keys}
    values.update({key: record.get(key) for key in (
        "schema_hash", "dependency_hash", "dependencies", "factor_components", "run_id")})
    with eng.begin() as conn:
        updated = conn.execute(daily_factor_runs.update().where(
            daily_factor_runs.c.trade_date == record["trade_date"]).values(**values))
        if not updated.rowcount:
            conn.execute(daily_factor_runs.insert().values(
                trade_date=record["trade_date"], **values))


def _normalize_daily_factor_run(row: dict[str, Any]) -> dict[str, Any]:
    """把数据库类型转为可直接 JSON 序列化的值。"""
    item = dict(row)
    if item.get("coverage_ratio") is not None:
        item["coverage_ratio"] = float(item["coverage_ratio"])
    for key in ("started_at", "finished_at"):
        if item.get(key):
            item[key] = item[key].strftime("%Y-%m-%d %H:%M:%S")
    return item


def _normalize_precompute_job(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("started_at", "heartbeat_at", "finished_at"):
        if item.get(key):
            item[key] = item[key].strftime("%Y-%m-%d %H:%M:%S")
    item["active"] = item.get("status") in ACTIVE_PRECOMPUTE_STATUSES
    return item


def get_precompute_job() -> Optional[dict[str, Any]]:
    """读取当前活跃任务或最近一次终态任务。"""
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(precompute_jobs).where(
            precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY)).mappings().first()
    return _normalize_precompute_job(dict(row)) if row else None


def claim_precompute_job(job_id: str, params: dict[str, Any],
                         stale_after_minutes: int = 10) -> tuple[bool, dict[str, Any]]:
    """原子认领全局唯一预计算任务；活跃时返回同一任务而不重复启动。

    心跳超过阈值的任务视为进程异常退出，先标记失败再允许新任务认领。
    固定主键约束同时覆盖多线程、多 worker 和多服务实例。
    """
    eng = get_engine()
    now = datetime.now()
    stale_before = now - timedelta(minutes=stale_after_minutes)
    values = {
        "job_id": job_id,
        "status": "queued",
        "params": params,
        "progress": 0,
        "stage": "等待执行",
        "message": "任务已进入后台队列",
        "current_date": None,
        "completed_count": 0,
        "total_count": 0,
        "result": None,
        "error": None,
        "started_at": now,
        "heartbeat_at": now,
        "finished_at": None,
    }
    try:
        with eng.begin() as conn:
            conn.execute(precompute_jobs.update().where(
                precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
                precompute_jobs.c.status.in_(ACTIVE_PRECOMPUTE_STATUSES),
                precompute_jobs.c.heartbeat_at < stale_before,
            ).values(
                status="failed",
                stage="任务中断",
                message="任务心跳超时，服务进程可能已退出",
                error=f"预计算任务超过 {stale_after_minutes} 分钟没有心跳，已自动释放",
                heartbeat_at=now,
                finished_at=now,
            ))
            claimed = conn.execute(precompute_jobs.update().where(
                precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
                ~precompute_jobs.c.status.in_(ACTIVE_PRECOMPUTE_STATUSES),
            ).values(**values))
            if claimed.rowcount:
                started = True
            else:
                conn.execute(precompute_jobs.insert().values(
                    task_key=PRECOMPUTE_TASK_KEY, **values))
                started = True
    except IntegrityError:
        # 另一请求在本请求插入前已完成认领；读取并复用它的任务。
        started = False
    job = get_precompute_job()
    if not job:
        raise RuntimeError("预计算任务认领后无法读取状态")
    return started, job


def reap_stale_precompute_jobs(stale_after_minutes: int = 10) -> int:
    """独立回收心跳超时的活跃预计算任务（进程异常退出/被杀留下的僵尸任务）。

    与 `claim_precompute_job` 内联回收同口径，但不认领新任务，供启动自检和周期巡检调用，
    使僵尸任务无需等到下一次预计算调用即可自愈。`stale_after_minutes<=0` 表示回收全部活跃
    任务（仅用于单实例启动自检：新进程内不可能存在在跑的预计算线程）。返回回收条数。
    """
    eng = get_engine()
    now = datetime.now()
    conditions = [
        precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
        precompute_jobs.c.status.in_(ACTIVE_PRECOMPUTE_STATUSES),
    ]
    if stale_after_minutes > 0:
        conditions.append(
            precompute_jobs.c.heartbeat_at < now - timedelta(minutes=stale_after_minutes))
        reason = f"预计算任务超过 {stale_after_minutes} 分钟没有心跳，已自动释放"
    else:
        reason = "服务重启时发现无归属的活跃预计算任务，已自动释放"
    with eng.begin() as conn:
        result = conn.execute(precompute_jobs.update().where(*conditions).values(
            status="failed",
            stage="任务中断",
            message="任务心跳超时或进程已退出，自愈机制已自动释放",
            error=reason,
            heartbeat_at=now,
            finished_at=now,
        ))
    return int(result.rowcount or 0)


def update_precompute_job(job_id: str, **changes: Any) -> bool:
    """仅允许任务持有者更新状态，防止旧 worker 覆盖后续任务。"""
    allowed = {
        "status", "progress", "stage", "message", "current_date",
        "completed_count", "total_count", "result", "error", "finished_at",
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    values["heartbeat_at"] = datetime.now()
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(precompute_jobs.update().where(
            precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
            precompute_jobs.c.job_id == job_id,
        ).values(**values))
    return bool(result.rowcount)


def precompute_job_owned(job_id: str) -> bool:
    """确认当前全局租约仍属于 job_id，供旧 worker 在写结果前执行栅栏检查。"""
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(select(func.count()).select_from(precompute_jobs).where(
            precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
            precompute_jobs.c.job_id == job_id,
            precompute_jobs.c.status.in_(ACTIVE_PRECOMPUTE_STATUSES),
        )).scalar_one() > 0


def get_daily_factor_run(trade_date: str) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(daily_factor_runs).where(
            daily_factor_runs.c.trade_date == trade_date)).mappings().first()
    return _normalize_daily_factor_run(dict(row)) if row else None


def has_usable_daily_factors(trade_date: str, factor_version: str,
                              min_coverage: float = 0.8,
                              schema_hash: Optional[str] = None,
                              dependency_hash: Optional[str] = None) -> bool:
    """校验成功运行、覆盖率、依赖指纹，并要求全部因子行与质量记录属于同一 run。"""
    run = get_daily_factor_run(trade_date)
    if not run or run.get("status") != "success" or not run.get("run_id"):
        return False
    if run.get("factor_version") != factor_version:
        return False
    if schema_hash is not None and run.get("schema_hash") != schema_hash:
        return False
    if dependency_hash is not None and run.get("dependency_hash") != dependency_hash:
        return False
    if run.get("dependencies") is not None:
        if _json_fingerprint(run["dependencies"]) != run.get("dependency_hash"):
            return False
    if float(run.get("coverage_ratio") or 0) < min_coverage:
        return False

    eng = get_engine()
    matches = [
        daily_factors.c.factor_version == factor_version,
        daily_factors.c.run_id == run["run_id"],
    ]
    if schema_hash is not None:
        matches.append(daily_factors.c.schema_hash == schema_hash)
    if dependency_hash is not None:
        matches.append(daily_factors.c.dependency_hash == dependency_hash)
    with eng.connect() as conn:
        total = conn.execute(select(func.count()).select_from(daily_factors).where(
            daily_factors.c.trade_date == trade_date)).scalar_one()
        matched = conn.execute(select(func.count()).select_from(daily_factors).where(
            daily_factors.c.trade_date == trade_date, *matches)).scalar_one()
    return total > 0 and matched == total


def daily_factor_run_status(limit: int = 30) -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(daily_factor_runs)
                            .order_by(daily_factor_runs.c.trade_date.desc())
                            .limit(limit)).mappings().all()
    return [_normalize_daily_factor_run(dict(row)) for row in rows]


def fetch_daily_factors(trade_date: str) -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(
            daily_factors.c.code,
            daily_factors.c.factors,
            daily_factors.c.factor_version,
            daily_factors.c.schema_hash,
            daily_factors.c.dependency_hash,
            daily_factors.c.dependencies,
            daily_factors.c.run_id,
        ).where(daily_factors.c.trade_date == trade_date)).mappings().all()
    return [dict(row) for row in rows]


def fetch_daily_factor(trade_date: str, code: str) -> Optional[dict[str, Any]]:
    """兼容旧调用：仅返回单只股票某交易日的 factors。"""
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(daily_factors.c.factors).where(
            daily_factors.c.trade_date == trade_date,
            daily_factors.c.code == code,
        )).first()
    return dict(row[0]) if row and row[0] else None


def fetch_latest_usable_factor(code: str, as_of: Any, factor_version: str,
                               schema_hash: str,
                               min_coverage: float = 0.8,
                               dependency_hash: Optional[str] = None) -> Optional[dict[str, Any]]:
    """读取截至 as_of 最新且运行成功、覆盖达标、契约与 run_id 完全一致的因子。"""
    if hasattr(as_of, "strftime"):
        as_of_date = as_of.strftime("%Y%m%d")
    else:
        as_of_date = str(as_of).replace("-", "")[:8]
    joined = daily_factors.join(
        daily_factor_runs,
        (daily_factor_runs.c.trade_date == daily_factors.c.trade_date)
        & (daily_factor_runs.c.run_id == daily_factors.c.run_id),
    )
    stmt = select(
        daily_factors.c.trade_date,
        daily_factors.c.factors,
        daily_factors.c.factor_version,
        daily_factors.c.schema_hash,
        daily_factors.c.dependency_hash,
        daily_factors.c.dependencies,
        daily_factors.c.run_id,
        daily_factor_runs.c.dependencies.label("run_dependencies"),
        daily_factor_runs.c.factor_components,
        daily_factor_runs.c.coverage_ratio,
    ).select_from(joined).where(
        daily_factors.c.code == code,
        daily_factors.c.trade_date <= as_of_date,
        daily_factors.c.factor_version == factor_version,
        daily_factors.c.schema_hash == schema_hash,
        daily_factor_runs.c.status == "success",
        daily_factor_runs.c.coverage_ratio >= min_coverage,
        daily_factor_runs.c.factor_version == factor_version,
        daily_factor_runs.c.schema_hash == schema_hash,
    )
    if dependency_hash is not None:
        stmt = stmt.where(
            daily_factors.c.dependency_hash == dependency_hash,
            daily_factor_runs.c.dependency_hash == dependency_hash,
        )
    stmt = stmt.order_by(daily_factors.c.trade_date.desc()).limit(1)
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    if not row:
        return None
    result = dict(row)
    row_dependencies = result.get("dependencies")
    run_dependencies = result.pop("run_dependencies", None)
    if row_dependencies != run_dependencies:
        return None
    if row_dependencies is not None and _json_fingerprint(row_dependencies) != result.get("dependency_hash"):
        return None
    result["coverage_ratio"] = float(result["coverage_ratio"])
    result["contract"] = get_factor_contract(schema_hash)
    return result


def has_daily_factors(trade_date: str) -> bool:
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(select(func.count()).select_from(daily_factors)
                            .where(daily_factors.c.trade_date == trade_date)).scalar() > 0


def latest_factor_date() -> Optional[str]:
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(select(func.max(daily_factors.c.trade_date))).scalar()


def factor_date_counts(limit: int = 30) -> list[dict[str, Any]]:
    """返回最近若干交易日的原始因子行数，仅供 legacy/诊断展示。"""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(daily_factors.c.trade_date, func.count().label("cnt"))
            .group_by(daily_factors.c.trade_date)
            .order_by(daily_factors.c.trade_date.desc())
            .limit(limit)
        ).all()
    return [{"trade_date": r[0], "count": int(r[1])} for r in rows]


def usable_factor_date_counts(factor_version: str, schema_hash: str,
                              dependency_hash: str, limit: int = 30,
                              min_coverage: float = 0.8) -> list[dict[str, Any]]:
    """只统计当前契约成功运行且因子行与质量记录 run_id 一致的覆盖数。"""
    joined = daily_factors.join(
        daily_factor_runs,
        (daily_factor_runs.c.trade_date == daily_factors.c.trade_date)
        & (daily_factor_runs.c.run_id == daily_factors.c.run_id),
    )
    stmt = select(
        daily_factors.c.trade_date, func.count().label("cnt")
    ).select_from(joined).where(
        daily_factor_runs.c.status == "success",
        daily_factor_runs.c.coverage_ratio >= min_coverage,
        daily_factor_runs.c.factor_version == factor_version,
        daily_factor_runs.c.schema_hash == schema_hash,
        daily_factor_runs.c.dependency_hash == dependency_hash,
        daily_factors.c.factor_version == factor_version,
        daily_factors.c.schema_hash == schema_hash,
        daily_factors.c.dependency_hash == dependency_hash,
    ).group_by(daily_factors.c.trade_date).order_by(
        daily_factors.c.trade_date.desc()).limit(limit)
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(stmt).all()
    return [{"trade_date": row[0], "count": int(row[1])} for row in rows]


def latest_usable_factor_date(factor_version: str, schema_hash: str,
                              dependency_hash: str,
                              min_coverage: float = 0.8) -> Optional[str]:
    """返回当前完整契约下最近可用的成功预计算日期。"""
    rows = usable_factor_date_counts(
        factor_version, schema_hash, dependency_hash, limit=1,
        min_coverage=min_coverage)
    return rows[0]["trade_date"] if rows else None


# ---------- daily_sector_scores（行业评分） ----------
def replace_daily_sector_scores(trade_date: str, items: list[dict[str, Any]],
                                factor_version: Optional[str] = None,
                                schema_hash: Optional[str] = None,
                                run_id: Optional[str] = None,
                                dependency_hash: Optional[str] = None,
                                dependencies: Optional[dict[str, Any]] = None) -> int:
    """以事务整日替换行业评分，并把契约元数据写入每一行。"""
    eng = get_engine()
    now = datetime.now()
    with eng.begin() as conn:
        conn.execute(daily_sector_scores.delete().where(
            daily_sector_scores.c.trade_date == trade_date))
        if items:
            conn.execute(daily_sector_scores.insert(), [{
                "trade_date": trade_date,
                "code": item["code"],
                "name": item.get("name", ""),
                "score": item.get("score", 0),
                "percentile": item.get("percentile", 0),
                "factors": item.get("factors", {}),
                "factor_version": factor_version,
                "schema_hash": schema_hash,
                "dependency_hash": dependency_hash,
                "dependencies": dependencies,
                "run_id": run_id,
                "computed_at": now,
            } for item in items])
    return len(items)


def publish_daily_factor_bundle(job_id: str, trade_date: str,
                                factor_items: list[dict[str, Any]],
                                sector_items: list[dict[str, Any]],
                                run_record: dict[str, Any],
                                factor_version: str, schema_hash: str,
                                sector_version: str, sector_schema_hash: str,
                                dependency_hash: str,
                                dependencies: dict[str, Any]) -> Optional[str]:
    """原子发布整日结果；返回 published/preserved_success，租约失效返回 None。"""
    if _json_fingerprint(dependencies) != dependency_hash:
        raise ValueError("dependency_hash 与 dependencies 完整摘要不一致")
    eng = get_engine()
    now = datetime.now()
    with eng.begin() as conn:
        owned = conn.execute(select(func.count()).select_from(precompute_jobs).where(
            precompute_jobs.c.task_key == PRECOMPUTE_TASK_KEY,
            precompute_jobs.c.job_id == job_id,
            precompute_jobs.c.status.in_(ACTIVE_PRECOMPUTE_STATUSES),
        )).scalar_one()
        if not owned:
            return None
        existing = conn.execute(select(daily_factor_runs).where(
            daily_factor_runs.c.trade_date == trade_date)).mappings().first()
        if (run_record.get("status") != "success" and existing
                and existing.get("status") == "success"
                and existing.get("factor_version") == factor_version
                and existing.get("schema_hash") == schema_hash
                and existing.get("dependency_hash") == dependency_hash):
            return "preserved_success"
        conn.execute(daily_factors.delete().where(daily_factors.c.trade_date == trade_date))
        conn.execute(daily_sector_scores.delete().where(
            daily_sector_scores.c.trade_date == trade_date))
        if factor_items:
            conn.execute(daily_factors.insert(), [{
                "trade_date": trade_date, "code": item["code"], "factors": item["factors"],
                "factor_version": factor_version, "schema_hash": schema_hash,
                "dependency_hash": dependency_hash, "dependencies": dependencies,
                "run_id": job_id, "computed_at": now,
            } for item in factor_items])
        if sector_items:
            conn.execute(daily_sector_scores.insert(), [{
                "trade_date": trade_date, "code": item["code"], "name": item.get("name", ""),
                "score": item.get("score", 0), "percentile": item.get("percentile", 0),
                "factors": item.get("factors", {}), "factor_version": sector_version,
                "schema_hash": sector_schema_hash, "dependency_hash": dependency_hash,
                "dependencies": dependencies, "run_id": job_id, "computed_at": now,
            } for item in sector_items])
        values = {key: run_record.get(key) for key in (
            "factor_version", "schema_hash", "dependency_hash", "dependencies",
            "factor_components", "run_id", "lookback",
            "universe_count", "computed_count", "coverage_ratio", "status", "errors",
            "started_at", "finished_at")}
        updated = conn.execute(daily_factor_runs.update().where(
            daily_factor_runs.c.trade_date == trade_date).values(**values))
        if not updated.rowcount:
            conn.execute(daily_factor_runs.insert().values(trade_date=trade_date, **values))
    return "published"


def fetch_daily_sector_scores(trade_date: str) -> list[dict[str, Any]]:
    """读取某交易日行业评分，按综合分从高到低；调用方需自行判断运行质量。"""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(daily_sector_scores).where(
            daily_sector_scores.c.trade_date == trade_date
        ).order_by(daily_sector_scores.c.score.desc())).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["score"] = float(item.get("score") or 0)
        item["percentile"] = float(item.get("percentile") or 0)
        if item.get("computed_at"):
            item["computed_at"] = item["computed_at"].strftime("%Y-%m-%d %H:%M:%S")
        out.append(item)
    return out


_SECTOR_REQUIRED_FACTORS = frozenset({
    "sec_mom_12_1", "sec_mom_20d", "sec_mom_5d", "sec_vol_confirm", "sec_low_vol",
})
_MIN_COMPLETE_SECTOR_COUNT = 28


def _sector_dependencies_match(dependencies: Any, factor_version: str,
                               schema_hash: str, dependency_hash: str) -> bool:
    """独立校验行业公式与完整上游依赖，不借用个股计算覆盖率。"""
    if not isinstance(dependencies, dict) or _json_fingerprint(dependencies) != dependency_hash:
        return False
    sector_contract = dependencies.get("sector_scoring")
    return bool(isinstance(sector_contract, dict)
                and sector_contract.get("factor_version") == factor_version
                and sector_contract.get("schema_hash") == schema_hash)


def _sector_date_group_is_usable(rows: list[dict[str, Any]], factor_version: str,
                                 schema_hash: str, dependency_hash: str) -> bool:
    """按行业自身批次、数量和字段判断整日快照是否完整。"""
    if len(rows) < _MIN_COMPLETE_SECTOR_COUNT:
        return False
    codes = {str(row.get("code") or "").strip() for row in rows}
    run_ids = {str(row.get("run_id") or "").strip() for row in rows}
    if len(codes) != len(rows) or "" in codes or len(run_ids) != 1 or "" in run_ids:
        return False
    for row in rows:
        if (row.get("factor_version") != factor_version
                or row.get("schema_hash") != schema_hash
                or row.get("dependency_hash") != dependency_hash
                or not _sector_dependencies_match(
                    row.get("dependencies"), factor_version, schema_hash, dependency_hash)):
            return False
        factors_value = row.get("factors")
        if not isinstance(factors_value, dict) or not _SECTOR_REQUIRED_FACTORS.issubset(factors_value):
            return False
        numeric_values = [row.get("score"), row.get("percentile"),
                          *(factors_value.get(name) for name in _SECTOR_REQUIRED_FACTORS)]
        try:
            if any(not math.isfinite(float(value)) for value in numeric_values):
                return False
            if not 0 <= float(row.get("percentile")) <= 1:
                return False
        except (TypeError, ValueError):
            return False
    return True


def fetch_usable_sector_score_history(date_from: str, date_to: str,
                                        factor_version: str, schema_hash: str,
                                        dependency_hash: str,
                                        codes: Optional[list[str]] = None,
                                        min_coverage: float = 0.8) -> list[dict[str, Any]]:
    """读取当前契约下的完整行业历史；行业质量不再与个股覆盖率耦合。"""
    del min_coverage  # 保留兼容参数；行业采用自身行业数与字段完整性门禁。
    stmt = select(daily_sector_scores).where(
        daily_sector_scores.c.trade_date >= str(date_from),
        daily_sector_scores.c.trade_date <= str(date_to),
        daily_sector_scores.c.factor_version == factor_version,
        daily_sector_scores.c.schema_hash == schema_hash,
        daily_sector_scores.c.dependency_hash == dependency_hash,
    ).order_by(daily_sector_scores.c.trade_date.desc(), daily_sector_scores.c.score.desc())
    with get_engine().connect() as conn:
        raw_rows = [dict(row) for row in conn.execute(stmt).mappings().all()]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        grouped.setdefault(str(row.get("trade_date") or ""), []).append(row)
    usable_dates = {
        trade_date for trade_date, date_rows in grouped.items()
        if _sector_date_group_is_usable(
            date_rows, factor_version, schema_hash, dependency_hash)
    }
    normalized_codes = None
    if codes is not None:
        normalized_codes = {str(code).strip() for code in codes if str(code).strip()}
        if not normalized_codes:
            return []

    output: list[dict[str, Any]] = []
    for item in raw_rows:
        if str(item.get("trade_date") or "") not in usable_dates:
            continue
        if normalized_codes is not None and str(item.get("code") or "") not in normalized_codes:
            continue
        item["score"] = float(item.get("score") or 0)
        item["percentile"] = float(item.get("percentile") or 0)
        if item.get("computed_at"):
            item["computed_at"] = item["computed_at"].strftime("%Y-%m-%d %H:%M:%S")
        output.append(item)
    return output


def usable_sector_score_dates(as_of: str, factor_version: str, schema_hash: str,
                               dependency_hash: str, limit: int = 60,
                               min_coverage: float = 0.8) -> list[str]:
    """返回截至 as_of 的当前契约合格行业评分日期，按交易日降序。"""
    rows = fetch_usable_sector_score_history(
        "00000000", str(as_of), factor_version, schema_hash, dependency_hash,
        min_coverage=min_coverage)
    dates = sorted({str(row["trade_date"]) for row in rows}, reverse=True)
    return dates[:max(0, int(limit))]


def latest_usable_sector_score_date(as_of: str, factor_version: str,
                                     schema_hash: str, dependency_hash: str,
                                     min_coverage: float = 0.8) -> Optional[str]:
    """返回不晚于 as_of 的当前契约最近合格行业评分日期。"""
    dates = usable_sector_score_dates(
        as_of, factor_version, schema_hash, dependency_hash, limit=1,
        min_coverage=min_coverage)
    return dates[0] if dates else None


def fetch_usable_daily_sector_scores(trade_date: str, factor_version: str,
                                     schema_hash: str, dependency_hash: str,
                                     min_coverage: float = 0.8) -> list[dict[str, Any]]:
    """兼容旧调用：精确读取某日当前契约下的合格行业评分。"""
    return fetch_usable_sector_score_history(
        trade_date, trade_date, factor_version, schema_hash, dependency_hash,
        min_coverage=min_coverage)


def latest_sector_score_date() -> Optional[str]:
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(select(func.max(daily_sector_scores.c.trade_date))).scalar()


# ---------- 动态访客 Key ----------
def list_user_api_keys() -> list[dict[str, Any]]:
    """列出可管理元数据；只返回掩码，不返回摘要或明文。"""
    with get_engine().connect() as conn:
        rows = conn.execute(select(
            user_api_keys.c.id, user_api_keys.c.label, user_api_keys.c.key_prefix,
            user_api_keys.c.created_at, user_api_keys.c.disabled,
        ).order_by(user_api_keys.c.created_at.desc())).mappings().all()
    return [{
        "id": str(row["id"]), "label": str(row["label"] or "访客"),
        "masked_key": f"{str(row['key_prefix'])}…",
        "created_at": str(row["created_at"] or ""),
        "disabled": bool(row["disabled"]),
    } for row in rows]


def create_user_api_key(key_id: str, label: str, raw_key: str) -> dict[str, Any]:
    """原子创建访客 Key；调用方负责仅在本次响应返回 raw_key。"""
    values = {
        "id": str(key_id)[:24], "label": str(label or "访客")[:64],
        "key_hash": _user_api_key_hash(raw_key), "key_prefix": str(raw_key)[:24],
        "created_at": datetime.now(), "disabled": 0,
    }
    with get_engine().begin() as conn:
        conn.execute(user_api_keys.insert().values(**values))
    return {
        "id": values["id"], "label": values["label"],
        "masked_key": f"{values['key_prefix']}…",
        "created_at": str(values["created_at"]), "disabled": False,
    }


def verify_user_api_key(raw_key: str) -> bool:
    """按摘要验证动态访客 Key，停用记录立即失效。"""
    if not raw_key:
        return False
    with get_engine().connect() as conn:
        row = conn.execute(select(user_api_keys.c.id).where(
            user_api_keys.c.key_hash == _user_api_key_hash(raw_key),
            user_api_keys.c.disabled == 0,
        )).first()
    return row is not None


def toggle_user_api_key(key_id: str) -> Optional[bool]:
    """单条 SQL 原子切换启停状态，避免整数组读改写导致并发丢更新。"""
    with get_engine().begin() as conn:
        result = conn.execute(user_api_keys.update().where(
            user_api_keys.c.id == key_id).values(
                disabled=case((user_api_keys.c.disabled == 0, 1), else_=0)))
        if result.rowcount == 0:
            return None
        disabled = conn.execute(select(user_api_keys.c.disabled).where(
            user_api_keys.c.id == key_id)).scalar_one()
    return bool(disabled)


def delete_user_api_key(key_id: str) -> bool:
    """按主键原子删除动态访客 Key。"""
    with get_engine().begin() as conn:
        result = conn.execute(user_api_keys.delete().where(user_api_keys.c.id == key_id))
    return result.rowcount > 0


# ---------- config_kv ----------
def get_config(key: str) -> Optional[Any]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(config_kv.c.v).where(config_kv.c.k == key)).first()
    return row[0] if row else None


def set_config(key: str, value: Any) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        exists = conn.execute(select(config_kv.c.k).where(config_kv.c.k == key)).first()
        if exists:
            conn.execute(config_kv.update().where(config_kv.c.k == key)
                         .values(v=value, updated_at=datetime.now()))
        else:
            conn.execute(config_kv.insert().values(k=key, v=value, updated_at=datetime.now()))


# ---------- config_versions（配置变更留痕 / 类 commit 版本） ----------
def _gen_version_id(config_key: str, payload: Any, parent: Optional[str]) -> str:
    """生成类 commit 的短哈希版本号（含微秒时间戳，避免碰撞）。"""
    base = "|".join([
        config_key,
        _json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
        parent or "",
        datetime.now().isoformat(),
    ])
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]


def latest_config_version(config_key: str) -> Optional[str]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(
            select(config_versions.c.version_id)
            .where(config_versions.c.config_key == config_key)
            .order_by(config_versions.c.id.desc()).limit(1)).first()
    return row[0] if row else None


def record_config_version(config_key: str, payload: Any, actor: str,
                          reason: str = "") -> dict[str, Any]:
    """把一次配置变更留痕为新版本，返回 {version_id, parent_version, created_at}。"""
    eng = get_engine()
    parent = latest_config_version(config_key)
    now = datetime.now()
    vid = _gen_version_id(config_key, payload, parent)
    with eng.begin() as conn:
        conn.execute(config_versions.insert().values(
            version_id=vid, config_key=config_key, actor=(actor or "unknown"),
            reason=(reason or ""), payload=payload, parent_version=parent, created_at=now))
    return {"version_id": vid, "parent_version": parent,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S")}


def publish_factor_weights(model: str, weights: dict[str, Any], actor: str,
                           reason: str, expected_parent_version: Optional[str],
                           entry_metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """以行锁和父版本比较原子发布模型权重，同时写配置历史和完整配置。"""
    eng = get_engine()
    config_key = "factor_weights"
    history_key = f"factor_weights:{model}"
    now = datetime.now()
    with eng.begin() as conn:
        row = conn.execute(select(config_kv.c.v).where(
            config_kv.c.k == config_key).with_for_update()).first()
        full_config = dict(row[0] or {}) if row else {}
        active_entry = full_config.get(model) or {}
        active_version = active_entry.get("version_id")
        expected_version = (None if expected_parent_version in (None, "", "default")
                            else expected_parent_version)
        if expected_version != active_version and expected_parent_version is not None:
            return {
                "applied": False,
                "conflict": True,
                "model": model,
                "expected_parent_version": expected_parent_version,
                "active_version": active_version,
            }

        version_id = _gen_version_id(history_key, weights, active_version)
        conn.execute(config_versions.insert().values(
            version_id=version_id,
            config_key=history_key,
            actor=actor or "unknown",
            reason=reason or "",
            payload=weights,
            parent_version=active_version,
            created_at=now,
        ))
        entry = dict(entry_metadata or {})
        entry.update({
            "weights": weights,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version_id": version_id,
            "actor": actor or "unknown",
            "reason": reason or "",
            "parent_version": active_version,
        })
        full_config[model] = entry
        if row:
            conn.execute(config_kv.update().where(config_kv.c.k == config_key).values(
                v=full_config, updated_at=now))
        else:
            conn.execute(config_kv.insert().values(
                k=config_key, v=full_config, updated_at=now))
    return {
        "applied": True,
        "conflict": False,
        "model": model,
        "weights": weights,
        "version_id": version_id,
        "parent_version": active_version,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "entry": entry,
    }


def list_config_versions(config_key: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    eng = get_engine()
    stmt = select(config_versions)
    if config_key:
        stmt = stmt.where(config_versions.c.config_key == config_key)
    stmt = stmt.order_by(config_versions.c.id.desc()).limit(limit)
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S") if d.get("created_at") else None
        out.append(d)
    return out


def get_config_version(version_id: str) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(config_versions)
                           .where(config_versions.c.version_id == version_id)).mappings().first()
    if not row:
        return None
    d = dict(row)
    d["created_at"] = d["created_at"].strftime("%Y-%m-%d %H:%M:%S") if d.get("created_at") else None
    return d


# ---------- daily_sentiment ----------
def upsert_daily_sentiment(trade_date: str, indicators: dict[str, Any],
                           factor_version: Optional[str] = None,
                           schema_hash: Optional[str] = None) -> None:
    """写入完整收盘情绪原始指标；盘中快照禁止进入持久层。"""
    metadata_value = indicators.get("_meta") if isinstance(indicators, dict) else None
    if not isinstance(metadata_value, dict) or metadata_value.get("is_final") is not True:
        raise ValueError("daily_sentiment 只允许写入 is_final=true 的完整收盘数据")
    eng = get_engine()
    values = {"indicators": indicators, "factor_version": factor_version,
              "schema_hash": schema_hash, "computed_at": datetime.now()}
    with eng.begin() as conn:
        exists = conn.execute(select(daily_sentiment.c.trade_date)
                              .where(daily_sentiment.c.trade_date == trade_date)).first()
        if exists:
            conn.execute(daily_sentiment.update().where(
                daily_sentiment.c.trade_date == trade_date).values(**values))
        else:
            conn.execute(daily_sentiment.insert().values(trade_date=trade_date, **values))


def fetch_daily_sentiment(dates: list[str], factor_version: Optional[str] = None,
                           schema_hash: Optional[str] = None) -> dict[str, Any]:
    eng = get_engine()
    stmt = select(daily_sentiment.c.trade_date, daily_sentiment.c.indicators).where(
        daily_sentiment.c.trade_date.in_(dates))
    if factor_version is not None:
        stmt = stmt.where(daily_sentiment.c.factor_version == factor_version)
    if schema_hash is not None:
        stmt = stmt.where(daily_sentiment.c.schema_hash == schema_hash)
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return {row["trade_date"]: row["indicators"] for row in rows}


# ---------- 量化盯盘 ----------
def _quant_watch_claim_identity(owner_id: Optional[str],
                                fence_token: Optional[int]) -> tuple[str, int]:
    """解析显式 fencing 身份；仅为旧扫描调用保留当前执行上下文兼容。"""
    if owner_id is not None and fence_token is not None:
        return str(owner_id), int(fence_token)
    current = _quant_watch_claim_context.get()
    if current is None:
        return "", -1
    return current


def current_quant_watch_claim() -> Optional[tuple[str, int]]:
    """返回当前执行上下文的租约身份，仅供服务内部通知桥接使用。"""
    return _quant_watch_claim_context.get()


def claim_quant_watch_scan(owner_id: str, lease_seconds: int,
                           interval_seconds: int, manual: bool = False) -> dict[str, Any]:
    """原子认领扫描并返回 fencing token；自动和手动扫描均受 next_scan_at 控频。"""
    owner = str(owner_id or "").strip()
    if not owner:
        return {"claimed": False, "reason": "invalid_owner", "message": "扫描实例标识不能为空"}
    try:
        lease = max(1, int(lease_seconds))
        interval = max(1, int(interval_seconds))
    except (TypeError, ValueError):
        return {"claimed": False, "reason": "invalid_interval", "message": "租约和扫描间隔必须为整数"}
    eng = get_engine()
    now = datetime.now()
    lease_until = now + timedelta(seconds=lease)
    next_scan_at = now + timedelta(seconds=interval)
    lease_available = (
        quant_watch_state.c.owner_id.is_(None)
        | quant_watch_state.c.lease_until.is_(None)
        | (quant_watch_state.c.lease_until <= now)
    )
    conditions = [
        quant_watch_state.c.task_key == "quant_watch",
        lease_available,
        quant_watch_state.c.next_scan_at.is_(None)
        | (quant_watch_state.c.next_scan_at <= now),
    ]
    with eng.begin() as conn:
        updated = conn.execute(
            quant_watch_state.update().where(*conditions).values(
                owner_id=owner, lease_until=lease_until,
                fence_token=quant_watch_state.c.fence_token + 1,
                next_scan_at=next_scan_at, status="running", heartbeat_at=now))
        if updated.rowcount:
            row = conn.execute(select(
                quant_watch_state.c.fence_token,
                quant_watch_state.c.lease_until,
                quant_watch_state.c.next_scan_at,
            ).where(quant_watch_state.c.task_key == "quant_watch")).mappings().one()
            token = int(row["fence_token"])
            result = {
                "claimed": True, "reason": "claimed", "fence_token": token,
                "lease_until": row["lease_until"], "next_scan_at": row["next_scan_at"],
                "manual": bool(manual),
            }
        else:
            row = conn.execute(select(quant_watch_state).where(
                quant_watch_state.c.task_key == "quant_watch")).mappings().first()
            result = None
    if result is not None:
        _quant_watch_claim_context.set((owner, token))
        return result
    if row is None:
        try:
            with eng.begin() as conn:
                conn.execute(quant_watch_state.insert().values(
                    task_key="quant_watch", owner_id=owner, lease_until=lease_until,
                    fence_token=1, next_scan_at=next_scan_at, status="running",
                    heartbeat_at=now))
            _quant_watch_claim_context.set((owner, 1))
            return {
                "claimed": True, "reason": "claimed", "fence_token": 1,
                "lease_until": lease_until, "next_scan_at": next_scan_at,
                "manual": bool(manual),
            }
        except IntegrityError:
            with eng.connect() as conn:
                row = conn.execute(select(quant_watch_state).where(
                    quant_watch_state.c.task_key == "quant_watch")).mappings().first()
    if row and row.get("owner_id") is not None and row.get("lease_until") and row["lease_until"] > now:
        message = ("当前实例已有扫描正在执行，租约尚未到期"
                   if row.get("owner_id") == owner else "其他实例正在扫描，租约尚未到期")
        return {"claimed": False, "reason": "lease_active", "message": message}
    if row and row.get("next_scan_at") and row["next_scan_at"] > now:
        return {
            "claimed": False, "reason": "not_due", "message": "尚未到达下一次允许扫描时间",
            "next_scan_at": row["next_scan_at"],
        }
    return {"claimed": False, "reason": "claim_conflict", "message": "扫描认领发生并发冲突，请稍后重试"}


def claim_quant_watch_lease(owner_id: str, lease_seconds: int) -> bool:
    """兼容旧扫描线程；内部统一走原子认领并保存本次 fencing 上下文。"""
    result = claim_quant_watch_scan(
        owner_id, lease_seconds, max(1, int(lease_seconds) // 4), manual=False)
    return bool(result.get("claimed"))


def renew_quant_watch_lease(owner_id: str, fence_token: int,
                            lease_seconds: int) -> bool:
    """仅当前未过期的 owner_id 与 fence_token 可续租。"""
    now = datetime.now()
    lease_until = now + timedelta(seconds=max(1, int(lease_seconds)))
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_state.update().where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == str(owner_id),
            quant_watch_state.c.fence_token == int(fence_token),
            quant_watch_state.c.lease_until > now,
        ).values(lease_until=lease_until, heartbeat_at=now))
    return bool(result.rowcount)


def quant_watch_scan_owned(owner_id: str, fence_token: int) -> bool:
    """校验扫描所有权、fencing token 与租约有效期。"""
    now = datetime.now()
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(quant_watch_state.c.task_key).where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == str(owner_id),
            quant_watch_state.c.fence_token == int(fence_token),
            quant_watch_state.c.lease_until > now,
        )).first()
    return row is not None


def update_quant_watch_state(owner_id: str, fence_token: Optional[int] = None,
                             **changes: Any) -> bool:
    """仅当前未过期租约持有者可更新状态，旧调用从认领上下文取得 token。"""
    owner, token = _quant_watch_claim_identity(owner_id, fence_token)
    allowed = {
        "status", "trade_date", "phase", "last_scan_at", "last_error",
        "last_message_id", "lease_until",
    }
    values = {key: value for key, value in changes.items() if key in allowed}
    values["heartbeat_at"] = datetime.now()
    now = datetime.now()
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_state.update().where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == owner,
            quant_watch_state.c.fence_token == token,
            quant_watch_state.c.lease_until > now,
        ).values(**values))
    return bool(result.rowcount)


def release_quant_watch_lease(owner_id: str, fence_token: Optional[int] = None) -> bool:
    """只释放匹配所有者的租约，不改写最近运行状态或错误。"""
    owner = str(owner_id)
    conditions = [quant_watch_state.c.task_key == "quant_watch",
                  quant_watch_state.c.owner_id == owner]
    if fence_token is not None:
        conditions.append(quant_watch_state.c.fence_token == int(fence_token))
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_state.update().where(*conditions).values(
            owner_id=None, lease_until=None, heartbeat_at=datetime.now()))
    current = _quant_watch_claim_context.get()
    if result.rowcount and current and current[0] == owner:
        _quant_watch_claim_context.set(None)
    return bool(result.rowcount)


def get_quant_watch_state() -> Optional[dict[str, Any]]:
    """读取可公开运行状态；不返回所有者与 fencing 凭据。"""
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(quant_watch_state).where(
            quant_watch_state.c.task_key == "quant_watch")).mappings().first()
    if not row:
        return None
    result = dict(row)
    for field in ("lease_until", "next_scan_at", "last_scan_at", "heartbeat_at"):
        value = result.get(field)
        result[field] = value.strftime("%Y-%m-%d %H:%M:%S") if value else None
    result.pop("owner_id", None)
    result.pop("fence_token", None)
    return result


def save_quant_watch_message(record: dict[str, Any], owner_id: Optional[str] = None,
                             fence_token: Optional[int] = None) -> bool:
    """同一事务内验证有效租约后保存消息；失去所有权时返回 False。"""
    owner, token = _quant_watch_claim_identity(owner_id, fence_token)
    values = {key: record.get(key) for key in
              ("message_id", "trade_date", "scanned_at", "phase", "status", "payload")}
    # 拒绝 NaN、无穷值和不可序列化对象，绝不静默改写业务载荷。
    _json.dumps(values["payload"], ensure_ascii=False, allow_nan=False)
    values["created_at"] = datetime.now()
    now = datetime.now()
    eng = get_engine()
    with eng.begin() as conn:
        owned = conn.execute(select(quant_watch_state.c.task_key).where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == owner,
            quant_watch_state.c.fence_token == token,
            quant_watch_state.c.lease_until > now,
        ).with_for_update()).first()
        if not owned:
            return False
        exists = conn.execute(select(quant_watch_messages.c.message_id).where(
            quant_watch_messages.c.message_id == values["message_id"])).first()
        if exists:
            return True
        conn.execute(quant_watch_messages.insert().values(**values))
    return True


def cleanup_quant_watch_tickets() -> int:
    """删除已过期或已消费票据，避免凭据表持续增长。"""
    eng = get_engine()
    now = datetime.now()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_tickets.delete().where(
            (quant_watch_tickets.c.expires_at <= now)
            | quant_watch_tickets.c.consumed_at.is_not(None)))
    return int(result.rowcount or 0)


def issue_quant_watch_ticket(role: str, purpose: str,
                             ttl_seconds: int = 60) -> str:
    """签发原始随机票据，数据库仅保存其 SHA-256 摘要。"""
    cleanup_quant_watch_tickets()
    ticket = secrets.token_urlsafe(32)
    now = datetime.now()
    digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(quant_watch_tickets.insert().values(
            ticket_hash=digest, role=str(role), purpose=str(purpose),
            expires_at=now + timedelta(seconds=max(1, int(ttl_seconds))),
            consumed_at=None, created_at=now))
    return ticket


def consume_quant_watch_ticket(ticket: str, role: str = "admin",
                               purpose: str = "quant_watch_ws") -> Optional[str]:
    """原子消费一次性票据，并同时校验角色、用途和有效期。"""
    raw = str(ticket or "")
    if not raw:
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = datetime.now()
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_tickets.update().where(
            quant_watch_tickets.c.ticket_hash == digest,
            quant_watch_tickets.c.role == str(role),
            quant_watch_tickets.c.purpose == str(purpose),
            quant_watch_tickets.c.expires_at > now,
            quant_watch_tickets.c.consumed_at.is_(None),
        ).values(consumed_at=now))
    return str(role) if result.rowcount else None


def claim_quant_watch_notification_event(event_key: str, trade_date: str,
                                         owner_id: str, fence_token: int,
                                         retry_after_seconds: int = 60,
                                         max_attempts: int = 3) -> dict[str, Any]:
    """在有效扫描租约下认领按渠道事件；失败或超时事件可有限重试。"""
    key = str(event_key or "").strip()
    if not key:
        return {"claimed": False, "reason": "invalid_event_key", "message": "通知事件键不能为空"}
    now = datetime.now()
    retry_before = now - timedelta(seconds=max(1, int(retry_after_seconds)))
    attempts = max(1, int(max_attempts))
    eng = get_engine()
    try:
        with eng.begin() as conn:
            owned = conn.execute(select(quant_watch_state.c.task_key).where(
                quant_watch_state.c.task_key == "quant_watch",
                quant_watch_state.c.owner_id == str(owner_id),
                quant_watch_state.c.fence_token == int(fence_token),
                quant_watch_state.c.lease_until > now,
            ).with_for_update()).first()
            if not owned:
                return {"claimed": False, "reason": "lease_lost", "message": "扫描租约已失效"}
            conn.execute(quant_watch_notification_events.insert().values(
                event_key=key, trade_date=str(trade_date), status="claimed",
                owner_id=str(owner_id), fence_token=int(fence_token),
                retry_count=1, result=None, claimed_at=now, finished_at=None))
        return {"claimed": True, "reason": "claimed", "attempt": 1}
    except IntegrityError:
        pass
    with eng.begin() as conn:
        owned = conn.execute(select(quant_watch_state.c.task_key).where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == str(owner_id),
            quant_watch_state.c.fence_token == int(fence_token),
            quant_watch_state.c.lease_until > now,
        ).with_for_update()).first()
        if not owned:
            return {"claimed": False, "reason": "lease_lost", "message": "扫描租约已失效"}
        retryable = (
            ((quant_watch_notification_events.c.status == "failed")
             & (quant_watch_notification_events.c.claimed_at <= retry_before))
            | ((quant_watch_notification_events.c.status == "claimed")
               & (quant_watch_notification_events.c.claimed_at <= retry_before))
        )
        updated = conn.execute(quant_watch_notification_events.update().where(
            quant_watch_notification_events.c.event_key == key,
            quant_watch_notification_events.c.retry_count < attempts,
            retryable,
        ).values(
            status="claimed", owner_id=str(owner_id), fence_token=int(fence_token),
            retry_count=quant_watch_notification_events.c.retry_count + 1,
            result=None, claimed_at=now, finished_at=None,
        ))
        if updated.rowcount:
            row = conn.execute(select(quant_watch_notification_events.c.retry_count).where(
                quant_watch_notification_events.c.event_key == key)).first()
            return {"claimed": True, "reason": "retry", "attempt": int(row[0])}
    return {"claimed": False, "reason": "already_processed", "message": "通知事件已处理或已达重试上限"}


def save_quant_watch_notification_result(event_key: str, owner_id: str,
                                         fence_token: int, status: str,
                                         result: Any) -> bool:
    """仅认领者可保存通知终态结果，并在同一事务验证 fencing 所有权。"""
    final_status = str(status)
    if final_status not in {"success", "partial", "failed"}:
        raise ValueError("通知结果状态必须为 success、partial 或 failed")
    _json.dumps(result, ensure_ascii=False, allow_nan=False)
    now = datetime.now()
    eng = get_engine()
    with eng.begin() as conn:
        owned = conn.execute(select(quant_watch_state.c.task_key).where(
            quant_watch_state.c.task_key == "quant_watch",
            quant_watch_state.c.owner_id == str(owner_id),
            quant_watch_state.c.fence_token == int(fence_token),
            quant_watch_state.c.lease_until > now,
        ).with_for_update()).first()
        if not owned:
            return False
        updated = conn.execute(quant_watch_notification_events.update().where(
            quant_watch_notification_events.c.event_key == str(event_key),
            quant_watch_notification_events.c.status == "claimed",
            quant_watch_notification_events.c.owner_id == str(owner_id),
            quant_watch_notification_events.c.fence_token == int(fence_token),
        ).values(status=final_status, result=result, finished_at=now))
    return bool(updated.rowcount)


def fetch_quant_watch_messages(trade_date: str, limit: int = 60) -> list[dict[str, Any]]:
    """只读取指定交易日的盯盘聚合消息，最新在前。"""
    eng = get_engine()
    stmt = (select(quant_watch_messages)
            .where(quant_watch_messages.c.trade_date == str(trade_date))
            .order_by(quant_watch_messages.c.scanned_at.desc())
            .limit(max(1, min(int(limit), 300))))
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("scanned_at", "created_at"):
            value = item.get(field)
            item[field] = value.strftime("%Y-%m-%d %H:%M:%S") if value else None
        result.append(item)
    return result


def fetch_quant_watch_dates(on_or_before: str, limit: int = 30) -> list[str]:
    """返回不晚于指定日期且实际有聚合消息的日期，按新到旧排列。"""
    eng = get_engine()
    stmt = (select(quant_watch_messages.c.trade_date)
            .where(quant_watch_messages.c.trade_date <= str(on_or_before))
            .group_by(quant_watch_messages.c.trade_date)
            .order_by(quant_watch_messages.c.trade_date.desc())
            .limit(max(1, min(int(limit), 366))))
    with eng.connect() as conn:
        return [str(value) for value in conn.execute(stmt).scalars().all()]


def clear_quant_watch_before(cutoff_date: str) -> int:
    """删除保留截止日之前的聚合消息和通知事件；不影响截止日及之后数据。"""
    eng = get_engine()
    with eng.begin() as conn:
        result = conn.execute(quant_watch_messages.delete().where(
            quant_watch_messages.c.trade_date < str(cutoff_date)))
        conn.execute(quant_watch_notification_events.delete().where(
            quant_watch_notification_events.c.trade_date < str(cutoff_date)))
    return int(result.rowcount or 0)

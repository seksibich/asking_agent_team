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
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from sqlalchemy import (JSON, Column, Date, DateTime, Integer, MetaData, Numeric,
                        SmallInteger, String, Table, Text, UniqueConstraint,
                        create_engine, func, inspect, select, text)
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

_engine: Optional[Engine] = None


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
    """按唯一键写入；auto/manual 已存在时保持不可变，watch/holding 可更新。"""
    eng = get_engine()
    immutable = rec["category"] in {"auto", "manual"}
    payload = {k: rec.get(k) for k in
               ("sel_date", "code", "name", "score", "driver", "reason",
                "category", "extra", "logged_at")}
    with eng.begin() as conn:
        stmt = select(selections).where(
            selections.c.sel_date == rec["sel_date"],
            selections.c.code == rec["code"],
            selections.c.category == rec["category"],
        )
        if eng.dialect.name == "mysql":
            stmt = stmt.with_for_update()
        row = conn.execute(stmt).mappings().first()
        if row and immutable:
            record = dict(row)
            return {"inserted": False, "id": record["id"], "record": record}
        if row:
            conn.execute(selections.update().where(
                selections.c.id == row["id"]).values(**payload))
            record = conn.execute(select(selections).where(
                selections.c.id == row["id"])).mappings().one()
            return {"inserted": False, "id": row["id"], "record": dict(record)}

        result = conn.execute(selections.insert().values(**payload))
        selection_id = int(result.inserted_primary_key[0])
        record = conn.execute(select(selections).where(
            selections.c.id == selection_id)).mappings().one()
        return {"inserted": True, "id": selection_id, "record": dict(record)}


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
def save_factor_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """按 schema_hash 不可变保存因子契约；重复保存直接返回原契约。"""
    eng = get_engine()
    with eng.begin() as conn:
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


def get_factor_contract(schema_hash: str) -> Optional[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        row = conn.execute(select(factor_contracts).where(
            factor_contracts.c.schema_hash == schema_hash)).mappings().first()
    return dict(row) if row else None


def save_screening_run(record: dict[str, Any]) -> dict[str, Any]:
    """按 run_id 不可变保存筛选运行记录，重复保存返回原记录。"""
    eng = get_engine()
    with eng.begin() as conn:
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
                         stale_after_minutes: int = 180) -> tuple[bool, dict[str, Any]]:
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
                error="预计算任务超过 180 分钟没有心跳，已自动释放",
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


def fetch_usable_daily_sector_scores(trade_date: str, factor_version: str,
                                     schema_hash: str, dependency_hash: str,
                                     min_coverage: float = 0.8) -> list[dict[str, Any]]:
    """只读取与成功个股预计算同一 run、同一依赖契约的行业评分。"""
    run = get_daily_factor_run(trade_date)
    if (not run or run.get("status") != "success" or not run.get("run_id")
            or run.get("dependency_hash") != dependency_hash
            or float(run.get("coverage_ratio") or 0) < min_coverage):
        return []
    if run.get("dependencies") is not None:
        if _json_fingerprint(run["dependencies"]) != dependency_hash:
            return []
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(daily_sector_scores).where(
            daily_sector_scores.c.trade_date == trade_date,
            daily_sector_scores.c.run_id == run["run_id"],
            daily_sector_scores.c.factor_version == factor_version,
            daily_sector_scores.c.schema_hash == schema_hash,
            daily_sector_scores.c.dependency_hash == dependency_hash,
        ).order_by(daily_sector_scores.c.score.desc())).mappings().all()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("dependencies") != run.get("dependencies"):
            return []
        item["score"] = float(item.get("score") or 0)
        item["percentile"] = float(item.get("percentile") or 0)
        if item.get("computed_at"):
            item["computed_at"] = item["computed_at"].strftime("%Y-%m-%d %H:%M:%S")
        output.append(item)
    return output


def latest_sector_score_date() -> Optional[str]:
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(select(func.max(daily_sector_scores.c.trade_date))).scalar()


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

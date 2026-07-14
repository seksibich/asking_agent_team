"""持久化数据库层（本地 SQLite / 上云 RDS MySQL 两用）。

- 通过环境变量 DB_URL 切换：
  - 未设置：默认本地 SQLite，文件在 DATA_DIR/stock_agent.db（Docker 卷持久化）
  - 上云：设为 RDS MySQL，如 mysql+pymysql://user:pwd@host:3306/stock_agent?charset=utf8mb4
- 表结构与 service/db/schema.sql（RDS 权威 DDL）保持一致：
  selections / predictions / selection_forward_returns / backtest_snapshots / daily_factors
- SQLAlchemy Core 定义，create_all() 在两种方言下都可建表；RDS 上也可直接执行 schema.sql。

对外提供幂等写入与查询帮助函数，供选股/预判/回测脚本使用。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (JSON, Column, Date, DateTime, Integer, MetaData, Numeric,
                        SmallInteger, String, Table, Text, UniqueConstraint,
                        create_engine, func, select, text)
from sqlalchemy.engine import Engine

import common

metadata = MetaData()

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

predictions = Table(
    "predictions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pred_date", Date, nullable=False, index=True),
    Column("target", String(32), nullable=False, index=True),
    Column("direction", String(8), nullable=False),
    Column("driver", String(32), nullable=False, default="未标注"),
    Column("reason", Text),
    Column("extra", JSON),
    Column("created_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("pred_date", "target", "direction", name="uk_pred"),
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

# 每日情绪原始指标（0-100 情绪温度 / 择时的底层数据，落库持久）
daily_sentiment = Table(
    "daily_sentiment", metadata,
    Column("trade_date", String(8), primary_key=True),   # YYYYMMDD
    Column("indicators", JSON, nullable=False),
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
)

# 全市场因子预计算表（见 service/db/PRECOMPUTE_PLAN.md）
daily_factors = Table(
    "daily_factors", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_date", String(8), nullable=False, index=True),  # YYYYMMDD
    Column("code", String(16), nullable=False, index=True),
    Column("factors", JSON, nullable=False),   # {mom_12_1, reversal_1m, ...}
    Column("computed_at", DateTime, nullable=False, default=datetime.now),
    UniqueConstraint("trade_date", "code", name="uk_df_date_code"),
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


def init_db() -> None:
    """建表（幂等）。启动时调用。"""
    metadata.create_all(get_engine())


# ---------- selections ----------
def upsert_selection(rec: dict[str, Any]) -> None:
    """按 (sel_date, code, category) 幂等写入；已存在则更新。"""
    eng = get_engine()
    with eng.begin() as conn:
        row = conn.execute(
            select(selections.c.id).where(
                selections.c.sel_date == rec["sel_date"],
                selections.c.code == rec["code"],
                selections.c.category == rec["category"],
            )
        ).first()
        payload = {k: rec.get(k) for k in
                   ("sel_date", "code", "name", "score", "driver", "reason", "category", "extra", "logged_at")}
        if row:
            conn.execute(selections.update().where(selections.c.id == row[0]).values(**payload))
        else:
            conn.execute(selections.insert().values(**payload))


def fetch_selections() -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(selections)).mappings().all()
    return [dict(r) for r in rows]


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


# ---------- predictions ----------
def upsert_prediction(rec: dict[str, Any]) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        row = conn.execute(
            select(predictions.c.id).where(
                predictions.c.pred_date == rec["pred_date"],
                predictions.c.target == rec["target"],
                predictions.c.direction == rec["direction"])
        ).first()
        payload = {k: rec.get(k) for k in
                   ("pred_date", "target", "direction", "driver", "reason", "extra")}
        if row:
            conn.execute(predictions.update().where(predictions.c.id == row[0]).values(**payload))
        else:
            conn.execute(predictions.insert().values(**payload))


def fetch_predictions(pred_date: Optional[str] = None) -> list[dict[str, Any]]:
    eng = get_engine()
    stmt = select(predictions)
    if pred_date:
        stmt = stmt.where(predictions.c.pred_date == pred_date)
    with eng.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


# ---------- snapshots ----------
def save_snapshot(kind: str, payload: dict[str, Any]) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(backtest_snapshots.insert().values(
            kind=kind, as_of=datetime.now(), payload=payload))


# ---------- daily_factors (预计算) ----------
def bulk_upsert_daily_factors(trade_date: str, items: list[dict[str, Any]]) -> int:
    """批量写入某交易日的全市场因子（幂等覆盖）。items: [{code, factors}]"""
    eng = get_engine()
    n = 0
    with eng.begin() as conn:
        for it in items:
            row = conn.execute(select(daily_factors.c.id).where(
                daily_factors.c.trade_date == trade_date,
                daily_factors.c.code == it["code"])).first()
            vals = {"factors": it["factors"], "computed_at": datetime.now()}
            if row:
                conn.execute(daily_factors.update()
                             .where(daily_factors.c.id == row[0]).values(**vals))
            else:
                conn.execute(daily_factors.insert().values(
                    trade_date=trade_date, code=it["code"], **vals))
            n += 1
    return n


def fetch_daily_factors(trade_date: str) -> list[dict[str, Any]]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(daily_factors.c.code, daily_factors.c.factors)
                            .where(daily_factors.c.trade_date == trade_date)).mappings().all()
    return [dict(r) for r in rows]


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
    """返回最近若干交易日的因子覆盖股票数：[{trade_date, count}]（降序）。"""
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            select(daily_factors.c.trade_date, func.count().label("cnt"))
            .group_by(daily_factors.c.trade_date)
            .order_by(daily_factors.c.trade_date.desc())
            .limit(limit)
        ).all()
    return [{"trade_date": r[0], "count": int(r[1])} for r in rows]


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


# ---------- daily_sentiment ----------
def upsert_daily_sentiment(trade_date: str, indicators: dict[str, Any]) -> None:
    eng = get_engine()
    with eng.begin() as conn:
        exists = conn.execute(select(daily_sentiment.c.trade_date)
                              .where(daily_sentiment.c.trade_date == trade_date)).first()
        if exists:
            conn.execute(daily_sentiment.update().where(daily_sentiment.c.trade_date == trade_date)
                         .values(indicators=indicators, computed_at=datetime.now()))
        else:
            conn.execute(daily_sentiment.insert().values(
                trade_date=trade_date, indicators=indicators, computed_at=datetime.now()))


def fetch_daily_sentiment(dates: list[str]) -> dict[str, Any]:
    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(select(daily_sentiment.c.trade_date, daily_sentiment.c.indicators)
                            .where(daily_sentiment.c.trade_date.in_(dates))).mappings().all()
    return {r["trade_date"]: r["indicators"] for r in rows}

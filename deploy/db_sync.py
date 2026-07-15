"""检查并同步数据库结构（本地 SQLite / 云端 RDS MySQL 通用）。

在 service/ 目录下用项目 venv 运行：
    cd <APP_DIR>/service && <VENV>/bin/python <APP_DIR>/deploy/db_sync.py

依赖已加载的环境变量（DB_URL 等，来自 .env）。逻辑：
1. 连接数据库，打印当前已有表；
2. 对比 SQLAlchemy 元数据中定义的表，打印将新建的表；
3. 调用 db.init_db()（create_all + 轻量兼容迁移，如补 selection_forward_returns.matured 列）；
4. 打印同步后的表清单。结构不合法或连不上数据库时以非 0 退出，供部署脚本判定失败。
"""
from __future__ import annotations

import sys

from sqlalchemy import inspect

import db  # 需在 service/ 目录下运行，保证可 import


def _mask(url: str) -> str:
    """隐藏连接串中的密码，避免日志泄露。"""
    if "@" in url and "//" in url:
        head, tail = url.split("//", 1)
        if "@" in tail:
            cred, host = tail.split("@", 1)
            user = cred.split(":", 1)[0]
            return f"{head}//{user}:***@{host}"
    return url


def main() -> int:
    try:
        engine = db.get_engine()
        print(f"[db_sync] 目标数据库: {_mask(db.db_url())}")
        with engine.connect() as conn:  # 连通性探测
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"[db_sync] 数据库连接失败: {e}", file=sys.stderr)
        return 1

    try:
        existing = set(inspect(engine).get_table_names())
        defined = set(db.metadata.tables.keys())
        missing = sorted(defined - existing)
        print(f"[db_sync] 现有表({len(existing)}): {sorted(existing) or '无'}")
        print(f"[db_sync] 待创建表: {missing or '无（结构已存在）'}")

        db.init_db()  # 创建缺失表 + 兼容迁移

        after = sorted(inspect(db.get_engine()).get_table_names())
        print(f"[db_sync] 同步后表({len(after)}): {after}")
        still_missing = sorted(set(db.metadata.tables.keys()) - set(after))
        if still_missing:
            print(f"[db_sync] 同步后仍缺失表: {still_missing}", file=sys.stderr)
            return 1
        print("[db_sync] 数据库结构检查与同步完成 ✓")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[db_sync] 结构同步失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

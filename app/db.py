"""SQLite 连接与初始化。

事务策略：连接使用 autocommit（isolation_level=None），所有需要原子性的
写操作都显式执行 ``BEGIN IMMEDIATE`` ... ``COMMIT/ROLLBACK``，避免并发
交分 / 确认时出现写竞争与 “database is locked”。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, current_app, g

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "festival.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    path = db_path or os.environ.get("DB_PATH") or str(DEFAULT_DB_PATH)
    return str(Path(path).expanduser())


def connect(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """打开一个配置好的 SQLite 连接（供 Flask 与脚本/测试共用）。"""
    path = resolve_db_path(db_path)
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # PRAGMA foreign_keys 不能放在事务里；autocommit 模式下直接生效
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = connect(current_app.config["DATABASE"])
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
    # 创建应用时一次性建好表结构
    conn = connect(app.config["DATABASE"])
    try:
        init_schema(conn)
    finally:
        conn.close()

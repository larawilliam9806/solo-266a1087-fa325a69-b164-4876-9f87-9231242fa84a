"""SQLite 存储层。

设计要点：
- WAL 模式 + busy_timeout：多读单写，写者排队而不是立即报 SQLITE_BUSY。
- 每个版本（region 不可变、parcels、well_attributions、meta 指针）在
  同一个事务里写入，COMMIT 之前对任何读者都不可见 —— 失败或进程被杀
  时 SQLite 自动回滚，不会留下"半版"。
- meta.current_revision 是唯一的版本指针；发布时用
  `UPDATE ... WHERE current_revision = :base` 做乐观并发控制，
  同一基版的并发修订只有一份能把这个 UPDATE 做成，其余 rowcount=0 → 409。
"""
from __future__ import annotations

import os
import sqlite3

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    current_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    revision     INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    parcel_count INTEGER NOT NULL,
    well_count   INTEGER NOT NULL
);

-- 区域在首次提交后不可变，只存一份。
CREATE TABLE IF NOT EXISTS regions (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    geojson TEXT NOT NULL
);

-- 井的几何在首次提交后不可变，归属按版本重算。
CREATE TABLE IF NOT EXISTS wells (
    well_id TEXT PRIMARY KEY,
    geojson TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcels (
    revision  INTEGER NOT NULL REFERENCES versions(revision),
    parcel_id TEXT NOT NULL,
    geojson   TEXT NOT NULL,
    PRIMARY KEY (revision, parcel_id)
);

CREATE TABLE IF NOT EXISTS well_attributions (
    revision  INTEGER NOT NULL REFERENCES versions(revision),
    well_id   TEXT NOT NULL REFERENCES wells(well_id),
    status    TEXT NOT NULL CHECK (status IN ('assigned', 'pending', 'uncovered')),
    parcel_id TEXT,
    candidates TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (revision, well_id)
);
"""


def db_path() -> str:
    """每次连接时读取环境变量，便于测试切换数据库文件。"""
    return os.environ.get("DB_PATH", _DEFAULT_DB)


def connect() -> sqlite3.Connection:
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # isolation_level=None → 自动提交，事务用显式 BEGIN/COMMIT 控制。
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    """建表，并防御性清理可能残留的未提交版本数据。

    正常路径下所有写都在单事务内完成，崩溃即回滚，下面的 DELETE 应当
    永远是 no-op；保留它作为"重启不留半版"的双保险。
    """
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        if row is not None:
            current = row[0]
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM well_attributions WHERE revision > ?", (current,))
                conn.execute("DELETE FROM parcels WHERE revision > ?", (current,))
                conn.execute("DELETE FROM versions WHERE revision > ?", (current,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()

"""SQLite 连接与建表（严格对齐 MEMORY_SCHEMA_V1.md §4 / §8）。

只依赖标准库 sqlite3。WAL + foreign_keys；单进程内同步访问（P7 的 Engine 与
DSH 插件通过常驻子进程通信，不跨进程共享数据库文件句柄）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "3"

DDL_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id                     TEXT PRIMARY KEY,
    user_id                TEXT NOT NULL DEFAULT 'default',
    type                   TEXT NOT NULL CHECK (type IN ('profile','domain','state','episodic')),
    subtype                TEXT NOT NULL,
    subject_type           TEXT NOT NULL CHECK (subject_type IN ('user','person','project','org','other')),
    subject_name           TEXT,
    domain_key             TEXT,
    content                TEXT NOT NULL,
    keywords               TEXT NOT NULL DEFAULT '',
    confidence             REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    importance             TEXT NOT NULL CHECK (importance IN ('key','important','normal','low')),
    injection_policy       TEXT NOT NULL CHECK (injection_policy IN ('always','contextual','on_demand','never')),
    scope                  TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global','domain','task')),
    status                 TEXT NOT NULL CHECK (status IN ('active','superseded','archived','deleted')),
    source_message_id      TEXT,
    source_conversation_id TEXT,
    valid_from             TEXT NOT NULL,
    valid_until            TEXT,
    superseded_by          TEXT REFERENCES memories(id),
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    metadata               TEXT NOT NULL DEFAULT '{}',
    inject_count           INTEGER NOT NULL DEFAULT 0,
    use_count              INTEGER NOT NULL DEFAULT 0
)
"""

DDL_CHANGE = """
CREATE TABLE IF NOT EXISTS memory_change (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id         TEXT NOT NULL,
    action            TEXT NOT NULL,
    old_snapshot      TEXT,
    new_snapshot      TEXT,
    reason            TEXT NOT NULL,
    actor             TEXT NOT NULL,
    source_message_id TEXT,
    at                TEXT NOT NULL
)
"""

DDL_META = """
CREATE TABLE IF NOT EXISTS memory_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
)
"""

DDL_COUNTER = """
CREATE TABLE IF NOT EXISTS memory_counter (
    name       TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
)
"""

DDL_OBSERVATION = """
CREATE TABLE IF NOT EXISTS memory_observation (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    subject TEXT,
    detail  TEXT,
    at      TEXT NOT NULL
)
"""

#: 索引按检索路径设计（P2 §8），不是按字段美观
DDL_REQUEST = """
CREATE TABLE IF NOT EXISTS memory_request (
    key        TEXT PRIMARY KEY,
    op         TEXT NOT NULL,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL
)"""

DDL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_mem_policy ON memories(user_id, injection_policy, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_type_domain ON memories(user_id, type, domain_key, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(user_id, scope, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject_type, subject_name, status)",
    "CREATE INDEX IF NOT EXISTS idx_mem_valid ON memories(status, valid_until)",
    "CREATE INDEX IF NOT EXISTS idx_mem_superseded ON memories(superseded_by)",
    "CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(user_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_change_mid ON memory_change(memory_id, at)",
    "CREATE INDEX IF NOT EXISTS idx_obs_kind ON memory_observation(kind, at)",
    "CREATE INDEX IF NOT EXISTS idx_req_created ON memory_request(created_at)",
)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """打开（或创建）记忆库。``:memory:`` 用于测试。"""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表 + 建索引 + 写 schema_version。"""
    conn.executescript(
        "BEGIN IMMEDIATE;\n"
        + DDL_MEMORIES
        + ";\n"
        + DDL_CHANGE
        + ";\n"
        + DDL_META
        + ";\n"
        + DDL_COUNTER
        + ";\n"
        + DDL_OBSERVATION
        + ";\n"
        + DDL_REQUEST
        + ";\n"
        + ";\n".join(DDL_INDEXES)
        + ";\n"
        + f"INSERT OR REPLACE INTO memory_meta(k, v) VALUES ('schema_version', '{SCHEMA_VERSION}');\n"
        + "COMMIT;"
    )


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]

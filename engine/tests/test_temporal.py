"""阶段 3 回归验收（W-3.6）：时态反例集。

这里刻意不按"作者预设路径"写：每条用例先构造一个**会出错的现实场景**，
再看系统是否挡住。反例集一旦建立就不因不通过而删改（预注册，见 V1-5）。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from attune_memory import Memory
from attune_memory.db import SCHEMA_VERSION, connect, init_schema, migrate_data, table_names
from attune_memory.read import stable_items
from attune_memory.search import search
from attune_memory.util import now_iso


class TemporalCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = Memory(":memory:")
        self.store = self.mem.store

    def tearDown(self) -> None:
        self.mem.close()

    def columns(self) -> set[str]:
        return {r["name"] for r in self.store.conn.execute("PRAGMA table_info(memories)").fetchall()}


class TestSchemaContract(TemporalCase):
    def test_four_temporal_columns_exist(self) -> None:
        cols = self.columns()
        for name in ("temporal_kind", "invalidation", "event_date", "last_verified"):
            self.assertIn(name, cols)

    def test_lex_order_columns(self) -> None:
        self.assertEqual(SCHEMA_VERSION.split(".")[0], "5")
        self.assertIn("idx_event", {
            r["name"] for r in self.store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        })

    def test_legacy_database_gains_columns_without_data_loss(self) -> None:
        """R-3.1.2：老库（没有四列）迁移后数据一条不少。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            raw = sqlite3.connect(str(path))
            raw.executescript(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'default',
                    type TEXT NOT NULL, subtype TEXT NOT NULL, subject_type TEXT NOT NULL,
                    subject_name TEXT, domain_key TEXT, content TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
                    importance TEXT NOT NULL, injection_policy TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'global', status TEXT NOT NULL,
                    source_message_id TEXT, source_conversation_id TEXT,
                    valid_from TEXT NOT NULL, valid_until TEXT, superseded_by TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    inject_count INTEGER NOT NULL DEFAULT 0, use_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO memories (id, type, subtype, subject_type, content, confidence,
                    importance, injection_policy, status, valid_from, created_at, updated_at)
                VALUES ('m-legacy', 'profile', 'identity', 'user', '用户叫老王', 0.9,
                    'key', 'always', 'active', '2026-01-01T00:00:00.000Z',
                    '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
                """
            )
            raw.commit()
            raw.close()

            conn = connect(path)
            init_schema(conn)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM memories").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["content"], "用户叫老王")
            self.assertEqual(rows[0]["temporal_kind"], "current")
            self.assertIsNone(rows[0]["invalidation"])
            conn.close()

    def test_illegal_temporal_kind_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.mem.add("用户喜欢喝茶", "profile", "preference", temporal_kind="someday", explicit=True)
        self.assertIn("temporal_kind", str(ctx.exception))


class TestTemporalWrite(TemporalCase):
    def test_historical_and_event(self) -> None:
        hist = self.mem.add("用户 2020 年在某公司上班", "episodic", "event",
                            temporal_kind="historical", explicit=True)
        row = self.mem.get(hist["memory"]["id"])
        self.assertEqual(row["temporal_kind"], "historical")

        event = self.mem.add("用户 2026 年 3 月参加了考试", "episodic", "event",
                             temporal_kind="event", event_date="2026-03-01T00:00:00Z", explicit=True)
        row = self.mem.get(event["memory"]["id"])
        self.assertEqual(row["event_date"], "2026-03-01T00:00:00Z")

    def test_event_date_requires_event_kind(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self.mem.add("用户喜欢喝咖啡", "profile", "preference",
                         event_date="2026-01-01T00:00:00Z", explicit=True)
        self.assertIn("event_date", str(ctx.exception))

    def test_default_is_current(self) -> None:
        out = self.mem.add("用户叫张三", "profile", "identity", explicit=True)
        self.assertEqual(self.mem.get(out["memory"]["id"])["temporal_kind"], "current")


class TestInvalidationBinary(TemporalCase):
    def test_supersede_marks_superseded(self) -> None:
        old = self.mem.add("用户正在备考数学一", "state", "goal", explicit=True)
        self.mem.supersede(old["memory"]["id"], "用户已改考计算机 408", reason="改考")
        row = self.mem.get(old["memory"]["id"])
        self.assertEqual(row["status"], "superseded")
        self.assertEqual(row["invalidation"], "superseded")
        self.assertIsNotNone(row["valid_until"])
        self.assertIsNotNone(row["superseded_by"])

    def test_corrected_is_archived_and_never_recalled(self) -> None:
        old = self.mem.add("用户正在备考数学一", "state", "goal", explicit=True)
        self.mem.supersede(old["memory"]["id"], "用户正在备考数学二", reason="记错了",
                           invalidation="corrected")
        row = self.mem.get(old["memory"]["id"])
        self.assertEqual(row["status"], "archived")
        self.assertEqual(row["invalidation"], "corrected")

        hits = search(self.store, "备考数学一", asof=now_iso(), include_superseded=True,
                      include_expired=True, limit=10)
        self.assertNotIn(old["memory"]["id"], [h["memory"]["id"] for h in hits])


class TestTemporalRead(TemporalCase):
    def test_expired_fact_never_enters_stable_block(self) -> None:
        """R-3.4.1：#48 的现场——过期事实以前照样每轮被注入。"""
        out = self.mem.add("用户在某公司上班", "profile", "identity",
                           injection_policy="always", confidence=0.95, explicit=True)
        mid = out["memory"]["id"]
        self.assertIn(mid, [m["id"] for m in stable_items(self.store)])
        # 旧库/手工写入可能留下过期事实：绕过写入口直接改 valid_until
        with self.store.tx():
            self.store.conn.execute(
                "UPDATE memories SET valid_until=? WHERE id=?",
                ("2020-01-01T00:00:00.000Z", mid),
            )
        self.assertNotIn(mid, [m["id"] for m in stable_items(self.store)])

    def test_asof_returns_historical_fact(self) -> None:
        old = self.mem.add("用户正在备考数学一", "state", "goal", explicit=True)
        mid = old["memory"]["id"]
        # 时间戳是毫秒精度：add 与 supersede 紧挨着会落在同一毫秒里，
        # 那样 "as-of 那一瞬间" 与 valid_until 相等，边界无法区分。
        # 真实场景里两次对话至少隔几秒，这里只取最小间隔把边界让开。
        time.sleep(0.01)
        before = now_iso()
        time.sleep(0.01)
        self.mem.supersede(mid, "用户已改考计算机 408", reason="改考")

        now_hits = search(self.store, "备考数学一", limit=10)
        self.assertNotIn(mid, [h["memory"]["id"] for h in now_hits])

        past_hits = search(self.store, "备考数学一", asof=before, limit=10)
        self.assertIn(mid, [h["memory"]["id"] for h in past_hits])


class TestMigration(TemporalCase):
    def test_migrate_on_current_schema_is_noop(self) -> None:
        self.mem.add("用户正在备考数学一", "state", "goal", explicit=True)
        result = migrate_data(self.store.conn)
        self.assertEqual(result, {"added_columns": 0, "filled_temporal_kind": 0, "filled_invalidation": 0})

    def test_legacy_superseded_rows_backfilled_conservatively(self) -> None:
        """R-3.5.2：旧库里的 superseded 一律回填成 superseded（不猜 corrected）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            raw = sqlite3.connect(str(path))
            raw.executescript(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT 'default',
                    type TEXT NOT NULL, subtype TEXT NOT NULL, subject_type TEXT NOT NULL,
                    subject_name TEXT, domain_key TEXT, content TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
                    importance TEXT NOT NULL, injection_policy TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'global', status TEXT NOT NULL,
                    source_message_id TEXT, source_conversation_id TEXT,
                    valid_from TEXT NOT NULL, valid_until TEXT, superseded_by TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    inject_count INTEGER NOT NULL DEFAULT 0, use_count INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO memories (id, type, subtype, subject_type, content, confidence,
                    importance, injection_policy, status, valid_from, created_at, updated_at)
                VALUES ('m-old', 'state', 'goal', 'user', '用户正在备考数学一', 0.9,
                    'normal', 'contextual', 'superseded', '2026-01-01T00:00:00.000Z',
                    '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z');
                """
            )
            raw.commit()
            raw.close()

            conn = connect(path)
            init_schema(conn)
            result = migrate_data(conn)
            self.assertEqual(result["added_columns"], 0)  # init_schema 已经补过列
            self.assertGreaterEqual(result["filled_invalidation"], 1)
            row = conn.execute("SELECT * FROM memories WHERE id='m-old'").fetchone()
            self.assertEqual(row["invalidation"], "superseded")
            self.assertEqual(row["temporal_kind"], "current")
            # 幂等：再跑一次没有任何变化
            again = migrate_data(conn)
            self.assertEqual(again["filled_invalidation"], 0)
            conn.close()

    def test_tables_intact_after_migrate(self) -> None:
        migrate_data(self.store.conn)
        self.assertEqual(
            table_names(self.store.conn),
            ["memories", "memory_change", "memory_counter", "memory_gram",
             "memory_gram_df", "memory_meta", "memory_observation", "memory_request"],
        )


if __name__ == "__main__":
    unittest.main()

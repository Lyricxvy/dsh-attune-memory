"""v0.3.0 阶段 2 验收：数据一致性 + 规模（W-2.1 ~ W-2.4）。

对应 RUNBOOK 用例：R-2.1.x purge 原子/tombstone、R-2.2.x merge/update 统一校验、
R-2.3.x 倒排索引、R-2.4.x 分组不变式与毫秒时间。
"""
from __future__ import annotations

import time
import unittest

from attune_memory import const
from attune_memory.db import connect, init_schema
from attune_memory.manager import Memory
from attune_memory.store import Store
from attune_memory.util import fmt_iso, now_iso, parse_iso
from datetime import datetime, timedelta, timezone


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = Memory(":memory:")

    def tearDown(self) -> None:
        self.mem.close()

    def add(self, content, type, subtype, **kw):  # noqa: A002
        kw.setdefault("explicit", True)
        return self.mem.add(content, type, subtype, **kw)


class TestPurgeAtomic(Base):
    """W-2.1：purge 单事务 + tombstone。"""

    def test_purge_keeps_tombstone_and_removes_row(self):
        """R-2.1.1：记忆行没了，但 memory_change 里的 purge 流水留着（可审计）。"""
        mid = self.add("临时结论：先跑测试再提交", "episodic", "lesson")["memory"]["id"]
        self.mem.forget(mid, reason="不要了")
        self.mem.purge(mid, reason="确认删除")
        self.assertIsNone(self.mem.get(mid))
        rows = self.mem.store.conn.execute(
            "SELECT action FROM memory_change WHERE memory_id=?", (mid,)
        ).fetchall()
        actions = [r["action"] for r in rows]
        self.assertIn("purge", actions, actions)
        self.assertIn("delete", actions, actions)

    def test_purge_rejects_active(self):
        """R-2.1.2：active 不能直接物理删除。"""
        mid = self.add("用户叫测试丁", "profile", "identity")["memory"]["id"]
        with self.assertRaises(Exception) as ctx:
            self.mem.purge(mid)
        self.assertIn("forget", str(ctx.exception))
        self.assertIsNotNone(self.mem.get(mid))

    def test_purge_rolls_back_midway(self):
        """R-2.1.3：删主行时出错 → 整个 purge 回滚，记忆仍是 deleted、流水完整。"""
        mid = self.add("临时结论：缓存要放在 system prompt", "episodic", "lesson")["memory"]["id"]
        self.mem.forget(mid)
        before = len(self.mem.store.changes(mid))
        raw = self.mem.store.conn

        class Boom(RuntimeError):
            pass

        class Flaky:
            """代理连接：只在删主行那一步抛错，其余照常转发。"""

            def __getattr__(self, name):
                return getattr(raw, name)

            def execute(self, sql, *args):
                if isinstance(sql, str) and sql.startswith("DELETE FROM memories"):
                    raise Boom("注入的中断")
                return raw.execute(sql, *args)

        self.mem.store.conn = Flaky()  # type: ignore[assignment]
        try:
            with self.assertRaises(Boom):
                self.mem.purge(mid)
        finally:
            self.mem.store.conn = raw
        row = self.mem.get(mid)
        self.assertIsNotNone(row, "回滚后记忆必须还在")
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(len(self.mem.store.changes(mid)), before, "流水不该被多写一条 purge")

    def test_purge_refuses_to_break_supersede_chain(self):
        """被 superseded_by 指着的条目不能直接删（会留断链）。"""
        old = self.add("用户正在备考数学一", "state", "goal")["memory"]["id"]
        new = self.add("用户改考计算机 408", "state", "goal",
                       supersede_id=old)["memory"]["id"]
        # 旧条通过 superseded_by 指着新条：真正会断链的是"新条"
        self.mem.forget(new)
        with self.assertRaises(Exception) as ctx:
            self.mem.purge(new)
        self.assertIn("superseded_by", str(ctx.exception))
        self.assertIsNotNone(self.mem.get(old))


class TestMergeUpdateValidation(Base):
    """W-2.2：merge/update 走同一套 validate + 生命周期。"""

    def test_merge_rejects_empty_content(self):
        """R-2.2.1：合并成空内容必须被拒。"""
        a = self.add("用户喜欢红色", "profile", "preference")["memory"]["id"]
        b = self.add("用户喜欢蓝色", "profile", "preference")["memory"]["id"]
        with self.assertRaises(Exception) as ctx:
            self.mem.merge([a, b], "   ")
        self.assertIn("content", str(ctx.exception))

    def test_merge_rejects_illegal_importance(self):
        """R-2.2.2：合并时传非法 importance 必须被拒（以前能直接写进库）。"""
        a = self.add("用户喜欢红色", "profile", "preference")["memory"]["id"]
        b = self.add("用户喜欢蓝色", "profile", "preference")["memory"]["id"]
        with self.assertRaises(Exception) as ctx:
            self.mem.merge([a, b], "用户喜欢红色和蓝色", importance="super-high")
        self.assertIn("importance", str(ctx.exception))

    def test_update_valid_until_in_past_archives(self):
        """R-2.2.3：把 valid_until 改到过去 → 立刻归档，不再注入。"""
        long_ago = fmt_iso(datetime.now(timezone.utc) - timedelta(days=30))
        mid = self.add("用户这学期在准备数学一", "state", "goal",
                       valid_from=long_ago)["memory"]["id"]
        past = fmt_iso(datetime.now(timezone.utc) - timedelta(days=1))
        updated = self.mem.update(mid, valid_until=past, reason="补上失效时间")
        self.assertEqual(updated["status"], "archived", updated)
        ctx = self.mem.read_for_context("数学一怎么复习")
        self.assertNotIn(mid, ctx["injected_ids"])

    def test_update_still_rejects_type_change(self):
        """update 仍然不允许改 type（那是另一条事实）。"""
        mid = self.add("用户喜欢红色", "profile", "preference")["memory"]["id"]
        with self.assertRaises(Exception) as ctx:
            self.mem.update(mid, type="episodic")
        self.assertIn("type", str(ctx.exception))


class TestKeywordIndex(Base):
    """W-2.3：写入索引（倒排）而不是全表扫。"""

    def _seed(self, n=40):
        topics = ["极限", "传热学", "概率", "线代", "英语", "政治", "复试", "调剂"]
        for i in range(n):
            t = topics[i % len(topics)]
            # 内容里刻意不带"数字+单位"：那会被 key_values 判成同一属性的不同取值，
            # 触发互斥取代，种子数据就变成"只剩最后一条"了。
            self.add(f"{t}笔记第{i}号要点：先看概念再看例题", "domain", "fact",
                     domain_key="study", keywords=[t])

    def test_index_matches_full_scan(self):
        """R-2.3.2：倒排结果与全表扫完全一致（集合相等）。"""
        self._seed()
        terms = ["极限", "传热", "第3章", "英语"]
        got = {m["id"] for m in self.mem.store.near_keywords(terms, limit=1000)}
        allrows = self.mem.store.conn.execute(
            "SELECT * FROM memories WHERE user_id='default' AND status='active'"
        ).fetchall()
        expect = set()
        for r in allrows:
            hay = (r["keywords"] or "") + " " + (r["content"] or "")
            if any(t in hay for t in terms):
                expect.add(r["id"])
        self.assertEqual(got, expect)

    def test_keyword_update_reindexes(self):
        """R-2.3.3：改了 keywords 之后，新词能命中、旧词不再命中。"""
        mid = self.add("用户每天刷两套卷子", "domain", "fact",
                       domain_key="study.math", keywords=["真题"])["memory"]["id"]
        self.assertTrue(self.mem.store.near_keywords(["真题"], limit=10))
        self.mem.update(mid, keywords=["模拟题"], reason="改叫法")
        self.assertFalse(self.mem.store.near_keywords(["真题"], limit=10))
        self.assertTrue(self.mem.store.near_keywords(["模拟题"], limit=10))

    def test_rebuild_index_repairs(self):
        """索引坏了不丢数据：清空后 rebuild 能恢复（R-2.3 的回滚路径）。"""
        self._seed(10)
        before = self.mem.store.conn.execute("SELECT count(*) AS n FROM memory_gram").fetchone()["n"]
        self.mem.store.conn.execute("DELETE FROM memory_gram")
        self.assertFalse(self.mem.store.near_keywords(["极限"], limit=10))
        after = self.mem.store.rebuild_index()
        self.assertEqual(after, before)
        self.assertTrue(self.mem.store.near_keywords(["极限"], limit=10))

    def test_index_keeps_writes_near_linear(self):
        """写入耗时不随库规模平方增长（轻量冒烟：500 条写入 < 8 秒）。"""
        start = time.perf_counter()
        self._seed(500)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 8.0, f"500 条写入用了 {elapsed:.1f}s，索引可能没生效")


class TestInvariantsAndTime(Base):
    """W-2.4：按 user 分组 + 毫秒时间戳。"""

    def test_users_are_grouped(self):
        """R-2.4.1：两个用户的同槽位 active 各自独立，不误报。"""
        self.add("用户正在备考数学一", "state", "goal")
        self.mem.add("另一个用户正在备考数学一", "state", "goal", user_id="u2", explicit=True)
        problems = self.mem.store.verify_invariants()
        self.assertEqual(problems, [])

    def test_same_slot_identity_is_reported(self):
        """R-2.4.3：profile/identity 同槽位两条 active → 报问题。"""
        store = self.mem.store
        now = now_iso()
        for i in range(2):
            store.insert({
                "id": f"m-manual-{i}", "user_id": "default", "type": "profile",
                "subtype": "identity", "subject_type": "user", "subject_name": None,
                "domain_key": None, "content": f"用户叫测试{i}", "keywords": "用户 测试",
                "confidence": 0.9, "importance": "key", "injection_policy": "contextual",
                "scope": "global", "status": "active", "source_message_id": None,
                "source_conversation_id": None, "valid_from": now, "valid_until": None,
                "superseded_by": None, "created_at": now, "updated_at": now,
                "metadata": {}, "inject_count": 0, "use_count": 0,
            }, reason="手工构造", actor="test")
        problems = self.mem.store.verify_invariants()
        self.assertTrue(any("profile/identity" in p for p in problems), problems)

    def test_millisecond_timestamps_are_ordered(self):
        """R-2.4.2：同一秒的两次写入，毫秒时间戳能分出先后。"""
        base = datetime(2026, 11, 22, 12, 0, 0, 1000, tzinfo=timezone.utc)
        a = fmt_iso(base)
        b = fmt_iso(base + timedelta(milliseconds=5))
        self.assertLess(a, b)
        self.assertLess(parse_iso(a), parse_iso(b))
        self.assertTrue(a.endswith("Z") and "." in a and len(a) == len(b))
        # 旧秒级格式仍可解析且语义不变
        self.assertEqual(parse_iso("2026-11-19T13:47:21Z").second, 21)

    def test_written_rows_use_millisecond_precision(self):
        mid = self.add("用户叫测试戊", "profile", "identity")["memory"]["id"]
        row = self.mem.get(mid)
        self.assertRegex(row["created_at"], r"\.\d{3}Z$")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestIndexedCandidates(Base):
    """R-2.3.5：检索必须走倒排，不能只按 updated_at 取最近 200 条。

    本地复现的原始缺陷：写完 1 条相关事实再写 500 条噪声，query 命中 0 条——
    相关记忆被"最近写入"挤出候选窗口。库上三万条时这就是外部基线 0.4% 的原因。
    """

    def test_old_relevant_memory_survives_recency_flood(self) -> None:
        rel = self.mem.add(
            "I graduated with a Master of Science in Computer Science.",
            "episodic", "event",
            keywords=["graduated", "master", "science", "computer", "degree"],
            explicit=True,
        )
        rel_id = rel["memory"]["id"]
        for i in range(260):                      # 超过 MAX_CANDIDATES(200)
            self.mem.add(
                f"Session {i} notes about grocery shopping and laundry list item {i}.",
                "episodic", "event", keywords=[f"noise{i}", "grocery", "laundry"],
                explicit=True,
            )
        hits = self.mem.search("What degree did I graduate with?", limit=5)
        self.assertIn(rel_id, [h["id"] for h in hits])
        ctx = self.mem.read_for_context(query="What degree did I graduate with?")["context"]
        self.assertIn(rel_id, ctx["ids"])

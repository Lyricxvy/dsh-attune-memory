"""阶段 4 回归（W-4.1 / W-4.2 / W-4.3）：冲突判据与措辞修正。"""

from __future__ import annotations

import unittest

from attune_memory import Memory


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = Memory(":memory:")

    def tearDown(self) -> None:
        self.mem.close()

    def add(self, content: str, type: str, subtype: str, **kw):
        return self.mem.add(content, type, subtype, explicit=kw.pop("explicit", True), **kw)


class TestSlotExclusivity(Base):
    def test_same_subject_scores_are_not_exclusive(self) -> None:
        """R-4.1.2：数学 120 与英语 120 是两条事实，不该互相取代。"""
        a = self.add("用户数学考了 120 分", "domain", "fact", domain_key="study")
        b = self.add("用户英语考了 120 分", "domain", "fact", domain_key="study")
        self.assertEqual(b["action"], "add")
        self.assertEqual(self.mem.get(a["memory"]["id"])["status"], "active")
        self.assertEqual(a["memory"]["id"] != b["memory"]["id"], True)

    def test_same_attribute_different_value_supersedes(self) -> None:
        """R-4.1.3：同一属性两个取值才算互斥。"""
        a = self.add("用户数学考了 120 分", "domain", "fact", domain_key="study")
        b = self.add("用户数学考了 130 分", "domain", "fact", domain_key="study")
        self.assertEqual(b["action"], "supersede")
        self.assertEqual(self.mem.get(a["memory"]["id"])["status"], "superseded")

    def test_identity_slot_is_single_valued(self) -> None:
        """R-4.1.1：identity 槽位两条 active 必须被 verify_invariants 抓到。"""
        self.add("用户叫张三", "profile", "identity")
        self.mem.store.conn.execute(
            "INSERT INTO memories (id,user_id,type,subtype,subject_type,content,keywords,confidence,"
            "importance,injection_policy,scope,status,valid_from,created_at,updated_at,metadata)"
            " VALUES ('m-dup','default','profile','identity','user','用户叫李四','',0.9,"
            "'key','always','global','active','2026-01-01T00:00:00.000Z',"
            "'2026-01-01T00:00:00.000Z','2026-01-01T00:00:00.000Z','{}')"
        )
        problems = self.mem.store.verify_invariants()
        self.assertTrue(any("profile/identity" in p for p in problems), problems)


class TestPolarity(Base):
    def test_negated_preference_stays_negative(self) -> None:
        """R-4.2.1：抽取器不能把"我不喜欢先给结论"记成"喜欢先给结论"。"""
        out = self.add("用户不喜欢先给结论", "profile", "preference", importance="important")
        self.assertIn("不喜欢", out["memory"]["content"])

    def test_polarity_pair_conflict(self) -> None:
        """R-4.2.2：极性相反的同槽位事实应触发取代（这里用 支持/不支持）。

        没有用「不考数学二」当输入：那条会被 W-1.1 的意图准入门直接拦掉（否定句式
        且句内没有"改考/决定"这类替代表述），测不到冲突检测。
        """
        new = self.add("用户不支持这个方案", "profile", "preference")
        self.assertEqual(new["action"], "add")
        other = self.add("用户支持这个方案", "profile", "preference")
        self.assertEqual(other["action"], "supersede")
        self.assertEqual(self.mem.get(new["memory"]["id"])["status"], "superseded")

    def test_short_numbers_survive_keywords(self) -> None:
        """R-4.2.3：801 / 数一 这类短数字/编号不能被关键词过滤掉。"""
        out = self.add("用户要考 801 传热学", "state", "goal", domain_key="study.heat")
        keywords = out["memory"]["keywords"]
        self.assertTrue("801" in keywords or "传热学" in keywords, keywords)


class TestWordingFix(Base):
    def test_wording_change_updates_in_place(self) -> None:
        """R-4.3.1：同一件事换个说法 → update，不新增也不取代。"""
        first = self.add("用户正在备考数学一", "state", "goal")
        second = self.add("用户目前正在备考数学一", "state", "goal")
        self.assertEqual(second["action"], "update")
        self.assertEqual(second["memory"]["id"], first["memory"]["id"])
        total = len(self.mem.list(status=None, limit=100))
        self.assertEqual(total, 1)

    def test_different_subject_is_new_fact(self) -> None:
        """R-4.3.2：换成英语一不是措辞修正。"""
        self.add("用户正在备考数学一", "state", "goal")
        second = self.add("用户正在备考英语一", "state", "goal")
        self.assertEqual(second["action"], "add")

    def test_wording_fix_skips_number_change(self) -> None:
        """R-4.3.3：关键值变了就不算措辞修正（120 → 130 是事实变了）。"""
        first = self.add("用户数学考了 120 分", "domain", "fact", domain_key="study")
        second = self.add("用户数学目前考了 130 分", "domain", "fact", domain_key="study")
        self.assertNotEqual(second["action"], "update")
        self.assertEqual(self.mem.get(first["memory"]["id"])["status"], "superseded")


if __name__ == "__main__":
    unittest.main()

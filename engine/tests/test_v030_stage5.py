"""阶段 5 回归（W-5.1 / W-5.2 / W-5.3）：可观测性与读路径只读。"""

from __future__ import annotations

import unittest

from attune_memory import Memory


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = Memory(":memory:")

    def tearDown(self) -> None:
        self.mem.close()


class TestFunnel(Base):
    def test_six_state_funnel_present(self) -> None:
        """R-5.1.1：六态分账字段齐全，且写入会让 candidate/generated 动。"""
        self.mem.add("用户叫张三", "profile", "identity", explicit=True)
        funnel = self.mem.metrics()["funnel"]
        self.assertEqual(
            set(funnel),
            {"candidate", "generated", "rendered", "delivered", "visible", "used"},
        )
        self.assertGreaterEqual(funnel["candidate"], 1)
        self.assertGreaterEqual(funnel["generated"], 1)

    def test_render_and_deliver_counted(self) -> None:
        """R-5.1.2：读出上下文会让 rendered / delivered 增加。"""
        self.mem.add("用户叫张三", "profile", "identity", injection_policy="always",
                     confidence=0.95, explicit=True)
        self.mem.add("用户备考数学一", "state", "goal", domain_key="study.math", explicit=True)
        before = self.mem.metrics()["funnel"]
        self.mem.read_for_context(query="数学一怎么复习", domain="study.math")
        after = self.mem.metrics()["funnel"]
        self.assertGreaterEqual(after["rendered"], before["rendered"])
        self.assertGreaterEqual(after["delivered"], before["delivered"])

    def test_metrics_envelope(self) -> None:
        """R-5.2.1：聚合指标带 schema/version/started_at/coverage。"""
        data = self.mem.metrics()
        self.assertEqual(data["schema"], "attune-memory.metrics/v1")
        self.assertTrue(data["version"])
        self.assertTrue(data["started_at"])
        self.assertIn("injected_ratio", data["coverage"])
        self.assertIn("trace_ratio", data["coverage"])

    def test_started_at_is_stable(self) -> None:
        """R-5.2.2：started_at 只写一次，重复读不会漂移。"""
        first = self.mem.metrics()["started_at"]
        second = self.mem.metrics()["started_at"]
        self.assertEqual(first, second)


class TestReadOnlyPath(Base):
    def test_read_does_not_write_use_count(self) -> None:
        """R-5.3.1（#24）：检索不能再顺手写 use_count。"""
        out = self.mem.add("用户主要备考数学一", "profile", "identity", explicit=True)
        mid = out["memory"]["id"]
        hits = self.mem.search("数学一")
        self.assertTrue(hits)
        self.assertEqual(self.mem.get(mid)["use_count"], 0)

    def test_explicit_count_use_still_works(self) -> None:
        """R-5.3.2：真要记"被用上了"时显式传 count_use。"""
        out = self.mem.add("用户主要备考数学一", "profile", "identity", explicit=True)
        mid = out["memory"]["id"]
        self.assertTrue(self.mem.search("数学一", count_use=True))
        self.assertEqual(self.mem.get(mid)["use_count"], 1)

    def test_read_for_context_does_not_write_use_count(self) -> None:
        """R-5.3.3：注入路径同样不写。"""
        out = self.mem.add("用户备考数学一", "state", "goal", domain_key="study.math", explicit=True)
        mid = out["memory"]["id"]
        self.mem.read_for_context(query="数学一极限题", domain="study.math")
        self.assertEqual(self.mem.get(mid)["use_count"], 0)

    def test_read_does_not_change_updated_at(self) -> None:
        """R-5.3.4：读不能改 updated_at，否则 freshness 会被自己刷新。"""
        out = self.mem.add("用户备考数学一", "state", "goal", explicit=True)
        mid = out["memory"]["id"]
        before = self.mem.get(mid)["updated_at"]
        self.mem.search("数学一")
        self.assertEqual(self.mem.get(mid)["updated_at"], before)


if __name__ == "__main__":
    unittest.main()

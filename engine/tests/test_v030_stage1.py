"""v0.3.0 阶段 1 验收：职责分层（W-1.1 意图准入门 / W-1.2 目标层声明 / W-1.3 task scope）。

对应用例编号见 DSH_v0.3.0_EXECUTION_RUNBOOK.md：
  R-1.1.1 运行时状态被拒   R-1.1.2 文件可验证被拒
  R-1.1.3 否定极性不记反   R-1.1.4 真实偏好不被误拦
  R-1.2.1 describe 每 op 有 layer   R-1.2.2 声明错层被拒
  R-1.3.1 task 无 task_key 拒绝     R-1.3.2 task_key 隔离
  R-1.3.3 task 记忆不进稳定块
"""
from __future__ import annotations

import unittest

from attune_memory import const
from attune_memory.manager import Memory
from attune_memory.rpc import LAYERS, OPS, describe_ops, handle


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = Memory(":memory:")

    def tearDown(self) -> None:
        self.mem.close()

    def rpc(self, op: str, **args):
        return handle(self.mem, {"id": 1, "op": op, "args": args})


class TestIntentGate(Base):
    """W-1.1：写入意图准入门。"""

    def test_runtime_state_is_rejected(self):
        """R-1.1.1：'本窗口负责记忆分析'是工位，不是偏好。"""
        out = self.mem.add("本窗口负责记忆分析", "profile", "interaction")
        self.assertEqual(out["action"], "ignore", out)
        self.assertIn("运行时状态", out["reason"])
        self.assertEqual(self.mem.list(type="profile"), [])

    def test_file_verifiable_is_rejected(self):
        """R-1.1.2：能从仓库/文档直接查到的东西不该进记忆。"""
        out = self.mem.add("信箱机制已废弃", "domain", "decision")
        self.assertEqual(out["action"], "ignore", out)
        self.assertIn("文件", out["reason"])

    def test_negation_is_not_recorded_as_opposite(self):
        """R-1.1.3：'不考数学二'不能落成'考数学二'（也不能落成任何一条事实）。"""
        out = self.mem.add("不考数学二", "state", "goal")
        self.assertEqual(out["action"], "ignore", out)
        self.assertIn("否定", out["reason"])
        self.assertEqual(self.mem.list(type="state"), [])
        # 关键词里也不能留下"数学二"，否则将来检索会把它当正面事实召回
        self.assertNotIn("数学二", str(self.mem.list()))

    def test_real_preference_still_passes(self):
        """R-1.1.4：'完整而非简洁'是真实偏好，不能被否定/文件规则误伤。"""
        out = self.mem.add("报告写得完整而非简洁", "profile", "preference", explicit=True)
        self.assertEqual(out["action"], "add", out)
        self.assertEqual(self.mem.list(type="profile")[0]["status"], "active")

    def test_negation_with_correction_passes(self):
        """否定 + 明确改成什么 → 放行（有效信息是后半句，交给决策表取代旧条）。"""
        old = self.mem.add("用户正在备考数学一", "state", "goal", explicit=True,
                           confidence=0.9)
        out = self.mem.add("用户不考数学一了，改考计算机 408", "state", "goal",
                           explicit=True, confidence=0.9)
        self.assertEqual(out["action"], "supersede", out)
        self.assertEqual(self.mem.get(old["memory"]["id"])["status"], "superseded")

    def test_explicit_does_not_bypass_intent_gate(self):
        """explicit 只跳过低级词表（疑问/指代），不跳过"这是工位不是偏好"。"""
        out = self.mem.add("本会话主要在处理打包", "episodic", "event", explicit=True)
        self.assertEqual(out["action"], "ignore", out)

    def test_rejection_is_counted_and_logged(self):
        """拒写要留痕：计数 + 丢弃日志（用户问"我明明说过"时能解释）。"""
        self.mem.add("本窗口负责记忆分析", "profile", "interaction")
        self.assertEqual(self.mem.store.counters().get("intent_rejected"), 1)
        rows = self.mem.discarded(limit=5)
        self.assertTrue(any("运行时状态" in (r["reason"] or "") for r in rows), rows)


class TestLayerContract(Base):
    """W-1.2：每个 op 声明目标层，写操作只能在 fact 层。"""

    def test_every_op_declares_layer(self):
        """R-1.2.1：describe_ops 每个 op 都带 layer，且取值在 LAYERS 内。"""
        for row in describe_ops():
            self.assertIn("layer", row, row)
            self.assertIn(row["layer"], LAYERS, row)
        self.assertEqual(OPS["search"].layer, "assembly")
        self.assertEqual(OPS["read_for_context"].layer, "assembly")
        self.assertEqual(OPS["add"].layer, "fact")
        self.assertEqual(OPS["supersede"].layer, "fact")

    def test_write_op_wrong_layer_is_rejected(self):
        """R-1.2.2：写 op 声明成 assembly → 拒绝，且不落库。"""
        resp = handle(self.mem, {
            "id": 1, "op": "add", "layer": "assembly",
            "args": {"content": "用户叫测试乙", "type": "profile", "subtype": "identity"},
        })
        self.assertFalse(resp["ok"], resp)
        self.assertIn("layer 不匹配", resp["error"])
        self.assertEqual(self.mem.list(), [])

    def test_unknown_layer_is_rejected(self):
        resp = handle(self.mem, {
            "id": 1, "op": "add", "layer": "bogus",
            "args": {"content": "用户叫测试乙", "type": "profile", "subtype": "identity"},
        })
        self.assertFalse(resp["ok"], resp)
        self.assertIn("非法 layer", resp["error"])

    def test_evidence_layer_is_declared_but_unused(self):
        """v0.3.0 只声明 evidence 层，没有任何 op 落在它上面（Raw-history RAG 在 v0.4.0）。"""
        self.assertIn("evidence", LAYERS)
        self.assertNotIn("evidence", {spec.layer for spec in OPS.values()})

    def test_missing_layer_is_compatible(self):
        """旧客户端不传 layer：按 op 自己的声明走，不能因此写不进去。"""
        resp = self.rpc("add", content="用户叫测试乙", type="profile", subtype="identity")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["result"]["action"], "add")


class TestTaskScope(Base):
    """W-1.3：task scope 有真实落库与检索通道，且不污染稳定块。"""

    def test_task_scope_requires_task_key(self):
        """R-1.3.1：scope=task 但没有 metadata.task_key → 拒绝。"""
        with self.assertRaises(Exception) as ctx:
            self.mem.add("正在处理打包脚本", "state", "activity", scope="task")
        self.assertIn("task_key", str(ctx.exception))

    def test_task_key_isolates_retrieval(self):
        """R-1.3.2：task_key=A 写的，用 task_key=B 查不到；用 A 能查到。"""
        out = self.mem.add("打包任务里用的密钥文件名是 build.key", "domain", "convention",
                           scope="task", metadata={"task_key": "publish"},
                           explicit=True)
        self.assertEqual(out["action"], "add", out)
        hit_a = self.mem.search("build.key", task_key="publish")
        self.assertTrue(hit_a, "同 task_key 应该能检索到")
        hit_b = self.mem.search("build.key", task_key="other")
        self.assertEqual(hit_b, [], "别的 task_key 不该看到")

    def test_task_memory_never_in_stable_block(self):
        """R-1.3.3：task 记忆不进稳定块（哪怕手工把它写成 always）。"""
        self.mem.add("任务内约定：先跑测试再提交", "profile", "interaction",
                     scope="task", metadata={"task_key": "t1"}, explicit=True)
        ctx = self.mem.read_for_context("随便问一句")
        self.assertEqual(ctx["stable"]["ids"], [])
        self.assertNotIn("任务内约定", ctx["stable"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

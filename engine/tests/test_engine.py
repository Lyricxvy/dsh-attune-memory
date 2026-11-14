"""Memory Engine V1 独立测试。

跑法（在 ``engine/`` 目录下）：

    python -m unittest discover -s tests -v

这里不测 harness、不测 prompt，只测引擎自身的契约：
写管线七种动作、检索公式、读取预算、生命周期与可解释性。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from attune_memory import Memory  # noqa: E402
from attune_memory import const, rpc, text  # noqa: E402
from attune_memory.store import InvariantError  # noqa: E402
from attune_memory.write import CandidateError  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "mem.db")
        self.mem = Memory(self.path)

    def tearDown(self) -> None:
        self.mem.close()
        self.tmp.cleanup()

    def add(self, content: str, type: str, subtype: str, **kw):  # noqa: A002
        return self.mem.add(content, type, subtype, **kw)


class TestWrite(Base):
    def test_add_creates_active_row_with_source(self):
        out = self.add("用户希望被称呼为老张", "profile", "identity",
                       injection_policy="always", importance="key",
                       source_message_id="msg-1", source_conversation_id="conv-1")
        self.assertEqual(out["action"], "add")
        row = out["memory"]
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["source_message_id"], "msg-1")
        self.assertEqual(row["source_conversation_id"], "conv-1")
        self.assertTrue(row["keywords"])  # 没给 keywords 时自动兜底，保证可检索
        self.assertEqual(row["inject_count"], 0)

    def test_ignore_trash(self):
        before = self.mem.stats()["total"]
        out = self.add("今天天气不错", "episodic", "event")
        self.assertEqual(out["action"], "ignore")
        self.assertIsNone(out["memory"])
        self.assertEqual(self.mem.stats()["total"], before)

    def test_ignore_question_and_task_instruction(self):
        self.assertEqual(self.add("这道题怎么做？", "episodic", "event")["action"], "ignore")
        self.assertEqual(self.add("帮我改一下这段代码", "episodic", "event")["action"], "ignore")

    def test_gate_is_sentence_shaped_not_word_matched(self):
        """W3：疑问词/祈使词出现在句中不算提问/任务。"""
        # 不是提问：问词在句尾的陈述
        out = self.add("用户说膝盖一冷就疼，不知道为什么", "profile", "preference",
                       explicit=False, confidence=0.6)
        self.assertEqual(out["action"], "add")
        # 不是任务：祈使词在句中
        out = self.add("用户说这个习惯帮他省了很多时间", "profile", "preference",
                       explicit=False, confidence=0.6)
        self.assertEqual(out["action"], "add")

    def test_gate_rejects_question_at_sentence_start(self):
        out = self.add("为什么膝盖一冷就疼", "profile", "preference",
                       explicit=False, confidence=0.6)
        self.assertEqual(out["action"], "ignore")

    def test_explicit_remember_bypasses_question_word_but_not_question_mark(self):
        # 模型显式记得的候选，不因句首疑问词被拦
        out = self.add("为什么用户一冷就膝盖疼——医生说是旧伤", "profile", "preference",
                       explicit=True, confidence=0.9)
        self.assertEqual(out["action"], "add")
        # 但整句是问句仍然拦（结构性问题，不是词表）
        out = self.add("这道题怎么做？", "episodic", "event", explicit=True)
        self.assertEqual(out["action"], "ignore")

    def test_subject_collapse_is_rejected(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("我朋友小王喜欢吃红烧肉", "profile", "preference")
        self.assertIn("subject_type", str(ctx.exception))

    def test_third_party_subject_is_allowed(self):
        out = self.add("小王喜欢吃红烧肉", "profile", "preference",
                       subject_type="person", subject_name="小王",
                       injection_policy="on_demand")
        self.assertEqual(out["action"], "add")
        self.assertEqual(out["memory"]["subject_type"], "person")
        self.assertEqual(out["memory"]["subject_name"], "小王")

    def test_non_user_subject_cannot_be_always(self):
        with self.assertRaises(InvariantError):
            self.add("小王喜欢吃红烧肉", "profile", "interaction",
                     subject_type="person", subject_name="小王",
                     injection_policy="always")

    def test_touch_on_equivalent_duplicate(self):
        first = self.add("用户希望先讲思路再给步骤", "profile", "interaction", confidence=0.6)
        second = self.add("用户希望先讲思路再给步骤", "profile", "interaction", confidence=0.6)
        self.assertEqual(second["action"], "touch")
        self.assertEqual(second["memory"]["id"], first["memory"]["id"])
        self.assertAlmostEqual(second["memory"]["confidence"], 0.65, places=3)
        self.assertEqual(self.mem.stats()["total"], 1)

    def test_touch_confidence_is_capped(self):
        self.add("用户喜欢用 Python 写脚本", "profile", "preference", confidence=0.94)
        out = self.add("用户喜欢用 Python 写脚本", "profile", "preference", confidence=0.94)
        self.assertEqual(out["action"], "touch")
        self.assertLessEqual(out["memory"]["confidence"], 0.95)

    def test_inferred_candidate_is_downgraded(self):
        out = self.add("用户可能偏好极简风格", "profile", "preference",
                       explicit=False, confidence=0.99, injection_policy="always")
        self.assertEqual(out["memory"]["confidence"], 0.7)
        self.assertEqual(out["memory"]["injection_policy"], "contextual")

    def test_inferred_interaction_is_narrowed_to_on_demand(self):
        """W5：正则抽的 interaction 常是任务局部要求，默认不主动注入。"""
        out = self.add("用户要求以后的回答方式：这章用图讲", "profile", "interaction",
                       explicit=False, confidence=0.65)
        self.assertEqual(out["memory"]["injection_policy"], "on_demand")
        self.assertEqual(out["memory"]["metadata"]["scope_hint"], "inferred-interaction-narrowed")

    def test_explicit_interaction_keeps_contextual(self):
        out = self.add("用户要求以后的回答方式：这章用图讲", "profile", "interaction",
                       explicit=True, confidence=0.9)
        self.assertEqual(out["memory"]["injection_policy"], "contextual")

    def test_validation_messages_list_legal_values(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("随便一条够长的内容", "profile", "不存在的子类型")
        self.assertIn("interaction", str(ctx.exception))


class TestStateTransition(Base):
    def test_kaoyan_then_not_kaoyan_uses_supersede(self):
        first = self.add("用户正在备考研究生入学考试（考研）", "state", "goal",
                         importance="important", valid_from="2026-09-01T00:00:00Z")
        self.assertEqual(first["action"], "add")
        old_id = first["memory"]["id"]

        second = self.add("用户决定不考研了，改为直接找工作", "state", "goal",
                          importance="important")
        self.assertEqual(second["action"], "supersede", f"实际：{second['action']}")

        old = self.mem.get(old_id)
        new = second["memory"]
        self.assertEqual(old["status"], "superseded")
        self.assertIsNotNone(old["valid_until"])
        self.assertEqual(old["superseded_by"], new["id"])
        self.assertEqual(new["status"], "active")
        # 关键：不是"并列新增两条 active"
        active = self.mem.list(type="state")
        self.assertEqual([m["id"] for m in active], [new["id"]])

    def test_superseded_chain_is_explainable(self):
        first = self.add("用户正在备考研究生入学考试（考研）", "state", "goal")
        second = self.add("用户决定不考研了", "state", "goal")
        info = self.mem.explain(first["memory"]["id"])
        self.assertEqual(info["current"]["status"], "superseded")
        self.assertIn(second["memory"]["id"], info["state_reason"])
        self.assertEqual(info["related"]["superseded_by"]["id"], second["memory"]["id"])
        self.assertTrue(info["timeline"])
        self.assertEqual(info["timeline"][-1]["action"], "supersede")


class TestUpdateVsMerge(Base):
    def test_update_changes_wording_of_same_fact(self):
        out = self.add("用户偏好 TypeScript", "profile", "preference")
        mid = out["memory"]["id"]
        updated = self.mem.update(mid, content="用户偏好 TypeScript，不喜欢用 any",
                                  reason="补充细节")
        self.assertEqual(updated["id"], mid)          # 同一条
        self.assertIn("any", updated["content"])
        info = self.mem.explain(mid)
        self.assertEqual(len(info["versions"]), 1)
        self.assertEqual(info["versions"][0]["from"], "用户偏好 TypeScript")
        self.assertEqual(self.mem.stats()["total"], 1)  # 没有新增条目

    def test_update_rejects_fact_change(self):
        out = self.add("用户偏好 TypeScript", "profile", "preference")
        with self.assertRaises(CandidateError):
            self.mem.update(out["memory"]["id"], type="state")

    def test_merge_collapses_multiple_into_one(self):
        a = self.add("用户喜欢简洁的回答", "profile", "preference")["memory"]
        b = self.add("用户不喜欢冗长的铺垫", "profile", "preference")["memory"]
        c = self.add("用户希望回答直接给结论", "profile", "preference")["memory"]
        self.assertEqual({m["status"] for m in self.mem.list()}, {"active"})

        merged = self.mem.merge([a["id"], b["id"], c["id"]],
                                content="用户偏好简洁直接的表达：先给结论，不要冗长铺垫")
        self.assertEqual(merged["status"], "active")
        self.assertEqual(merged["metadata"]["merged_from"], [a["id"], b["id"], c["id"]])
        for mid in (a["id"], b["id"], c["id"]):
            self.assertEqual(self.mem.get(mid)["status"], "archived")  # 归档，不是删除
        active = self.mem.list()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], merged["id"])

    def test_merge_requires_same_slot(self):
        a = self.add("用户喜欢简洁的回答", "profile", "preference")["memory"]
        b = self.add("用户在做传热学作业", "state", "activity")["memory"]
        with self.assertRaises(InvariantError):
            self.mem.merge([a["id"], b["id"]], content="混在一起的记忆")

    def test_merge_requires_two(self):
        a = self.add("用户喜欢简洁的回答", "profile", "preference")["memory"]
        with self.assertRaises(CandidateError):
            self.mem.merge([a["id"]], content="只有一条")


class TestUpdateEdgeCases(Base):
    """面板保存路径上踩到的两个真 bug（v1.1 回归）。"""

    def test_update_accepts_subtype_within_same_type(self):
        mid = self.mem.add("用户叫张三，正在备考。", "profile", "identity")["memory"]["id"]
        out = self.mem.update(mid, subtype="preference")
        self.assertEqual(out["subtype"], "preference")

    def test_update_rejects_subtype_of_another_type(self):
        mid = self.mem.add("用户叫张三，正在备考。", "profile", "identity")["memory"]["id"]
        with self.assertRaises(CandidateError) as ctx:
            self.mem.update(mid, subtype="goal")  # goal 是 state 的子类型
        self.assertIn("subtype", str(ctx.exception))

    def test_update_accepts_keywords_as_list(self):
        # 面板把关键词拆成数组传上来；sqlite3 不会替我们序列化 list
        mid = self.mem.add("用户叫张三，正在备考。", "profile", "identity")["memory"]["id"]
        out = self.mem.update(mid, keywords=["张三", "备考", "2027"])
        self.assertEqual(out["keywords"].split(), ["张三", "备考", "2027"])

    def test_update_still_rejects_type_change(self):
        mid = self.mem.add("用户叫张三，正在备考。", "profile", "identity")["memory"]["id"]
        with self.assertRaises(CandidateError) as ctx:
            self.mem.update(mid, type="state")
        self.assertIn("type", str(ctx.exception))


class TestTypeAndPolicyGuards(Base):
    """W1 类型判据进不变量 + W6 always 白名单（三方审查）。"""

    def test_valid_until_only_on_state(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("用户偏好中文回答", "profile", "interaction",
                     valid_until="2027-01-01T00:00:00Z")
        self.assertIn("state", str(ctx.exception))

    def test_state_accepts_valid_until(self):
        out = self.add("用户备考到 2027 年 3 月", "state", "goal",
                       valid_until="2027-03-01T00:00:00Z", confidence=0.9)
        self.assertEqual(out["memory"]["valid_until"], "2027-03-01T00:00:00Z")

    def test_always_rejects_preference(self):
        # 偏好"喜欢红色"是领域/话题相关的，不是每轮都需要
        with self.assertRaises(CandidateError) as ctx:
            self.add("用户喜欢红色", "profile", "preference", injection_policy="always")
        self.assertIn("always", str(ctx.exception))

    def test_always_rejects_non_global_scope(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("讲数学题时先讲思路再给步骤", "profile", "interaction",
                     injection_policy="always", scope="domain", domain_key="study.math")
        self.assertIn("scope", str(ctx.exception))

    def test_always_accepts_global_interaction(self):
        out = self.add("用户要求先讲思路再给步骤", "profile", "interaction",
                       injection_policy="always", confidence=0.9)
        self.assertEqual(out["memory"]["injection_policy"], "always")

    def test_type_hint_flags_state_written_as_profile(self):
        from attune_memory import write as write_mod
        cand = write_mod.Candidate(content="用户正在备考 2027 考研", type="profile",
                                   subtype="identity", confidence=0.9)
        self.assertIn("state", write_mod.type_hint(cand))


    def test_type_hint_is_recorded_in_metadata_and_counter(self):
        out = self.add("用户正在备考 2027 考研", "profile", "identity", confidence=0.9)
        self.assertIn("state", out["memory"]["metadata"]["type_hint"])
        self.assertEqual(self.mem.telemetry()["counters"].get("type_hint_suspected"), 1)
    def test_type_hint_silent_when_consistent(self):
        from attune_memory import write as write_mod
        cand = write_mod.Candidate(content="用户叫张三", type="profile",
                                   subtype="identity", confidence=0.9)
        self.assertIsNone(write_mod.type_hint(cand))


class TestLifecycle(Base):
    def test_archive_restore_delete_purge(self):
        mid = self.add("用户以前用 Python 写脚本", "profile", "skill")["memory"]["id"]

        self.mem.archive(mid, reason="不再适用")
        self.assertEqual(self.mem.get(mid)["status"], "archived")
        self.assertEqual(self.mem.search("Python"), [])  # 归档后不再被检索

        self.mem.restore(mid)
        self.assertEqual(self.mem.get(mid)["status"], "active")

        self.mem.forget(mid)
        self.assertEqual(self.mem.get(mid)["status"], "deleted")

        self.mem.purge(mid, reason="彻底删除")
        self.assertIsNone(self.mem.get(mid))

    def test_purge_requires_delete_first(self):
        mid = self.add("用户喜欢喝美式咖啡", "profile", "preference")["memory"]["id"]
        with self.assertRaises(InvariantError) as ctx:
            self.mem.purge(mid)
        self.assertIn("deleted", str(ctx.exception))

    def test_archived_tick_only_touches_expired_state(self):
        m = self.add("用户正在写毕业论文", "state", "activity",
                     valid_from="2026-01-01T00:00:00Z",
                     valid_until="2026-02-01T00:00:00Z")["memory"]
        keep = self.add("用户偏好 TypeScript", "profile", "preference")["memory"]
        out = self.mem.tick(now="2026-11-13T00:00:00Z")
        self.assertIn(m["id"], out["archived"])
        self.assertEqual(self.mem.get(keep["id"])["status"], "active")


class TestRead(Base):
    def setUp(self) -> None:
        super().setUp()
        self.add("用户希望被称为老张", "profile", "identity",
                 injection_policy="always", importance="key", confidence=0.95)
        self.add("用户偏好中文回答", "profile", "interaction",
                 injection_policy="always", importance="key", confidence=0.9)
        self.add("用户主要备考数学一和英语一", "profile", "identity",
                 domain_key="study", scope="domain", confidence=0.9)
        self.add("用户的传热学基础较弱，讲题要先补物理图像", "domain", "fact",
                 domain_key="study.heat", scope="domain", confidence=0.85)
        self.add("2026-11-13 排查 asar 事件钩子时踩了 session/event 的坑",
                 "episodic", "lesson", injection_policy="on_demand", confidence=0.9)

    def test_stable_block_only_contains_always_and_high_confidence(self):
        block = self.mem.read_for_context("今天写个文案")["stable"]
        self.assertIn("老张", block["text"])
        self.assertNotIn("数学一", block["text"])   # contextual 不该进稳定层
        self.assertNotIn("传热学", block["text"])
        self.assertLessEqual(len(block["ids"]), const.BUDGET["stable_max_items"])

    def test_stable_block_has_no_timestamps(self):
        text1 = self.mem.read_for_context("随便")["stable"]["text"]
        self.assertNotRegex(text1, r"\d{4}-\d{2}-\d{2}")

    def test_context_excludes_stable_ids(self):
        out = self.mem.read_for_context("写文案", domain="writing")
        self.assertFalse(set(out["stable"]["ids"]) & set(out["context"]["ids"]))

    def test_context_is_domain_aware(self):
        heat = self.mem.read_for_context("传热学这道题怎么补基础", domain="study.heat")["context"]
        self.assertIn("传热学", heat["text"])
        writing = self.mem.read_for_context("帮我写个产品文案", domain="writing")["context"]
        self.assertNotIn("传热学", writing["text"])

    def test_never_policy_never_injected(self):
        self.add("用户的花生过敏属于隐私，保存但不主动注入", "profile", "constraint",
                 injection_policy="never", importance="key", confidence=0.99)
        out = self.mem.read_for_context("花生过敏能吃吗")
        self.assertNotIn("花生", out["stable"]["text"])
        self.assertNotIn("花生", out["context"]["text"])

    def test_contextual_profile_reaches_context_channel(self):
        """进不了 C 通道的 profile（太长/置信度不够）必须在 T 通道里补出来。

        P8 真机验证时发现的：这类条目在 search 里有分，却因为分组只认 state/domain/episodic
        而永远注入不出去 —— 检索"看起来正常"，注入端一片空白。
        """
        self.add("用户做数学一极限题时总是想不到变形方向，属于结构性判断缺失，"
                 "讲题要先把未定式类型和最低阶讲清楚", "profile", "skill",
                 injection_policy="contextual", importance="important", confidence=0.8)
        out = self.mem.read_for_context("我极限题总是卡在变形方向", domain="study.math")["context"]
        self.assertIn("极限", out["text"])
        self.assertIn("profile", out["groups"])

    def test_on_demand_not_auto_injected(self):
        out = self.mem.read_for_context("asar 事件钩子")
        self.assertNotIn("asar", out["context"]["text"])
        hits = self.mem.search("asar 事件钩子", policies=("on_demand",), include_never=False)
        self.assertTrue(hits)

    def test_context_budget(self):
        ctx = self.mem.read_for_context("数学一和传热学怎么安排")["context"]
        self.assertLessEqual(len(ctx["text"]), const.BUDGET["total_max_chars"] + 120)
        self.assertLessEqual(len(ctx["ids"]), 12)


class TestSearch(Base):
    def test_relevance_prefers_keyword_hit(self):
        self.add("用户主要备考数学一", "profile", "identity", domain_key="study",
                 scope="domain", importance="important", confidence=0.9)
        hits = self.mem.search("数学一", domain="study.math")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["matched_terms"][0], "数学一")
        self.assertGreater(hits[0]["score"], const.MIN_SCORE)

    def test_no_hit_returns_empty(self):
        self.add("用户主要备考数学一", "profile", "identity", domain_key="study",
                 scope="domain")
        self.assertEqual(self.mem.search("完全无关的量子色动力学"), [])

    def test_expired_state_is_penalised_and_flagged(self):
        self.add("用户正在做毕设", "state", "activity",
                 valid_from="2026-01-01T00:00:00Z", valid_until="2026-03-01T00:00:00Z",
                 importance="important")
        hits = self.mem.search("毕设", include_expired=True, now="2026-11-13T00:00:00Z")
        self.assertTrue(hits)
        self.assertIsNotNone(hits[0]["note"])
        self.assertLessEqual(hits[0]["factors"]["conflict"], 0.5)

    def test_top_k_is_bounded(self):
        for i in range(10):
            self.add(f"用户第 {i} 条数学一相关的偏好记录", "profile", "preference",
                     domain_key="study", scope="domain")
        self.assertLessEqual(len(self.mem.search("数学一")), const.K_MAX)
        self.assertLessEqual(len(self.mem.search("数学一", limit=99)), const.K_HARD_MAX)

    def test_find_similar_relations(self):
        self.add("用户在做传热学作业", "state", "activity")
        eq = self.mem.find_similar("用户在做传热学作业", type="state", subtype="activity")
        self.assertEqual(eq[0]["relation"], "equivalent")

    def test_search_records_usage(self):
        m = self.add("用户主要备考数学一", "profile", "identity", domain_key="study",
                     scope="domain", importance="important")["memory"]
        self.mem.search("数学一", domain="study.math")
        self.assertEqual(self.mem.get(m["id"])["use_count"], 1)


class TestCandidatePreview(Base):
    """W2：候选与落库拆开——preview 只算不写，且结论必须与 add 一致。"""

    def test_preview_does_not_write(self):
        out = self.mem.preview("用户叫张三", "profile", "identity")
        self.assertEqual(out["would"], "add")
        self.assertEqual(self.mem.count(), 0, "预览不落库")
        self.assertEqual(self.mem.telemetry()["counters"], {}, "预览不写计数")

    def test_preview_matches_add_for_new_fact(self):
        pv = self.mem.preview("用户叫张三", "profile", "identity")
        add = self.mem.add("用户叫张三", "profile", "identity")
        self.assertEqual(pv["would"], add["action"])

    def test_preview_predicts_duplicate_as_touch(self):
        self.mem.add("用户喜欢喝茶", "profile", "preference")
        pv = self.mem.preview("用户喜欢喝茶", "profile", "preference")
        self.assertEqual(pv["would"], "touch")
        self.assertEqual(len(pv["duplicates"]), 1)
        self.assertEqual(self.mem.count(), 1, "仍然只有一条")

    def test_preview_predicts_supersede(self):
        # 用明确的互斥对（需要 / 不需要）；"备考 → 放弃备考"这种改写不在极性词表里，
        # 属于 V2 的冲突发现范围（见 V2_V3_RESERVED.md）。
        self.mem.add("用户需要每天早上复习三小时", "state", "activity", confidence=0.95)
        pv = self.mem.preview("用户不需要每天早上复习三小时", "state", "activity", confidence=0.9)
        self.assertEqual(pv["would"], "supersede")
        self.assertEqual(len(pv["conflicts"]), 1)
        self.assertEqual(self.mem.add("用户不需要每天早上复习三小时", "state", "activity",
                                      confidence=0.9)["action"], "supersede")

    def test_preview_predicts_gate_reject(self):
        pv = self.mem.preview("今天天气不错", "episodic", "event")
        self.assertEqual(pv["would"], "ignore")
        self.assertEqual(self.mem.discarded(limit=5), [], "预览也不写丢弃日志")


class TestBigramRecall(Base):
    """R2：中文跨词查询靠二元组兜底，英文不能被拆碎。"""

    def test_cross_word_query_hits(self):
        self.add("用户在北京工作", "domain", "fact", domain_key="career", confidence=0.9)
        rows = self.mem.search("北京市有哪些工作机会", domain="career")
        self.assertTrue(rows, "用北京查北京市应该命中")

    def test_unrelated_query_stays_empty(self):
        self.add("用户喜欢喝咖啡", "profile", "preference")
        self.assertEqual(self.mem.search("完全无关的编程问题"), [])

    def test_latin_words_are_not_split(self):
        self.add("project uses sqlite wal mode", "domain", "fact", domain_key="code")
        self.assertTrue(self.mem.search("sqlite wal", domain="code"))
        self.assertEqual(self.mem.search("sq"), [])


class TestMaintenanceScan(Base):
    """M1：扫描只产出清单，不改数据。"""

    def test_scan_reports_duplicate_and_conflict(self):
        # 同内容不同 domain → 不同槽位，因此不会被 touch，两条都 active
        self.add("用户在做毕业设计", "domain", "fact", domain_key="study")
        self.add("用户在做毕业设计", "domain", "fact", domain_key="work")
        # 同槽位互斥但置信度落在"保留区间"（0.6–0.8）→ 两条并存
        self.add("用户需要每周汇报", "profile", "interaction", confidence=0.75)
        self.add("用户不需要每周汇报", "profile", "interaction", confidence=0.75)
        report = self.mem.scan()
        self.assertGreaterEqual(len(report["duplicates"]), 1)
        self.assertGreaterEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["problems"], [])

    def test_scan_is_read_only(self):
        mid = self.add("用户叫张三", "profile", "identity")["memory"]["id"]
        before = self.mem.get(mid)
        self.mem.scan()
        after = self.mem.get(mid)
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(before["content"], after["content"])

    def test_scan_groups_related_entries(self):
        for content in ("用户备考数学一", "用户每天复习数学一两小时", "用户讨厌数学一的极限题"):
            self.add(content, "state", "goal", confidence=0.9)
        self.add("用户喜欢喝咖啡", "profile", "preference")
        report = self.mem.scan()
        self.assertTrue(report["formations"])
        self.assertEqual(report["formations"][0]["size"], 3)

    def test_scan_flags_multi_active_state(self):
        self.add("用户正在备考数学一", "state", "goal", confidence=0.9)
        self.add("用户正在备考英语一", "state", "goal", confidence=0.9)
        report = self.mem.scan()
        self.assertGreaterEqual(len(report["multi_active"]), 1)


class TestSubjectConservatism(Base):
    """W4：第一人称与第三方两侧都拦住，不确定就不写。"""

    def test_first_person_must_be_user(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("我叫张三，做后端开发", "profile", "identity",
                     subject_type="person", subject_name="张三")
        self.assertIn("第一人称", str(ctx.exception))

    def test_third_party_must_not_be_user(self):
        with self.assertRaises(CandidateError):
            self.add("我朋友小王喜欢红色", "profile", "preference")

    def test_third_party_without_name_is_rejected(self):
        with self.assertRaises(CandidateError) as ctx:
            self.add("我朋友喜欢红色", "profile", "preference", subject_type="person")
        self.assertIn("subject_name", str(ctx.exception))

    def test_named_third_party_is_accepted(self):
        out = self.add("小王喜欢红色", "profile", "preference",
                       subject_type="person", subject_name="小王",
                       injection_policy="on_demand")
        self.assertEqual(out["memory"]["subject_type"], "person")


class TestKeyValuePrecision(Base):
    """W7：只有"同一属性不同取值"才算冲突，数值不同不等于事实矛盾。"""

    def test_different_attributes_are_not_conflict(self):
        a = self.add("用户每天学习三小时", "state", "activity", confidence=0.9)
        b = self.add("用户数学考了 120 分", "state", "activity", confidence=0.9)
        self.assertEqual(b["action"], "add", "不同属性不该被自动取代")

    def test_same_attribute_different_value_is_conflict(self):
        self.add("用户端口是 8080", "domain", "fact", confidence=0.9)
        out = self.add("用户端口是 9000", "domain", "fact", confidence=0.9)
        self.assertEqual(out["action"], "supersede")

    def test_same_attribute_same_value_is_not_conflict(self):
        self.add("用户端口是 8080", "domain", "fact", confidence=0.9)
        out = self.add("用户端口是 8080", "domain", "fact", confidence=0.9)
        self.assertIn(out["action"], ("touch", "add"))
        self.assertNotEqual(out["action"], "supersede")


class TestSearchStages(Base):
    """R4：检索拆成 资格 → 排序 → 预算，debug 能说清每段发生了什么。"""

    def setUp(self) -> None:
        super().setUp()
        self.add("用户备考数学一，做极限题常卡在变形方向", "state", "goal",
                 domain_key="study.math", scope="domain", confidence=0.9, importance="key")
        self.add("用户喜欢用 Rust 写小工具", "profile", "preference", confidence=0.9)
        self.add("用户讨厌味精", "profile", "preference", confidence=0.9)

    def test_debug_reports_each_stage(self):
        dbg = {}
        hits = self.mem.search("极限题", debug=dbg)
        self.assertTrue(hits, "query should hit the math memory")
        self.assertEqual(dbg["stages"]["candidates"], 3, "结构化过滤后剩 3 条")
        self.assertLessEqual(dbg["stages"]["eligible"], 3, "资格段只会做减法")
        self.assertEqual(dbg["stages"]["selected"], len(hits), "预算段的结果就是返回值")

    def test_gate_drops_only_after_ranking(self):
        # 一条零命中条目的 relevance=0，资格段就该没有它；排序段不该出现"被删"
        dbg = {}
        self.mem.search("完全无关的词", debug=dbg)
        self.assertEqual(dbg["stages"]["eligible"], 0, "零命中在资格段被挡")
        self.assertEqual(dbg["stages"]["ranked"], 0)

    def test_limit_applies_in_budget_stage(self):
        dbg = {}
        hits = self.mem.search("用户", limit=1, debug=dbg)
        self.assertLessEqual(len(hits), 1)
        self.assertGreaterEqual(dbg["stages"]["gate_passed"], len(hits))


class TestRpc(Base):
    def test_roundtrip(self):
        resp = rpc.handle(self.mem, {"id": 1, "op": "add", "args": {
            "content": "用户偏好中文回答", "type": "profile", "subtype": "interaction",
            "injection_policy": "always", "importance": "key",
        }})
        self.assertTrue(resp["ok"], resp)
        mid = resp["result"]["memory"]["id"]

        got = rpc.handle(self.mem, {"id": 2, "op": "get", "args": {"id": mid}})
        self.assertEqual(got["result"]["content"], "用户偏好中文回答")

        bad = rpc.handle(self.mem, {"id": 3, "op": "add", "args": {
            "content": "内容", "type": "profile", "subtype": "nope"}})
        self.assertFalse(bad["ok"])
        self.assertIn("合法值", bad["error"])

    def test_unknown_arg_is_rejected(self):
        resp = rpc.handle(self.mem, {"id": 1, "op": "search",
                                     "args": {"query": "x", "nonsense": 1}})
        self.assertFalse(resp["ok"])
        self.assertIn("nonsense", resp["error"])

    def test_explain_over_rpc(self):
        mid = self.add("用户偏好 TypeScript", "profile", "preference")["memory"]["id"]
        resp = rpc.handle(self.mem, {"id": 9, "op": "explain", "args": {"id": mid}})
        self.assertTrue(resp["ok"])
        self.assertIn("state_reason", resp["result"])


class TestSupersedeConfidence(Base):
    """模块 5：冲突不再一律自动取代，按"谁说的 + 多确定"分三档。"""

    def _seed(self, content="用户不考数学一了，改考计算机 408。"):
        return self.mem.add("用户正在备考考研数学一。", "state", "goal", confidence=0.95)["memory"]["id"]

    def test_explicit_high_confidence_supersedes(self):
        old_id = self._seed()
        out = self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal", confidence=0.9)
        self.assertEqual(out["action"], "supersede")
        self.assertEqual(self.mem.get(old_id)["status"], "superseded")
        self.assertEqual(self.mem.get(old_id)["superseded_by"], out["memory"]["id"])

    def test_explicit_mid_confidence_keeps_both(self):
        old_id = self._seed()
        out = self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal", confidence=0.7)
        self.assertEqual(out["action"], "add")
        self.assertEqual(self.mem.get(old_id)["status"], "active")
        self.assertEqual(out["memory"]["metadata"]["conflict_with"], old_id)
        self.assertIn("并存", out["reason"])

    def test_explicit_low_confidence_is_dropped(self):
        old_id = self._seed()
        out = self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal", confidence=0.3)
        self.assertEqual(out["action"], "ignore")
        self.assertEqual(self.mem.get(old_id)["status"], "active")
        self.assertEqual(self.mem.list(status=None, limit=10).__len__(), 1)
        rows = self.mem.discarded(limit=1)
        self.assertIn("置信度", rows[0]["reason"])

    def test_inferred_never_supersedes(self):
        old_id = self._seed()
        out = self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal",
                           confidence=0.9, explicit=False)
        self.assertNotEqual(out["action"], "supersede")
        self.assertEqual(self.mem.get(old_id)["status"], "active")

    def test_explicit_supersede_id_still_wins(self):
        old_id = self._seed()
        # 显式指定取代时，即使置信度低也照做（调用方明确要求）
        out = self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal",
                           confidence=0.5, supersede_id=old_id)
        self.assertEqual(out["action"], "supersede")

    def test_no_conflict_just_adds(self):
        self._seed()
        out = self.mem.add("用户每天复习两小时。", "state", "goal", confidence=0.9)
        self.assertEqual(out["action"], "add")

    def test_conflict_counter_moves_only_on_drop(self):
        self._seed()
        self.mem.add("用户不考数学一了，改考计算机 408。", "state", "goal", confidence=0.3)
        counters = self.mem.telemetry()["counters"]
        self.assertEqual(counters.get("supersede_low_confidence"), 1)


class TestObservability(Base):
    """模块 6/7/8：计数、读日志、丢弃日志。"""

    def test_write_and_search_counters(self):
        self.mem.add("用户在做数学一的极限题。", "state", "goal")
        counters = self.mem.telemetry()["counters"]
        self.assertEqual(counters.get("candidates_total"), 1)
        self.assertEqual(counters.get("op_add"), 1)
        # 注意：V1 是纯词法检索，"数学一极限"这种没有停用字分隔的连写串切不出来，
        # 必须用带分隔的写法（"的"是切分点）才能命中。这是已知边界，不是 bug。
        self.assertGreaterEqual(len(self.mem.search("数学一的极限题")), 1)
        counters = self.mem.telemetry()["counters"]
        self.assertGreaterEqual(counters.get("search_candidates", 0), 1)
        self.assertGreaterEqual(counters.get("search_hits", 0), 1)

    def test_discarded_log_keeps_reason(self):
        decision = self.mem.add("今天天气不错，挺适合晒被子的。", "episodic", "event")
        self.assertEqual(decision["action"], "ignore")
        rows = self.mem.discarded(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertIn("晒被子", rows[0]["content"])
        self.assertIn("时效性", rows[0]["reason"])

    def test_read_log_records_process_numbers(self):
        self.mem.add("用户做数学一极限题总卡在变形方向。", "profile", "skill", confidence=0.9)
        out = self.mem.read_for_context("数学一极限题怎么突破？", domain="study.math")
        self.assertIn("candidates", out["debug"])
        self.assertGreaterEqual(out["debug"]["hits"], 1)
        reads = self.mem.reads(limit=1)
        self.assertEqual(len(reads), 1)
        self.assertIn("数学一", reads[0]["query"])
        self.assertIn("candidates", reads[0])

    def test_read_log_is_trimmed(self):
        from attune_memory.read import READ_LOG_KEEP
        self.mem.add("用户做数学一极限题总卡在变形方向。", "profile", "skill", confidence=0.9)
        for i in range(READ_LOG_KEEP + 5):
            self.mem.read_for_context(f"数学一极限题第{i}次", domain="study.math")
        self.assertLessEqual(len(self.mem.reads(limit=1000)), READ_LOG_KEEP)

    def test_discard_log_does_not_grow_without_bound_and_can_be_purged(self):
        self.mem.add("刚吃完午饭，有点困。", "episodic", "event")
        self.assertEqual(len(self.mem.discarded(limit=10)), 1)
        self.assertEqual(self.mem.purge_observations("discarded"), 1)
        self.assertEqual(self.mem.discarded(limit=10), [])


class TestInvariants(Base):
    def test_whole_library_stays_consistent(self):
        self.add("用户希望被称为老张", "profile", "identity",
                 injection_policy="always", importance="key", confidence=0.95)
        self.add("用户正在备考考研", "state", "goal", valid_from="2026-09-01T00:00:00Z")
        self.add("用户决定不考研了", "state", "goal")
        self.add("小王喜欢红烧肉", "profile", "preference",
                 subject_type="person", subject_name="小王", injection_policy="on_demand")
        self.assertEqual(self.mem.store.verify_invariants(), [])

    def test_non_ascii_roundtrip(self):
        row = self.add("用户把「记忆」读作 jìyì，不接受机器翻译的谐音",
                       "profile", "preference")["memory"]
        raw = json.dumps(row, ensure_ascii=False)
        self.assertIn("记忆", raw)
        self.assertEqual(self.mem.get(row["id"])["content"], row["content"])


class TestDomainRules(Base):
    """领域约定补位：词不重合时也要能注入（P10 E 场景暴露的缺口）。"""

    def test_domain_rule_reaches_context_without_keyword_overlap(self):
        self.add("讲数学题时先讲思路再给步骤，不要直接抛公式", "profile", "interaction",
                 scope="domain", domain_key="study.math", confidence=0.9, importance="important")
        ctx = self.mem.read_for_context(query="这道极限题怎么下手", domain="study.math")["context"]
        self.assertIn("先讲思路", ctx["text"])
        self.assertIn("profile", ctx["groups"])

    def test_domain_rule_absent_without_domain(self):
        self.add("讲数学题时先讲思路再给步骤", "profile", "interaction",
                 scope="domain", domain_key="study.math", confidence=0.9)
        ctx = self.mem.read_for_context(query="今天想聊点别的")["context"]
        self.assertNotIn("先讲思路", ctx["text"])


class TestValidUntil(Base):
    def test_expired_state_is_writable(self):
        row = self.add("用户 2026 年上半年的备考计划（已过期）", "state", "situation",
                       valid_from="2025-12-01T00:00:00Z",
                       valid_until="2026-01-01T00:00:00Z")["memory"]
        self.assertEqual(row["valid_until"], "2026-01-01T00:00:00Z")

    def test_bad_valid_until_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self.add("用户备考计划安排", "state", "situation",
                     valid_from="2025-12-01T00:00:00Z", valid_until="明年一月")
        self.assertIn("ISO", str(ctx.exception))

    def test_valid_until_must_follow_valid_from(self):
        with self.assertRaises(Exception) as ctx:
            self.add("用户备考计划安排", "state", "situation",
                     valid_from="2026-05-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z")
        self.assertIn("必须晚于", str(ctx.exception))


class TestTelemetry(Base):
    """模块 9 的地基：计数器只做加法，观测只追加，两者都不改读写行为。"""

    def test_counters_are_additive(self):
        self.mem.record(counters={"inject_expected": 2})
        self.mem.record(counters={"inject_expected": 3, "inject_visible": 1})
        counters = self.mem.metrics()["counters"]
        self.assertEqual(counters["inject_expected"], 5)
        self.assertEqual(counters["inject_visible"], 1)

    def test_observation_kept_and_purgeable(self):
        self.mem.record(observation={"kind": "inject-faded", "subject": "m-x", "detail": "no-compaction-observed"})
        observations = self.mem.metrics()["observations"]
        self.assertEqual(observations[0]["kind"], "inject-faded")
        self.assertEqual(observations[0]["detail"], "no-compaction-observed")
        self.assertEqual(self.mem.purge_observations(), 1)
        self.assertEqual(self.mem.metrics()["observations"], [])

    def test_mark_injected_bumps_inject_count(self):
        mid = self.mem.add("用户做数学一极限题时常卡在变形方向。", "profile", "skill", confidence=0.9)["memory"]["id"]
        self.assertEqual(self.mem.get(mid)["inject_count"], 0)
        self.assertEqual(self.mem.mark_injected([mid, "m-missing"]), 1)
        self.assertEqual(self.mem.get(mid)["inject_count"], 1)
        self.mem.record(counters={"inject_turns": 1}, injected_ids=[mid])
        self.assertEqual(self.mem.get(mid)["inject_count"], 2)

    def test_telemetry_does_not_touch_memories(self):
        before = self.mem.stats()["total"]
        self.mem.record(counters={"inject_turns": 1}, observation={"kind": "inject-visible"})
        self.assertEqual(self.mem.stats()["total"], before)


class TestTextHelpers(unittest.TestCase):
    def test_segments_splits_on_function_words(self):
        self.assertEqual(text.segments("数学一的复习安排"), ["数学一", "复习安排"])
        self.assertEqual(text.segments("传热学怎么补"), ["传热学"])

    def test_auto_keywords_never_empty_for_real_sentence(self):
        self.assertTrue(text.auto_keywords("用户主要备考数学一和英语一"))

    def test_normalize_is_nfkc_and_lowercase(self):
        self.assertEqual(text.normalize("ＰＹＴＨＯＮ！"), "python")


if __name__ == "__main__":
    unittest.main(verbosity=2)

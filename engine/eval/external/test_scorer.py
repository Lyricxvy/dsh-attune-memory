#!/usr/bin/env python3
"""判分器校准 + 变异测试：先证明"判分器会红"，再相信它的绿。

这是整个外部评价体系里唯一由我们自己写的判定逻辑，所以它必须被当成被测对象：
  * 已知答案的输入 → 必须得到已知分数（校准）；
  * 故意给全对/全错/随机/重复的预测 → 分数必须动（变异）；
  * 缺 case / 引用不存在的单元 → 必须报错退出，而不是"没命中就算 0 分"。

用法：cd engine && python -m unittest eval.external.test_scorer -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import score


class TestBasicMetrics(unittest.TestCase):
    def test_perfect(self):
        self.assertEqual(score.recall_at(["a", "b"], ["a", "b"], 5), 1.0)
        self.assertEqual(score.hit_at(["a"], ["a"], 1), 1.0)

    def test_partial(self):
        self.assertAlmostEqual(score.recall_at(["a"], ["a", "b"], 5), 0.5)
        self.assertAlmostEqual(score.recall_at(["a", "b"], ["a", "b", "c", "d"], 2), 0.5)

    def test_zero(self):
        self.assertEqual(score.recall_at(["x", "y"], ["a"], 2), 0.0)
        self.assertEqual(score.hit_at(["x"], ["a"], 50), 0.0)

    def test_k_truncation(self):
        # a 排第 3，k=2 时不应该算命中
        self.assertEqual(score.recall_at(["x", "y", "a"], ["a"], 2), 0.0)
        self.assertEqual(score.recall_at(["x", "y", "a"], ["a"], 3), 1.0)

    def test_duplicates_do_not_inflate(self):
        # 同一个单元重复 10 次，只能算 1 个
        self.assertEqual(score.recall_at(["a"] * 10, ["a", "b"], 10), 0.5)

    def test_no_gold_is_nan(self):
        v = score.recall_at(["a"], [], 5)
        self.assertNotEqual(v, v)

    def test_random_expectation(self):
        self.assertAlmostEqual(score.random_recall(5, 100), 0.05)
        self.assertAlmostEqual(score.random_recall(200, 100), 1.0)


class TestAggregate(unittest.TestCase):
    def test_macro_average_and_categories(self):
        ks = [1, 5]
        s1 = score.score_case("c1", "catA", ["a"], ["a", "b"], 10, ks)
        s2 = score.score_case("c2", "catA", ["x"], ["a", "b"], 10, ks)
        s3 = score.score_case("c3", "catB", ["a", "b"], ["a", "b"], 10, ks)
        agg = score.aggregate([s1, s2, s3], ks)
        self.assertEqual(agg["overall"]["recall"]["5"], round((0.5 + 0.0 + 1.0) / 3, 4))
        # c3 的 gold 有两个单元，k=1 只覆盖一个 → 0.5
        self.assertEqual(agg["by_category"]["catB"]["recall"]["1"], 0.5)
        self.assertEqual(agg["by_category"]["catB"]["recall"]["5"], 1.0)
        self.assertEqual(agg["by_category"]["catA"]["n_cases"], 2)


def _write(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh, ensure_ascii=False)


class TestEndToEndScorer(unittest.TestCase):
    """用一份假数据集 + 三种预测，验证 CLI 层面的行为（能红也能绿）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # 假 LoCoMo：一个会话 10 轮，2 道题
        conv = {
            "sample_id": "conv-1",
            "conversation": {"session_1": [
                {"speaker": "A", "dia_id": f"D1:{i}", "text": f"turn number {i} about topic {i}"}
                for i in range(1, 11)
            ], "session_1_date_time": "1 Jan 2023"},
            "qa": [
                {"question": "q1", "answer": "a1", "evidence": ["D1:1"], "category": 4},
                {"question": "q2", "answer": "a2", "evidence": ["D1:2", "D1:3"], "category": 2},
            ],
        }
        self.data = os.path.join(self.tmp, "locomo.json")
        _write(self.data, [conv])

    def _predictions(self, name: str, ranked_by_case: dict) -> str:
        p = os.path.join(self.tmp, name)
        lines = [json.dumps({"case_id": cid, "query": "q", "ranked": r}, ensure_ascii=False)
                 for cid, r in ranked_by_case.items()]
        _write(p, "\n".join(lines) + "\n")
        return p

    def _run(self, pred: str) -> dict:
        out = os.path.join(self.tmp, "report.json")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = score.main(["--bench", "locomo", "--data", self.data,
                             "--predictions", pred, "--ks", "5", "--json-out", out])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)

    def test_oracle_scores_one(self):
        pred = self._predictions("oracle.jsonl", {
            "conv-1#0": ["D1:1"], "conv-1#1": ["D1:2", "D1:3"]})
        rep = self._run(pred)
        self.assertEqual(rep["overall"]["recall"]["5"], 1.0)

    def test_wrong_scores_zero(self):
        pred = self._predictions("wrong.jsonl", {
            "conv-1#0": ["D1:9"], "conv-1#1": ["D1:8"]})
        rep = self._run(pred)
        self.assertEqual(rep["overall"]["recall"]["5"], 0.0)

    def test_missing_case_fails_loudly(self):
        pred = self._predictions("missing.jsonl", {"conv-1#0": ["D1:1"]})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = score.main(["--bench", "locomo", "--data", self.data,
                             "--predictions", pred, "--ks", "5"])
        self.assertEqual(rc, 2)

    def test_unknown_unit_fails_loudly(self):
        pred = self._predictions("unknown.jsonl", {
            "conv-1#0": ["D9:9"], "conv-1#1": ["D1:2"]})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = score.main(["--bench", "locomo", "--data", self.data,
                             "--predictions", pred, "--ks", "5"])
        self.assertEqual(rc, 2)


class TestMutationSelfCheck(unittest.TestCase):
    """变异测试：换一个明显不同的"检索器"，分数必须明显不同。

    这一条是给外部审计看的：它能证明判分器不是在"总是返回漂亮数字"。
    """

    def _score_for(self, ranker) -> float:
        import random
        rng = random.Random(7)
        units = [f"u{i}" for i in range(100)]
        gold = units[:2]
        vals = []
        for _ in range(200):
            ranked = ranker(units, gold, rng)
            vals.append(score.recall_at(ranked, gold, 5))
        return sum(vals) / len(vals)

    def test_random_ranker_matches_expectation(self):
        got = self._score_for(lambda units, gold, rng: rng.sample(units, 5))
        self.assertAlmostEqual(got, 0.05, delta=0.02)      # k/n = 5/100

    def test_oracle_ranker_scores_one(self):
        got = self._score_for(lambda units, gold, rng: gold + rng.sample(units, 5))
        self.assertEqual(got, 1.0)

    def test_reverse_oracle_scores_zero(self):
        got = self._score_for(lambda units, gold, rng: [u for u in units if u not in gold][:5])
        self.assertEqual(got, 0.0)


if __name__ == "__main__":
    unittest.main()

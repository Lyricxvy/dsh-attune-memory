"""对照工具的自测（W-0.2）：用两份人造 run 目录验证判定逻辑。

不碰真实数据集——真实那份要跑几小时，测试只需要"两份运行的形状"。
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from eval.external import compare


def _case(case_id: str, gold: list[str]):
    class C:
        pass

    c = C()
    c.case_id = case_id
    c.category = "single-session"
    c.gold = gold
    c.unit_ids = ["u1", "u2", "u3", "u4", "u5"]
    c.query = "q"
    return c


class CompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_run(self, name: str, preds: dict[str, list[str]], *, engine: str = "e1",
                 case_ids: list[str] | None = None) -> str:
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "run.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "bench": "longmemeval",
                "data_sha256": "abc",
                "n_cases": len(case_ids or list(preds)),
                "case_ids": case_ids or list(preds),
                "n_requested": len(case_ids or list(preds)),
                "haystack": 0,
                "k": 20,
                "keywords_mode": "bow",
                "engine_sha256": engine,
            }, fh)
        with open(os.path.join(d, "predictions.jsonl"), "w", encoding="utf-8") as fh:
            for cid, ranked in preds.items():
                fh.write(json.dumps({"case_id": cid, "ranked": ranked}, ensure_ascii=False) + "\n")
        return d

    def run_compare(self, base: str, new: str) -> tuple[int, str]:
        cases = [_case("c1", ["u1"]), _case("c2", ["u2"])]
        old_load = compare.load_cases
        compare.load_cases = lambda *a, **k: cases
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = compare.main([
                    "--base", base, "--new", new,
                    "--bench", "longmemeval", "--data", "unused", "--ks", "5",
                ])
            return code, buf.getvalue()
        finally:
            compare.load_cases = old_load

    def test_incompatible_runs_refused(self) -> None:
        base = self.make_run("b", {"c1": ["u1"], "c2": ["u2"]})
        new = self.make_run("n", {"c1": ["u1"], "c2": ["u2"]})
        with open(os.path.join(new, "run.json"), encoding="utf-8") as fh:
            run = json.load(fh)
        run["data_sha256"] = "different"
        with open(os.path.join(new, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(run, fh)
        code, out = self.run_compare(base, new)
        self.assertEqual(code, 2)
        self.assertIn("不可比", out)

    def test_missing_predictions_block_verdict(self) -> None:
        base = self.make_run("b", {"c1": ["u1"]})
        new = self.make_run("n", {"c1": ["u1"]})
        code, out = self.run_compare(base, new)
        self.assertEqual(code, 3)
        self.assertIn("不做劣化判定", out)

    def test_degradation_detected(self) -> None:
        # 判定门要求至少 5 题（样本太少时只报"不足"，不报"劣化"），
        # 所以这里造 6 题，才能走到劣化分支。
        ids = [f"c{i}" for i in range(1, 7)]
        base = self.make_run("b", {cid: ["u1"] for cid in ids})
        new = self.make_run("n", {cid: ["u5"] for cid in ids})
        cases = [_case(cid, ["u1"]) for cid in ids]
        old_load = compare.load_cases
        compare.load_cases = lambda *a, **k: cases
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                compare.main(["--base", base, "--new", new, "--bench", "longmemeval",
                              "--data", "unused", "--ks", "5"])
            out = buf.getvalue()
        finally:
            compare.load_cases = old_load
        self.assertIn("新版存在劣化", out)
        self.assertIn("变差的题", out)


if __name__ == "__main__":
    unittest.main()

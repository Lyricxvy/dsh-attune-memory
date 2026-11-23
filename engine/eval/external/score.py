#!/usr/bin/env python3
"""确定性判分器（无 LLM judge）。

职责边界（这是"防自证"的关键）：
  * 输入只有两样：predictions.jsonl（模型/系统的排名）+ 外部数据集文件（标准答案）。
  * 本文件不 import 引擎、不读运行配置、不知道是谁跑的。
  * 同一份 predictions 任何人重跑都应得到同一个分数。

指标定义（写死在文档里，跑之前冻结）：
  Recall@k        = |top-k 去重单元 ∩ 标准答案单元| / |标准答案单元|
  Hit@k           = 1 如果 top-k 里至少有一个标准答案单元
  Random Recall@k = k / n（无放回抽样的期望召回，n=该 case 的候选单元数）
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Iterable


# --------------------------------------------------------------------- 基本指标
def dedup_units(ranked: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in ranked:
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def recall_at(ranked: list[str], gold: list[str], k: int) -> float:
    if not gold:
        return float("nan")
    top = set(dedup_units(ranked)[:k])
    return len(top & set(gold)) / len(gold)


def hit_at(ranked: list[str], gold: list[str], k: int) -> float:
    if not gold:
        return float("nan")
    return 1.0 if set(dedup_units(ranked)[:k]) & set(gold) else 0.0


def random_recall(k: int, n_units: int) -> float:
    """无放回抽 k 个单元时，覆盖标准答案比例的期望值 = k/n（截到 1）。"""
    if n_units <= 0:
        return float("nan")
    return min(k, n_units) / n_units


# --------------------------------------------------------------------- 汇总
@dataclass
class CaseScore:
    case_id: str
    category: str
    n_units: int
    n_gold: int
    recall: dict[int, float]
    hit: dict[int, float]


def score_case(case_id: str, category: str, ranked: list[str], gold: list[str],
               n_units: int, ks: list[int]) -> CaseScore:
    return CaseScore(
        case_id=case_id, category=category, n_units=n_units, n_gold=len(gold),
        recall={k: recall_at(ranked, gold, k) for k in ks},
        hit={k: hit_at(ranked, gold, k) for k in ks},
    )


def _mean(vals: list[float]) -> float | None:
    vals = [v for v in vals if v == v]     # 去掉 nan
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(scores: list[CaseScore], ks: list[int]) -> dict:
    by_cat: dict[str, list[CaseScore]] = {}
    for s in scores:
        by_cat.setdefault(s.category, []).append(s)

    def block(group: list[CaseScore]) -> dict:
        return {
            "n_cases": len(group),
            "recall": {str(k): _mean([s.recall[k] for s in group]) for k in ks},
            "hit": {str(k): _mean([s.hit[k] for s in group]) for k in ks},
            "random_recall": {str(k): _mean([random_recall(k, s.n_units) for s in group]) for k in ks},
        }

    return {
        "overall": block(scores),
        "by_category": {c: block(g) for c, g in sorted(by_cat.items())},
        "ks": ks,
    }


# --------------------------------------------------------------------- 载入
def load_predictions(path: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            rows[r["case_id"]] = [u for u in (r.get("ranked") or []) if u]
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="确定性判分：只读 predictions + 数据集")
    ap.add_argument("--bench", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--data", required=True, help="数据集文件路径")
    ap.add_argument("--predictions", required=True, help="predictions.jsonl")
    ap.add_argument("--ks", default="5,10,20")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--n", type=int, default=0, help="仅用于复现子集选择（>=0）")
    ap.add_argument("--haystack", type=int, default=0, help="与 run.py 的 --haystack 保持一致")
    args = ap.parse_args(argv)

    from .benchmarks import load_cases      # 延迟 import：只有读标准答案时才碰数据集

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    cases = load_cases(args.bench, args.data, n=args.n or None,
                       haystack=args.haystack or None)
    preds = load_predictions(args.predictions)

    missing = [c.case_id for c in cases if c.case_id not in preds]
    extra = sorted(set(preds) - {c.case_id for c in cases})
    if missing:
        print(f"[判分失败] 有 {len(missing)} 个 case 没有预测，例如 {missing[:3]}")
        return 2
    if extra:
        print(f"[判分失败] 预测里有 {len(extra)} 个 case 不在数据集里，例如 {extra[:3]}")
        return 2

    scores: list[CaseScore] = []
    for c in cases:
        ranked = preds[c.case_id]
        unknown = [u for u in ranked if u not in c.unit_ids]
        if unknown:
            print(f"[判分失败] case {c.case_id} 的预测引用了不存在的单元 {unknown[:3]}")
            return 2
        if not c.gold:
            continue          # 没有标准答案的 case（如 LoCoMo 对抗题）不计入召回
        scores.append(score_case(c.case_id, c.category, ranked, c.gold, len(c.unit_ids), ks))

    report = aggregate(scores, ks)
    report["bench"] = args.bench
    report["excluded_no_gold"] = len(cases) - len(scores)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

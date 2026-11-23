"""W-0.2：两份外部基线运行的差异对照（只读，不改任何产物）。

用法：
    python -m eval.external.compare --base .ext-runs/lme-baseline-full \
                                     --new  .ext-runs/lme-post-w23 \
                                     --bench longmemeval --data ../.ext-dl/longmemeval_s

输出：
1. 两份运行的元信息（引擎哈希、数据哈希、参数）——**不一致就拒绝比较**，
   否则比较的是"两份不同的题"，不是"两个版本的代码"。
2. 逐题 recall 对照 + 变好/变差/不变的题号。
3. 汇总差值，并明确写出"样本量"与"不能当结论"的边界。

判定（写进 W-0.2 完成标准）：
- 数据哈希、题目集合、K、keywords 模式必须一致；
- 新版 overall recall@K 不低于基线（允许 ±0：不劣化即可）；
- 任何变差的题都要在输出里点名，供人工写原因。
"""

from __future__ import annotations

import argparse
import json
import os

from . import score
from .benchmarks import load_cases


def _recall_at(sc, k: int):
    """CaseScore.recall 的键是 int（score_case 直接拿 ks 建字典），
    而 aggregate 的键是 str。两种都容忍，否则逐题对照会静默全判"持平"。"""
    if sc is None:
        return None
    return sc.recall.get(k, sc.recall.get(str(k)))


def _load_run(run_dir: str) -> dict:
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _compatible(a: dict, b: dict) -> list[str]:
    problems = []
    for key in ("bench", "data_sha256", "n_cases", "k", "keywords_mode", "haystack"):
        if a.get(key) != b.get(key):
            problems.append(f"{key}: {a.get(key)!r} vs {b.get(key)!r}")
    if sorted(a.get("case_ids") or []) != sorted(b.get("case_ids") or []):
        problems.append("题目集合不同")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对照两份外部基线运行（只读）")
    ap.add_argument("--base", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--bench", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--ks", default="5,10,20")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    base_run, new_run = _load_run(args.base), _load_run(args.new)

    problems = _compatible(base_run, new_run)
    if problems:
        print("两份运行不可比（参数/数据不一致）：")
        for line in problems:
            print(f"  - {line}")
        return 2

    cases = load_cases(
        args.bench, args.data,
        n=base_run.get("n_requested"), haystack=base_run.get("haystack"),
    )
    base_preds = score.load_predictions(os.path.join(args.base, "predictions.jsonl"))
    new_preds = score.load_predictions(os.path.join(args.new, "predictions.jsonl"))

    both = {
        c.case_id for c in cases
        if c.gold and c.case_id in base_preds and c.case_id in new_preds
    }

    def _score(run_dir: str, preds: dict) -> list:
        out = []
        for c in cases:
            if c.case_id not in both:
                continue
            out.append(score.score_case(
                c.case_id, c.category, preds.get(c.case_id, []), c.gold, len(c.unit_ids), ks
            ))
        return out

    base_scores = _score(args.base, base_preds)
    new_scores = _score(args.new, new_preds)
    base_by = {s.case_id: s for s in base_scores}
    new_by = {s.case_id: s for s in new_scores}

    all_ids = [c.case_id for c in cases if c.gold]
    missing = [cid for cid in all_ids if cid not in both]
    compared = [cid for cid in all_ids if cid in both]

    better, worse, same = [], [], []
    per_case = []
    for cid in compared:
        row = {"case_id": cid}
        for k in ks:
            b = _recall_at(base_by[cid], k)
            n = _recall_at(new_by[cid], k)
            row[f"base@{k}"] = b
            row[f"new@{k}"] = n
        top_k = str(ks[-1])  # 逐题归类用本次比较的最大 K，不要写死 20
        b20, n20 = _recall_at(base_by[cid], ks[-1]), _recall_at(new_by[cid], ks[-1])
        if b20 is None or n20 is None:
            same.append(cid)
        elif n20 > b20:
            better.append(cid)
        elif n20 < b20:
            worse.append(cid)
        else:
            same.append(cid)
        per_case.append(row)

    base_overall = score.aggregate(base_scores, ks)["overall"]
    new_overall = score.aggregate(new_scores, ks)["overall"]

    summary = {
        "status": "UNVERIFIED",
        "bench": args.bench,
        "data_sha256": base_run.get("data_sha256"),
        "engine_sha256": {"base": base_run.get("engine_sha256"), "new": new_run.get("engine_sha256")},
        "n_compared": len(compared),
        "n_missing": len(missing),
        "missing": missing,
        "overall": {
            f"@{k}": {
                "base": base_overall.get("recall", {}).get(str(k)),
                "new": new_overall.get("recall", {}).get(str(k)),
                "delta": (
                    None if new_overall.get("recall", {}).get(str(k)) is None
                    or base_overall.get("recall", {}).get(str(k)) is None
                    else round(
                        new_overall["recall"][str(k)] - base_overall["recall"][str(k)], 4
                    )
                ),
            }
            for k in ks
        },
        "cases": {"better": better, "worse": worse, "same": same, "detail": per_case},
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("> STATUS: UNVERIFIED —— 本对照由被测方自己生成，未经第三方审计。\n")
        print(f"题目：{len(compared)}/{len(all_ids)} 可对照，{len(missing)} 题缺预测")
        if missing:
            print(f"缺预测：{missing}")
        print(f"引擎：base={str(base_run.get('engine_sha256'))[:12]}… new={str(new_run.get('engine_sha256'))[:12]}…")
        print()
        print("| K | 基线 | 新版 | 差值 |")
        print("|---|---|---|---|")
        for k in ks:
            cell = summary["overall"][f"@{k}"]
            def fmt(v):
                return "—" if v is None else f"{v * 100:.1f}%"
            delta = cell["delta"]
            print(f"| {k} | {fmt(cell['base'])} | {fmt(cell['new'])} | "
                  f"{'—' if delta is None else f'{delta * 100:+.1f}pp'} |")
        print()
        print(f"变好 {len(better)} 题，变差 {len(worse)} 题，持平 {len(same)} 题")
        if worse:
            print("变差的题（必须逐题写原因）：")
            for cid in worse:
                b = _recall_at(base_by[cid], ks[-1])
                n = _recall_at(new_by[cid], ks[-1])
                print(f"  - {cid}: @20 {b:.3f} → {n:.3f}")
        if len(compared) < 50:
            print("\n样本 <50 题：只能当方向性证据，不能当结论（置信区间 ±10pp 量级）。")
        degraded = any(
            (summary["overall"][f"@{k}"]["delta"] or 0) < 0 for k in ks
        )
        enough = len(compared) >= max(5, int(0.8 * len(all_ids)))
        if not enough:
            print(f"\n判定：样本不足（{len(compared)}/{len(all_ids)} 题有双方预测），"
                  f"不做劣化判定。等两份运行都跑完再比。")
            return 3
        print("\n判定：" + ("新版存在劣化，需逐题解释后再谈通过。" if degraded else "不劣化。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

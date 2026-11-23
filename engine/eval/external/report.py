#!/usr/bin/env python3
"""把 run 产物 + 判分结果渲染成报告（默认 UNVERIFIED）。

这一层刻意很薄：它只做统计汇总和排版，不产生任何"判定"。判定在 score.py，
证据在 predictions.jsonl。报告之所以可被推翻，是因为任何人都能拿同样的输入
重算出同样的数字。
"""
from __future__ import annotations

import argparse
import json
import os

from . import score
from .benchmarks import load_cases

BANNER = (
    "> **STATUS: UNVERIFIED** —— 本报告由被测方自己生成，**未经第三方审计**。\n"
    "> 在第三方按《外部评价体系-v1.md》§8 清单逐条核验并签字之前，\n"
    "> 这里的任何数字都不构成「通过」或「合格」。\n"
)


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _warnings(overall: dict, n_cases: int, ks: list[int]) -> list[str]:
    """把"这个数字没法读"明确写进报告，而不是留给读者自己发现。"""
    out = []
    for k in ks:
        rnd = overall.get("random_recall", {}).get(str(k))
        if rnd is not None and rnd >= 0.5:
            out.append(
                f"K={k} 的随机基线是 {_pct(rnd)}——候选池几乎被 K 覆盖光了，"
                f"该 K 下的分数**不含任何信息**，别引用它。要读 K={k}，候选池至少是它的 10 倍。"
            )
    if 0 < n_cases < 50:
        out.append(f"样本只有 {n_cases} 题，置信区间很宽（±10 个百分点量级），只能当冒烟，不能当结论。")
    return out


def build(run_dir: str, bench: str, data: str, n: int | None, haystack: int | None,
          ks: list[int]) -> tuple[dict, str, list[dict]]:
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as fh:
        run = json.load(fh)
    ingest = {}
    ip = os.path.join(run_dir, "ingest.json")
    if os.path.exists(ip):
        with open(ip, encoding="utf-8") as fh:
            ingest = json.load(fh)
    preds = score.load_predictions(os.path.join(run_dir, "predictions.jsonl"))
    # 子集参数以 run.json 为准：CLI 没显式给就用跑的时候那套，否则重建出来的
    # gold / n_units 与预测对不上（曾把 haystack=20 的运行按 haystack=0 判分，
    # recall@5 从 42.5% 被算成 1.7%）。
    if n is None:
        n = run.get("n_requested")
    if haystack is None:
        haystack = run.get("haystack")
    seed = run.get("seed")
    if seed is None:
        cases = load_cases(bench, data, n=n, haystack=haystack)
    else:
        cases = load_cases(bench, data, n=n, haystack=haystack, seed=int(seed))

    rows = []
    scores = []
    for c in cases:
        ranked = preds.get(c.case_id, [])
        if not c.gold:
            continue
        s = score.score_case(c.case_id, c.category, ranked, c.gold, len(c.unit_ids), ks)
        scores.append(s)
        rows.append({"case_id": c.case_id, "category": c.category, "query": c.query,
                     "gold": c.gold, "gold_size": len(c.gold),
                     "ranked_top": score.dedup_units(ranked)[:10],
                     "recall": {str(k): s.recall[k] for k in ks}})
    agg = score.aggregate(scores, ks)
    agg.update({"bench": bench, "status": "UNVERIFIED", "run": run, "ingest": ingest,
                "excluded_no_gold": len(cases) - len(scores)})
    return agg, run_dir, rows


def render(agg: dict, rows: list[dict], run_dir: str, ks: list[int]) -> str:
    run = agg["run"]
    ing = agg.get("ingest") or {}
    overall = agg["overall"]
    L = []
    L.append(f"# 外部评测报告 · {agg['bench']}")
    L.append("")
    L.append(BANNER)
    L.append("## 0. 这次跑的是什么")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 数据集文件 | `{os.path.basename(run.get('data_path',''))}` |")
    L.append(f"| 数据集 sha256 | `{run.get('data_sha256','')[:16]}…` |")
    L.append(f"| 题数 / 库数 | {run.get('n_cases')} / {run.get('n_shards')} |")
    L.append(f"| 子集参数 | n={run.get('n_requested')}  haystack={run.get('haystack')}  seed={run.get('seed')} |")
    L.append(f"| K | {ks} |")
    L.append(f"| 关键词模式 | {run.get('keywords_mode')} |")
    L.append(f"| 引擎代码指纹 | `{run.get('engine_sha256','')[:16]}…` |")
    L.append(f"| 开始时间 | {run.get('started_at')} |")
    L.append("")
    L.append("## 1. 结果（A-R 检索模式）")
    L.append("")
    L.append("| K | Recall | Hit | 随机基线 | 说明 |")
    L.append("|---|---|---|---|---|")
    for k in ks:
        kk = str(k)
        L.append(f"| {k} | {_pct(overall['recall'].get(kk))} | {_pct(overall['hit'].get(kk))} | "
                 f"{_pct(overall['random_recall'].get(kk))} | 无放回抽样期望 |")
    L.append("")
    warns = _warnings(overall, int(overall.get("n_cases") or 0), ks)
    if warns:
        L.append("> **这份数字不能这么读：**")
        for w in warns:
            L.append(f"> - {w}")
        L.append("")
    L.append("## 2. 分类型")
    L.append("")
    L.append("| 类别 | 题数 | " + " | ".join(f"R@{k}" for k in ks) + " | 随机@5 |")
    L.append("|---" * (2 + len(ks) + 1) + "|")
    for cat, b in agg["by_category"].items():
        L.append(f"| {cat} | {b['n_cases']} | " + " | ".join(
            _pct(b['recall'].get(str(k))) for k in ks) + f" | "
                 f"{_pct(b['random_recall'].get('5'))} |")
    L.append("")
    L.append("## 3. 写入覆盖")
    L.append("")
    if ing:
        L.append(f"- 语料单元 {ing.get('units')}，切块 {ing.get('chunks')}，入库 {ing.get('added')}，"
                 f"准入丢弃 {ing.get('ignored')}，重复判定 {ing.get('touched')}")
        rate = ing.get("ingest_ok_rate")
        if rate is None and ing.get("chunks"):
            rate = (ing.get("added") or 0) / ing["chunks"]
        L.append(f"- 入库成功率 **{_pct(rate)}**（入库块 / 切块），"
                 f"整段未入库单元 {ing.get('dropped_units')}")
        L.append(f"- 耗时 {ing.get('elapsed_s')}s")
    else:
        L.append("- 缺少 ingest.json")
    L.append("")
    L.append("## 4. 失败样例（Recall@20 = 0，前 12 条）")
    L.append("")
    fails = [r for r in rows if (r["recall"].get("20") or 0) == 0][:12]
    if not fails:
        L.append("（无）")
    for r in fails:
        L.append(f"- `{r['case_id']}` **{r['query']}**")
        L.append(f"  - 标准答案：{r['gold'][:3]}")
        L.append(f"  - 实际 top-10：{r['ranked_top']}")
    L.append("")
    L.append("## 5. 已知偏差（读数字前必读）")
    L.append("")
    L.append("- 这是 **A-R 检索模式**：只说明「能否捞到」，不说明「写入治理是否正确」。")
    L.append("- K 上限 20：引擎 \`K_HARD_MAX\` 会静默截断更大的 K。")
    L.append("- 候选上限 200：语料超过 200 块时，候选截断本身会掉分（产品行为）。")
    L.append("- LongMemEval 若用了 \`--haystack\` 子集，分数不可与论文横向比较。")
    L.append("- LoCoMo 许可为 CC BY-NC 4.0，仅限非商业研究。")
    L.append("")
    L.append("## 6. 审计签字")
    L.append("")
    L.append("按《外部评价体系-v1.md》§8 的 10 条逐项核验后填写：")
    L.append("")
    L.append("- 审计人：____________  日期：__________")
    L.append("- 数据集哈希是否一致：□是 □否")
    L.append("- 是否复算过分数：□是（复算值 ____） □否")
    L.append("- 结论：□ 准予引用 □ 退回")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="渲染外部评测报告（默认 UNVERIFIED）")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--bench", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--haystack", type=int, default=None,
                    help="不传则用 run.json 记录的值")
    ap.add_argument("--ks", default="5,10,20")
    args = ap.parse_args(argv)
    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    agg, run_dir, rows = build(args.run_dir, args.bench, args.data,
                               args.n or None, args.haystack, ks)
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in agg.items() if k != "run"} | {"run_summary": {
            "bench": agg["run"].get("bench"), "case_ids": agg["run"].get("case_ids"),
            "data_sha256": agg["run"].get("data_sha256"),
            "engine_sha256": agg["run"].get("engine_sha256")}}, fh, ensure_ascii=False, indent=2)
    md = render(agg, rows, run_dir, ks)
    with open(os.path.join(run_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""写入规模基准（W-2.3 / W-EV1）：n 翻倍时总耗时的增长比应接近 2。

用法：
    python -m eval.bench_write_scale              # 500/1000/2000/4000/5000
    python -m eval.bench_write_scale --n 500 5000 --json

判定口径（写进 W-2.3 完成标准）：
- 最大样本（默认 5000 条）必须跑完，且总耗时不超过 `--max-seconds`（默认 120s）；
- **单次写入耗时的增长倍率**（n=max 的 per_write ÷ n=最小 的 per_write）< 3.0。

为什么不卡"逐段 doubling 比"：那是同一台机器上的相邻两次测量之比，机器上并行
跑着别的东西（比如真的在跑 LongMemEval）时，最后一段会被调度抖动放大到 3 倍以上，
误报成"退化成二次方"。而"500 条时每次写 3ms、5000 条时每次写 4.4ms"这种量级
判断对负载不敏感：真二次方下 n 涨 10 倍，per-write 会涨 10 倍，不是 1.5 倍。
逐段 doubling 仍然打印出来，供人看曲线形状，但不单独作为门槛。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from attune_memory import Memory  # noqa: E402

DOMAINS = ("study", "code", "writing", "career")


def run(n: int, *, shared_vocab: bool = False) -> dict:
    mem = Memory(":memory:")
    started = time.perf_counter()
    for i in range(n):
        if shared_vocab:
            head = DOMAINS[i % len(DOMAINS)]
            content = f"{head}-{i} 专属要点 {i * 7919 % 100003}"
            keywords = [head, f"{head}{i}"]
        else:
            content = f"第{i}号独立要点 {i * 7919 % 100003}"
            keywords = [f"k{i}"]
        mem.add(
            content,
            "domain",
            "fact",
            domain_key="study",
            keywords=keywords,
            explicit=True,
        )
    elapsed = time.perf_counter() - started
    total = mem.count()
    mem.close()
    return {"n": n, "seconds": round(elapsed, 3), "per_write_ms": round(elapsed * 1000 / n, 3), "total": total}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[500, 1000, 2000, 4000, 5000])
    ap.add_argument("--shared-vocab", action="store_true", help="所有条目共享部分词表（更接近真实语料）")
    ap.add_argument("--min-n", type=int, default=5000, help="规模闸要求的最大样本数（本地快速模式可降到 1000）")
    ap.add_argument("--max-seconds", type=float, default=120.0, help="最大样本的耗时上限")
    ap.add_argument("--max-growth", type=float, default=3.0, help="per-write 从最小样本到最大样本的增长倍率上限")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rows: list[dict] = []
    prev: float | None = None
    worst = 0.0
    growth = 0.0
    for n in args.n:
        row = run(n, shared_vocab=args.shared_vocab)
        if prev is not None:
            row["doubling_ratio"] = round(row["seconds"] / prev, 2)
            worst = max(worst, row["doubling_ratio"])
        prev = row["seconds"]
        rows.append(row)
        if not args.json:
            ratio = row.get("doubling_ratio")
            print(
                f"n={row['n']:>5}  {row['seconds']:>7.2f}s  "
                f"{row['per_write_ms']:>6.2f} ms/write"
                + (f"  ratio={ratio:.2f}" if ratio else "")
            )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    if len(rows) >= 2 and rows[0]["per_write_ms"] > 0:
        growth = rows[-1]["per_write_ms"] / rows[0]["per_write_ms"]
    ok = (
        rows[-1]["n"] >= args.min_n
        and rows[-1]["seconds"] <= args.max_seconds
        and growth < args.max_growth
    )
    if not args.json:
        print(
            f"\n最大逐段 doubling 比 {worst:.2f}（参考值）｜"
            f"per-write 增长 {growth:.2f}×（门槛 <{args.max_growth}）｜"
            f"最大样本耗时 {rows[-1]['seconds']:.1f}s（上限 {args.max_seconds:.0f}s）"
            f" → {'PASS' if ok else 'FAIL'}"
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

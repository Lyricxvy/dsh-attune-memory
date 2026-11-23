#!/usr/bin/env python3
"""控制实验：把一条记忆"原样"拿去搜，看它能不能被找回来。

为什么需要它：如果连"原文查原文"都排不进前 k，那么外部基准的低分就不是
"英文问题"或"题太难"，而是检索链路本身断了。反过来，如果原文自检索 100% 命中，
低分就要在"查询措辞与存储措辞的落差"上解释。

这不是基准，不进分数，只进报告的诊断段。
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
import os

from .benchmarks import locomo_shards, longmemeval_shards
from .harness import Engine, ingest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="自检索控制实验")
    ap.add_argument("--bench", choices=["locomo", "longmemeval"], required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--units", type=int, default=200, help="库大小（块数上限）")
    ap.add_argument("--probes", type=int, default=40)
    ap.add_argument("--keywords", default="auto", choices=["auto", "bow"])
    ap.add_argument("--seed", type=int, default=20261119)
    args = ap.parse_args(argv)

    loader = longmemeval_shards if args.bench == "longmemeval" else locomo_shards
    shards = loader(args.data, n=1 if args.bench == "locomo" else 1)
    shard = shards[0]
    units = shard.units[: args.units]

    tmp = tempfile.mkdtemp()
    eng = Engine(os.path.join(tmp, "c.db"))
    try:
        stats, ledger = ingest(eng, units, keywords_mode=args.keywords)
        by_unit: dict[str, str] = {}
        for mid, uid in ledger.items():
            by_unit.setdefault(uid, mid)
        rng = random.Random(args.seed)
        picked = rng.sample(sorted(by_unit), min(args.probes, len(by_unit)))
        texts = {u.unit_id: u.text for u in units}
        top1 = top5 = 0
        for uid in picked:
            hits = eng.call("search", query=texts[uid][:300], limit=20)
            ids = [h["id"] for h in hits]
            target = by_unit[uid]
            if ids[:1] == [target]:
                top1 += 1
            if target in ids[:5]:
                top5 += 1
        n = len(picked)
        print(json.dumps({
            "bench": args.bench, "keywords": args.keywords, "db_units": len(units),
            "db_chunks": stats.as_dict(), "probes": n,
            "self_top1": round(top1 / n, 4) if n else None,
            "self_top5": round(top5 / n, 4) if n else None,
        }, ensure_ascii=False, indent=2))
    finally:
        eng.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

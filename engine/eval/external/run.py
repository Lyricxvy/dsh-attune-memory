#!/usr/bin/env python3
"""跑外部基准，只产出原始产物，不判分。

产物目录（默认 .ext-runs/<bench>-<时间>）：
  run.json            配置 + 数据集 sha256 + 引擎代码指纹 + 耗时
  predictions.jsonl   每个 case 一行：case_id, query, ranked(单元 id 顺序)
  ledgers/<shard>.jsonl   memory_id <-> 语料单元的映射（供第三方复算）
  ingest.json         写入统计（准入丢了多少）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import shutil
import concurrent.futures
import tempfile
import threading
import time
from datetime import datetime

from . import benchmarks
from .harness import Engine, ingest, ranked_units

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(ENGINE_DIR)


def code_fingerprint() -> dict:
    """引擎代码指纹：让"这份报告对应哪份代码"可查，不依赖 git。"""
    files = []
    for root, _dirs, names in os.walk(ENGINE_DIR):
        if "__pycache__" in root or os.sep + "eval" + os.sep in root:
            continue
        for name in sorted(names):
            if name.endswith(".py"):
                p = os.path.join(root, name)
                files.append((os.path.relpath(p, ENGINE_DIR), benchmarks.sha256_file(p)))
    files.sort()
    h = hashlib.sha256()
    for rel, digest in files:
        h.update(f"{rel}:{digest}\n".encode())
    return {"engine_files": len(files), "engine_sha256": h.hexdigest()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑外部基准（A-R），只产生产物不判分")
    ap.add_argument("--bench", required=True, choices=["longmemeval", "locomo"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--n", type=int, default=0, help="0 = 全量")
    ap.add_argument("--haystack", type=int, default=0,
                    help="每题保留的历史 session 数（0=全量，仅 longmemeval）；证据 session 必留")
    #: 引擎把单次检索硬上限压在 20（const.K_HARD_MAX），超过会被静默截断
    ap.add_argument("--k", type=int, default=20)
    ap.add_argument("--seed", type=int, default=benchmarks.DEFAULT_SEED)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keywords", default="auto", choices=["auto", "bow"],
                    help="写入时给不给 keywords：auto=系统抽取，bow=内容实词（对照）")
    ap.add_argument("--keep-db", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="续跑：predictions.jsonl 里已有 case 的 shard 直接跳过，只补缺的")
    ap.add_argument("--force", action="store_true",
                    help="忽略 out 目录里的 .running 锁（上次异常退出留下的锁用这个清）")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并行跑几个 shard（每个 shard 独占一个库与 Engine，互不影响）。"
                         "默认 1：串行、日志最好读；真机上 20 题串行要五小时，并行能把"
                         "墙钟压到十几分钟，而**结果与串行完全一致**（shard 之间无共享状态）")
    args = ap.parse_args(argv)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out or os.path.join(REPO_ROOT, ".ext-runs", f"{args.bench}-{ts}")
    os.makedirs(out, exist_ok=True)
    os.makedirs(os.path.join(out, "ledgers"), exist_ok=True)

    # 单写者锁：同一个 out 目录只允许一个 run 进程。
    # 为什么需要：并行跑时 predictions 是**追加**写的，两个进程同时追加会写出
    # 同一 case 的多行（实测出现过 24 行 /16 个 case），而判分只取最后一次出现，
    # 于是报告看起来"正常"，其实一半是另一份代码的结果。
    lock_path = os.path.join(out, ".running")
    if os.path.exists(lock_path) and not (args.force or args.resume):
        try:
            with open(lock_path, encoding="utf-8") as fh:
                other = json.load(fh)
        except (OSError, ValueError):
            other = {}
        print(f"这个目录已经有一个 run 在写（pid={other.get('pid')}，"
              f"started_at={other.get('started_at')}）。", file=sys.stderr)
        print("确认它已经不在了，就加 --force 重跑；否则换一个 --out。", file=sys.stderr)
        return 4
    with open(lock_path, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")}, fh)

    if args.bench == "longmemeval":
        shards = benchmarks.longmemeval_shards(args.data, n=args.n or None,
                                               haystack=args.haystack or None, seed=args.seed)
    else:
        shards = benchmarks.locomo_shards(args.data, n=args.n or None, seed=args.seed)

    run_meta = {
        "bench": args.bench,
        "data_path": os.path.abspath(args.data),
        "data_bytes": os.path.getsize(args.data),
        "data_sha256": benchmarks.sha256_file(args.data),
        "n_requested": args.n, "haystack": args.haystack, "seed": args.seed, "k": args.k,
        "keywords_mode": args.keywords,
        "jobs": max(1, int(args.jobs)),
        "n_shards": len(shards),
        "n_cases": sum(len(s.cases) for s in shards),
        "case_ids": [c.case_id for s in shards for c in s.cases],
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "python": os.sys.version.split()[0],
        **code_fingerprint(),
    }
    run_json_path = os.path.join(out, "run.json")
    if args.resume and os.path.exists(run_json_path):
        with open(run_json_path, encoding="utf-8") as fh:
            existing = json.load(fh)
        existing.setdefault("resumes", []).append({
            "at": datetime.now().isoformat(timespec="seconds"),
            "jobs": max(1, int(args.jobs)),
        })
        run_meta = existing
    with open(run_json_path, "w", encoding="utf-8") as fh:
        json.dump(run_meta, fh, ensure_ascii=False, indent=2)

    pred_path = os.path.join(out, "predictions.jsonl")
    t_all = time.time()
    ingest_total = {"units": 0, "chunks": 0, "added": 0, "ignored": 0,
                    "touched": 0, "failed": 0, "dropped_units": 0}
    jobs = max(1, int(args.jobs))
    counter = {"done": 0}
    lock = threading.Lock()
    pred_lock = threading.Lock()

    def run_shard(shard) -> dict:
        """跑一个 shard：自己的临时库、自己的 Engine、写入全局 predictions。"""
        tmp = tempfile.mkdtemp(prefix="extdb-")
        db = os.path.join(tmp, "m.db")
        eng = Engine(db)
        stats_local = {}
        try:
            stats, ledger = ingest(
                eng, shard.units,
                ledger_path=os.path.join(out, "ledgers", f"{shard.shard_id}.jsonl"),
                keywords_mode=args.keywords,
            )
            stats_local = {k_: getattr(stats, k_) for k_ in vars(stats)}
            lines = []
            for case in shard.cases:
                rank = ranked_units(eng, case.query, ledger, k=args.k)
                lines.append(json.dumps({
                    "case_id": case.case_id,
                    "query": case.query,
                    "ranked": [r["unit_id"] for r in rank],
                    "hits": len(rank),
                }, ensure_ascii=False))
            with pred_lock:
                with open(pred_path, "a", encoding="utf-8") as pred:
                    for line in lines:
                        pred.write(line + "\n")
        finally:
            eng.close()
            if not args.keep_db:
                shutil.rmtree(tmp, ignore_errors=True)
        with lock:
            counter["done"] += 1
            if counter["done"] % 5 == 0 or counter["done"] == len(shards):
                print(f"[{counter['done']}/{len(shards)}] {time.time() - t_all:.0f}s", flush=True)
        return stats_local

    done_cases: set[str] = set()
    if args.resume and os.path.exists(pred_path):
        with open(pred_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    done_cases.add(json.loads(line)["case_id"])
                except (ValueError, KeyError):
                    continue
        before = len(shards)
        shards = [s for s in shards if any(c.case_id not in done_cases for c in s.cases)]
        print(f"续跑：已有 {len(done_cases)} 题预测，跳过 {before - len(shards)} 个 shard，"
              f"剩 {len(shards)} 个", flush=True)
    if not args.resume:
        # 非续跑：清空（并行时每个 shard 追加写，不能沿用 open(...,'w') 的句柄）
        with open(pred_path, "w", encoding="utf-8"):
            pass

    results: list[dict] = []
    if jobs == 1:
        for shard in shards:
            results.append(run_shard(shard))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(run_shard, shard) for shard in shards]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

    for stats_local in results:
        for k_ in ingest_total:
            ingest_total[k_] += int(stats_local.get(k_, 0) or 0)

    ingest_total["elapsed_s"] = round(time.time() - t_all, 1)
    with open(os.path.join(out, "ingest.json"), "w", encoding="utf-8") as fh:
        json.dump(ingest_total, fh, ensure_ascii=False, indent=2)
    print(f"done -> {out}  ({ingest_total['elapsed_s']}s)")
    try:
        os.remove(lock_path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

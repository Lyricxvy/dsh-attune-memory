#!/usr/bin/env python3
"""下载外部数据集并校验 sha256。

数据集不进仓库（体积 + 上游许可证），但哈希进仓库：
运行时先算文件哈希，对不上 registry.json 就直接拒绝跑——
"换了一份题库"这件事在物理上不会被悄悄发生。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(HERE, "registry.json")


def load_registry() -> dict:
    with open(REGISTRY, encoding="utf-8") as fh:
        return json.load(fh)


def download(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "attune-eval/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            buf = resp.read(1 << 20)
            if not buf:
                break
            out.write(buf)
            done += len(buf)
            if total:
                print(f"  {done / 1e6:.1f}/{total / 1e6:.1f} MB", end="\r", flush=True)
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="下载并校验外部数据集")
    ap.add_argument("--name", required=True, help="registry.json 里的 key")
    ap.add_argument("--dest", required=True)
    args = ap.parse_args(argv)

    entry = load_registry()["datasets"].get(args.name)
    if not entry:
        print(f"未登记的数据集：{args.name}")
        return 1
    if os.path.exists(args.dest):
        print(f"已存在：{args.dest}")
    else:
        print(f"下载 {args.name} <- {entry['url']}")
        download(entry["url"], args.dest)

    from .benchmarks import sha256_file
    got = sha256_file(args.dest)
    if got != entry["sha256"]:
        print(f"[哈希不符] 期望 {entry['sha256']}\n           实际 {got}")
        print("数据集被替换过或下载损坏：拒绝使用。")
        return 2
    print(f"OK  sha256={got}  license={entry.get('license')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

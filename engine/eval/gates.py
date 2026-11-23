"""四道闸（W-EV2）：把"自测全绿"变成可复算的流程。

四道闸的顺序是有理由的——从"最便宜、最不可能被糊弄"到"最贵、最依赖环境"：

1. **契约闸**：`unittest discover`。纯逻辑，不碰网络、不碰模型。它挡住"改坏了还没发现"。
2. **场景闸**：`eval.run_eval` 的 A–E 五个场景。挡住"逻辑对但行为不对"（垃圾入库、
   认错主体、状态并列、上下文污染、scope 串味）。
3. **判分器闸**：`eval.external.test_scorer`。挡住"改了判分口径却当成模型变强"。
4. **规模闸**：`eval.bench_write_scale`。挡住"功能对但 5000 条就卡死"。

任何一闸不过就整体 FAIL（退出码 1）。外部基线（LongMemEval / LoCoMo）**不在**这四闸里：
它要真跑模型、要几十小时，只能人工触发，而且报告一律标 UNVERIFIED——自己给自己打分
不算证据（#39/#43）。

用法：
    python -m eval.gates            # 全跑
    python -m eval.gates --fast     # 规模闸只跑 500/1000（本地开发用）
    python -m eval.gates --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], timeout: int = 1800) -> tuple[int, str]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ENGINE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        code = proc.returncode
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code = 124
        out = f"超时（>{timeout}s）"
    elapsed = time.perf_counter() - started
    tail = "\n".join([line for line in out.strip().splitlines() if line.strip()][-6:])
    return code, f"{elapsed:.1f}s\n{tail}"


GATES = [
    ("contract", "契约闸：单元测试", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]),
    ("scenarios", "场景闸：A–E 五条判据", [sys.executable, "-m", "eval.run_eval"]),
    ("scorer", "判分器闸：判分器校准 + 对照工具自测", [
        sys.executable, "-m", "unittest",
        "eval.external.test_scorer", "eval.external.test_compare", "-q",
    ]),
]

SCALE_FAST = [sys.executable, "-m", "eval.bench_write_scale", "--n", "500", "1000", "--min-n", "1000"]
SCALE_FULL = [sys.executable, "-m", "eval.bench_write_scale", "--n", "500", "1000", "2000", "5000"]


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，子进程输出里的替换字符（U+FFFD）会把它噎住；
    # 这是本机编码问题，不是闸门问题——统一按 UTF-8 输出、坏字符直接替换。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    gates = list(GATES)
    gates.append(("scale", "规模闸：写入 doubling 比", SCALE_FAST if args.fast else SCALE_FULL))

    results = []
    for key, title, cmd in gates:
        code, detail = _run(cmd)
        results.append({"gate": key, "title": title, "ok": code == 0, "detail": detail})
        if not args.json:
            print(f"[{'PASS' if code == 0 else 'FAIL'}] {title}")
            for line in detail.splitlines():
                print(f"    {line}")

    ok = all(r["ok"] for r in results)
    if args.json:
        print(json.dumps({"ok": ok, "gates": results,
                          "status": "UNVERIFIED（外部基线不在四闸内，需人工触发）"},
                         ensure_ascii=False, indent=2))
    else:
        print()
        print("四闸全过。" if ok else "有闸没过，别发版。")
        print("外部基线（LongMemEval/LoCoMo）不在四闸内：报告一律标 UNVERIFIED，"
              "并附 predictions.jsonl 供第三方重算。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""外部基准的引擎适配层（A-R 检索模式）。

设计原则（见 外部评价体系-v1.md）：

1. **只走产品路径**：喂数据用 add，取结果用 search，不 import 引擎内部函数。
   这样测出来的分数是"用户能感知到的行为"，不是"函数按写法工作"。
2. **代理不判分**：这里只负责"把语料灌进去、把排名取出来"，一行判定都不写。
   判分在 score.py，输入只有 predictions.jsonl 和数据集文件。
3. **可审计**：每条记忆 id 与原始语料单元（session/dia_id）的映射写进
   ledger.jsonl，与 predictions 一起落盘，第三方可以用另一个脚本重算。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Iterable

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 引擎写入层的硬上限（engine/attune_memory/write.py: MAX_CONTENT_CHARS）
MAX_CONTENT_CHARS = 300
#: 分块时留一点余量，避免"正好 300 被算成超长"
CHUNK_CHARS = 280


class RpcError(RuntimeError):
    pass


class Engine:
    """一个常驻 RPC 子进程 + 一个全新数据库。一个 case 一个库，互不串味。"""

    def __init__(self, db_path: str) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "attune_memory.rpc", db_path],
            cwd=ENGINE_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1, env=env,
        )
        self._next = 0

    def call(self, op: str, **args: Any) -> Any:
        self._next += 1
        payload = json.dumps({"id": self._next, "op": op, "args": args}, ensure_ascii=False)
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RpcError("引擎没有响应（op=%s）：%s" % (op, err[:400]))
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RpcError(str(resp.get("error")))
        return resp.get("result")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


@dataclass
class Unit:
    """一条待灌入的原始语料单元（一个 session / 一段对话），带可回收的 id。"""
    unit_id: str
    text: str
    date: str | None = None


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """按空白切到 <= size 字符。纯适配行为，不属于产品逻辑。"""
    text = " ".join(text.split())
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    buf = ""
    for word in text.split(" "):
        while len(word) > size:          # 超长单词硬切
            if buf:
                parts.append(buf); buf = ""
            parts.append(word[:size]); word = word[size:]
        cand = (buf + " " + word).strip() if buf else word
        if len(cand) <= size:
            buf = cand
        else:
            parts.append(buf); buf = word
    if buf:
        parts.append(buf)
    return [p for p in parts if len(p) >= 4]


@dataclass
class IngestStats:
    units: int = 0
    chunks: int = 0
    added: int = 0
    ignored: int = 0
    touched: int = 0
    failed: int = 0
    dropped_units: int = 0       # 整段语料一个块都没进去

    def as_dict(self) -> dict:
        return {
            "units": self.units, "chunks": self.chunks, "added": self.added,
            "ignored": self.ignored, "touched": self.touched, "failed": self.failed,
            "dropped_units": self.dropped_units,
            "ingest_ok_rate": round(self.added / self.chunks, 4) if self.chunks else 0.0,
        }


_STOP = {"the","a","an","is","are","was","were","to","of","and","or","in","on","for","with","what","how","why","do","does","did","i","you","it","that","this","my","me","we","us","can","should","would","please","at","be","been","have","has","had","not","but","so","if","then","there","their","he","she","they","them","his","her","from","by","as","about","just","really","very","also"}


def bow_keywords(text: str, limit: int = 8) -> str:
    """确定性"内容实词"关键词（不碰标准答案，只用该条语料自己的字面）。

    存在的意义：产品契约里 keywords 本该由写入方给；不给时 auto_keywords 对英文
    会退化成"前 8 个词"。为了让"检索坏了"和"关键词没喂好"能分开报告，
    增加这个 mode，作为对照，而不是替代品。
    """
    out: list[str] = []
    for w in " ".join(text.split()).split():
        w = "".join(ch for ch in w.lower() if ch.isalnum() or ch == "-")
        if len(w) >= 3 and w not in _STOP and w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return " ".join(out)


def ingest(engine: Engine, units: Iterable[Unit], *, ledger_path: str | None = None,
           keywords_mode: str = "auto") -> tuple[IngestStats, dict[str, str]]:
    """把语料灌进空库。返回 (统计, memory_id -> unit_id)。

    add 的准入会丢掉一部分块（闲聊、过短），这是产品的真实行为：
    不绕过、不补写，只如实记录。判分时按"存进去的"和"该在的"两种口径分别算。
    """
    stats = IngestStats()
    ledger: dict[str, str] = {}
    fh = open(ledger_path, "w", encoding="utf-8") if ledger_path else None
    try:
        for unit in units:
            stats.units += 1
            pieces = chunk(unit.text)
            stats.chunks += len(pieces)
            got = 0
            for i, piece in enumerate(pieces):
                extra = {}
                if keywords_mode == "bow":
                    kw = bow_keywords(unit.text)
                    if kw:
                        extra["keywords"] = kw
                try:
                    dec = engine.call(
                        "add", content=piece, type="episodic", subtype="event",
                        explicit=True, importance="normal", injection_policy="contextual", **extra
                    )
                except RpcError:
                    stats.failed += 1
                    continue
                action = dec.get("action")
                if action == "add":
                    stats.added += 1
                    mem = dec.get("memory") or {}
                    mid = mem.get("id")
                    if mid:
                        ledger[mid] = unit.unit_id
                        got += 1
                        if fh:
                            fh.write(json.dumps({"memory_id": mid, "unit_id": unit.unit_id,
                                                 "chunk": i, "text": piece}, ensure_ascii=False) + "\n")
                elif action == "touch":
                    mem = dec.get("memory") or {}
                    mid = mem.get("id")
                    stats.touched += 1
                    if mid:
                        ledger.setdefault(mid, unit.unit_id)
                        got += 1
                else:
                    stats.ignored += 1
            if got == 0:
                stats.dropped_units += 1
    finally:
        if fh:
            fh.close()
    return stats, ledger


def ranked_units(engine: Engine, query: str, ledger: dict[str, str], *, k: int = 50) -> list[dict]:
    """一次检索取到最大 K；排名只算一次，所有小 K 都从同一份排名里截断。"""
    hits = engine.call("search", query=query, limit=k)
    out: list[dict] = []
    for rank, h in enumerate(hits, start=1):
        mem_id = h.get("id")
        out.append({
            "rank": rank,
            "memory_id": mem_id,
            "unit_id": ledger.get(mem_id),
            "score": h.get("score"),
            "content": (h.get("content") or "")[:200],
        })
    return out

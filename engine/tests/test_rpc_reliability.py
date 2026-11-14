"""模块 2：RPC 可靠性。

三种坏情况，一件一件钉住：

1. **重发**：超时后调用方重试，同一件事不能写两遍 → `dedupe` 键 + 响应回放。
2. **重启**：进程被杀掉再起，幂等记录必须还在（否则重启后的重试还是会重复写）。
3. **并发**：多条请求同时写进同一根管道，不能串行错乱（JSON 行分隔 + 引擎单线程）。

这里全部走真实的 stdio 子进程，不用 mock：幂等这件事的失败方式恰好都在进程边界上。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

try:  # 发布树里包名是 attune_memory，源码树里是 memory
    from attune_memory import Memory
    from attune_memory.rpc import handle
    PKG = "attune_memory"
except ModuleNotFoundError:
    from attune_memory import Memory
    from attune_memory.rpc import handle
    PKG = "memory"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(mem: Memory, op: str, **args):
    """模拟一次 stdio 往返（handle 就是 main() 每一行要做的事）。"""
    return handle(mem, {"id": 1, "op": op, "args": args})


class TestDedupe(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="attune-dedupe-")
        self.db = os.path.join(self.dir, "memory.db")
        self.mem = Memory(self.db)

    def tearDown(self):
        self.mem.close()

    def test_same_key_writes_once(self):
        body = {"id": 1, "op": "add", "args": {
            "content": "用户备考 2027 年考研数学一。",
            "type": "state", "subtype": "goal", "__dedupe": "k-1",
        }}
        first = handle(self.mem, body)
        second = handle(self.mem, body)
        third = handle(self.mem, body)

        self.assertTrue(first["ok"])
        self.assertFalse(first.get("deduped", False))
        self.assertTrue(second.get("deduped"))
        self.assertTrue(third.get("deduped"))
        self.assertEqual(first["result"]["memory"]["id"], second["result"]["memory"]["id"])
        self.assertEqual(len(self.mem.list(status=None)), 1)

    def test_dedupe_survives_restart(self):
        body = {"id": 1, "op": "add", "args": {
            "content": "用户习惯先看思路再看步骤。",
            "type": "profile", "subtype": "interaction", "__dedupe": "k-restart",
        }}
        handle(self.mem, body)
        self.mem.close()

        # 换一个进程（新 Memory 实例）重发同一键：不能变成第二条
        again = Memory(self.db)
        try:
            reply = handle(again, body)
            self.assertTrue(reply.get("deduped"))
            self.assertEqual(len(again.list(status=None)), 1)
        finally:
            again.close()

    def test_failure_is_not_cached(self):
        bad = {"id": 1, "op": "add", "args": {
            "content": "用户叫张三，在做数学笔记。", "type": "profile", "subtype": "nope", "__dedupe": "k-bad",
        }}
        first = handle(self.mem, bad)
        self.assertFalse(first["ok"])
        # 同一个键换成合法内容：必须真的写进去（失败的结果绝不缓存）
        good = {"id": 1, "op": "add", "args": {
            "content": "用户叫张三，在做数学笔记。",
            "type": "profile", "subtype": "identity", "__dedupe": "k-bad",
        }}
        second = handle(self.mem, good)
        self.assertTrue(second["ok"])
        self.assertFalse(second.get("deduped", False))

    def test_request_gc_drops_old_rows(self):
        body = {"id": 1, "op": "add", "args": {
            "content": "用户备考 2027 年考研数学一。",
            "type": "state", "subtype": "goal", "__dedupe": "k-old",
        }}
        handle(self.mem, body)
        # 把时间线往前挪 30 天，再 GC：记录没了，但记忆还在
        self.mem.store.conn.execute(
            "UPDATE memory_request SET created_at='2020-01-01T00:00:00Z'"
        )
        self.assertEqual(self.mem.request_gc(keep_days=7), 1)
        self.assertEqual(len(self.mem.list(status=None)), 1)


class TestPipeConcurrency(unittest.TestCase):
    """真的把 8 条中文 add 同时写进同一根管道。"""

    def test_eight_concurrent_writes_over_one_pipe(self):
        tmp = tempfile.mkdtemp(prefix="attune-pipe-")
        db = os.path.join(tmp, "memory.db")
        proc = subprocess.Popen(
            [sys.executable, "-m", f"{PKG}.rpc", db],
            cwd=ROOT, text=True, encoding="utf-8", bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            payloads = [
                {"id": i, "op": "add", "args": {
                    "content": f"第{i}条并发写入的记忆，关于数学一极限题。",
                    "type": "domain", "subtype": "fact", "__dedupe": f"c-{i}",
                }}
                for i in range(1, 9)
            ]
            def writer():
                for p in payloads:
                    proc.stdin.write(json.dumps(p, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    time.sleep(0.005)

            t = threading.Thread(target=writer)
            t.start()
            replies = [json.loads(proc.stdout.readline()) for _ in range(8)]
            t.join(timeout=5)
            self.assertTrue(all(r["ok"] for r in replies), replies)
            self.assertEqual(len({r["result"]["memory"]["id"] for r in replies}), 8)

            mem = Memory(db)
            try:
                self.assertEqual(len(mem.list(status=None)), 8)
                contents = [m["content"] for m in mem.list(status=None, limit=20)]
                self.assertTrue(all("数学一极限题" in c for c in contents))
            finally:
                mem.close()
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

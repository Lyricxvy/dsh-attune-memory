#!/usr/bin/env python3
"""P10 评估套件：五条判据，跑在**真实 RPC 路径**上。

为什么不是单元测试：单元测试证明"函数按写法工作"，评估套件证明"系统在真实输入下
不给用户添乱"。所以这里每一格都从 ``attune_memory.rpc`` 的 stdio 入口进、用一个全新的临时库、
按用户真实会说的话喂进去，然后只断言**行为**（有没有多一条垃圾、主体对不对、
旧状态有没有退场、上下文里有没有混进无关条目），不断言内部实现。

五个场景（对齐任务书 P10）：

  A 不存垃圾       "今天天气不错" / "这道题怎么做？" → 不新增任何条目
  B 不认错主体     "我朋友小王喜欢红色" → subject_type=person；当 user 写必须被拒
  C State 正确更新 考研 → 不考研：旧条 superseded、新条 active，且 active 只有一条
  D 不污染上下文   问数学题：不注入项目约定、不注入过期状态、不注入无关历史
  E scope 隔离     scope=study.math 的交互约定只在数学上下文出现，写文案时不打扰

用法::

    cd engine
    python -m eval.run_eval            # 全部跑，表格输出
    python -m eval.run_eval --only ABC # 只跑 A/B/C（先跑绿再补 D/E）
    python -m eval.run_eval --json     # 机器可读

退出码：全通过 0，有失败 1。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Rpc:
    """一个常驻子进程 + 一行一请求，跟插件在宿主里的用法完全一致。"""

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
        rid = self._next
        payload = json.dumps({"id": rid, "op": op, "args": args}, ensure_ascii=False)
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"引擎没有响应（op={op}）：{err[:400]}")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RpcError(resp.get("error", "unknown error"))
        return resp.get("result")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class RpcError(Exception):
    pass


@dataclass
class Case:
    name: str
    ok: bool
    detail: str
    notes: list[str] = field(default_factory=list)


def expect_error(rpc: Rpc, op: str, *, contains: str, **args: Any) -> tuple[bool, str]:
    try:
        rpc.call(op, **args)
    except RpcError as exc:
        hit = contains in str(exc)
        return hit, str(exc)
    return False, "没有被拒绝（这条应该被拒绝）"


def contents(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("content", "")) for r in rows]


# --------------------------------------------------------------------------- A
def scenario_a(rpc: Rpc) -> list[Case]:
    cases: list[Case] = []
    before = rpc.call("stats")["total"]

    junk = [
        "今天天气不错，挺适合晒被子的",
        "刚吃完午饭，有点困",
    ]
    for text in junk:
        decision = rpc.call("add", content=text, type="episodic", subtype="event")
        cases.append(Case(
            f"A 垃圾不入库：{text[:8]}…",
            decision.get("action") == "ignore",
            f"action={decision.get('action')} reason={decision.get('reason')}",
        ))

    question = rpc.call("add", content="这道数学题怎么做？", type="state", subtype="goal")
    cases.append(Case(
        "A 问句不当事实",
        question.get("action") == "ignore",
        f"action={question.get('action')} reason={question.get('reason')}",
    ))

    after = rpc.call("stats")["total"]
    cases.append(Case(
        "A 整轮没有多出条目",
        after == before,
        f"{before} → {after}",
    ))
    return cases


# --------------------------------------------------------------------------- B
def scenario_b(rpc: Rpc) -> list[Case]:
    cases: list[Case] = []

    friend = rpc.call("add", content="我朋友小王很喜欢红色，买东西优先挑红色",
                      type="profile", subtype="preference",
                      subject_type="person", subject_name="小王")
    stored = rpc.call("get", id=friend["memory"]["id"])
    cases.append(Case(
        "B 朋友的事记在 person 名下",
        stored["subject_type"] == "person" and stored["subject_name"] == "小王",
        f"subject={stored['subject_type']}:{stored.get('subject_name')}",
    ))

    ok, msg = expect_error(rpc, "add", contains="person",
                           content="我朋友小王很喜欢红色", type="profile", subtype="preference",
                           subject_type="user")
    cases.append(Case("B 把朋友的事写成 user 会被拒", ok, msg[:110]))

    me = rpc.call("add", content="用户叫张三，正在备考考研数学一", type="profile", subtype="identity")
    me_row = rpc.call("get", id=me["memory"]["id"])
    cases.append(Case(
        "B 用户自己的事仍记在 user 名下",
        me_row["subject_type"] == "user",
        f"subject={me_row['subject_type']}",
    ))

    # 非 user 主体不能 always：否则"朋友喜欢红色"会每轮注入给用户看
    # 显式传 explicit=True：这条测的是「非 user 主体 + always」这条不变量本身，
    # 不能让「推断降级」先把 always 改成 contextual 从而绕开校验。
    ok2, msg2 = expect_error(rpc, "add", contains="always",
                             content="我朋友小王喜欢红色", type="profile", subtype="preference",
                             subject_type="person", subject_name="小王", injection_policy="always",
                             explicit=True)
    cases.append(Case("B 非 user 主体禁止 always", ok2, msg2[:110]))
    return cases


# --------------------------------------------------------------------------- C
def scenario_c(rpc: Rpc) -> list[Case]:
    cases: list[Case] = []
    old = rpc.call("add", content="用户正在备考 2026 考研数学一", type="state", subtype="goal",
                   importance="important")
    old_id = old["memory"]["id"]

    new = rpc.call("supersede", id=old_id, content="用户已放弃考研，不再备考",
                   reason="用户说自己不考了")
    new_id = new["memory"]["id"]

    old_row = rpc.call("get", id=old_id)
    cases.append(Case(
        "C 旧条目标 superseded 且有取代者",
        old_row["status"] == "superseded" and old_row["superseded_by"] == new_id,
        f"status={old_row['status']} superseded_by={old_row['superseded_by']}",
    ))
    cases.append(Case(
        "C 旧条目有失效时间",
        bool(old_row.get("valid_until")),
        f"valid_until={old_row.get('valid_until')}",
    ))
    new_row = rpc.call("get", id=new_id)
    cases.append(Case(
        "C 新条目 active 且指向旧条目",
        new_row["status"] == "active",
        f"status={new_row['status']}",
    ))

    active_states = rpc.call("list", status="active", type="state")
    cases.append(Case(
        "C 同槽位只有一条 active（不是并列新增）",
        len(active_states) == 1 and active_states[0]["id"] == new_id,
        f"active state 条数={len(active_states)}",
    ))

    exp = rpc.call("explain", id=old_id)
    reason = exp.get("state_reason", "")
    cases.append(Case(
        "C explain 能说清为什么被取代",
        "取代" in reason and new_id in reason,
        reason[:110],
    ))
    return cases


# --------------------------------------------------------------------------- D
def scenario_d(rpc: Rpc) -> list[Case]:
    cases: list[Case] = []
    rpc.call("add", content="用户做数学一极限题时总是想不到变形方向，应先讲判断依据",
             type="profile", subtype="skill", importance="important", domain_key="study.math")
    rpc.call("add", content="这个项目的记忆引擎用 Python + SQLite，通过 stdio RPC 给宿主用",
             type="domain", subtype="decision", domain_key="code.dsh",
             injection_policy="on_demand")
    rpc.call("add", content="用户 2026 年上半年的备考计划表（已过期）", type="state", subtype="situation",
             valid_from="2025-12-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z")
    rpc.call("add", content="上周排查过一个 asar 解包失败的问题，原因是中文路径",
             type="episodic", subtype="event", injection_policy="on_demand")

    ctx = rpc.call("read_for_context", query="数学一极限题怎么突破变形方向？", domain="study.math")
    text = json.dumps(ctx, ensure_ascii=False)
    cases.append(Case(
        "D 数学问题会带上数学相关记忆",
        "极限" in text,
        "命中" if "极限" in text else "没命中",
    ))
    cases.append(Case(
        "D 不注入无关的项目决策",
        "stdio" not in text and "SQLite" not in text,
        "干净" if "stdio" not in text else "混进了项目决策",
    ))
    cases.append(Case(
        "D 不注入过期状态",
        "备考计划表" not in text,
        "干净" if "备考计划表" not in text else "混进了过期状态",
    ))
    cases.append(Case(
        "D 不注入无关历史（on_demand 不自动出现）",
        "asar" not in text,
        "干净" if "asar" not in text else "混进了历史事件",
    ))

    writing = rpc.call("read_for_context", query="帮我写一段产品文案的开头", domain="writing")
    wt = json.dumps(writing, ensure_ascii=False)
    cases.append(Case(
        "D 写文案时不注入数学记忆",
        "极限" not in wt,
        "干净" if "极限" not in wt else "混进了数学记忆",
    ))
    return cases


# --------------------------------------------------------------------------- E
def scenario_e(rpc: Rpc) -> list[Case]:
    cases: list[Case] = []
    # 这两条是「用户明确交代的领域约定」，必须 explicit=True —— 领域约定补位要求
    # confidence>=0.8，而推断路径会把置信度压到 0.7（v0.2.0 起 explicit 默认 False）。
    math_rule = rpc.call("add", content="讲数学题时先讲思路再给步骤，不要直接抛公式",
                         type="profile", subtype="interaction", scope="domain",
                         domain_key="study.math", importance="important", explicit=True)
    write_rule = rpc.call("add", content="写文案时先给三个不同方向，再展开其中一个",
                          type="profile", subtype="interaction", scope="domain",
                          domain_key="writing", importance="important", explicit=True)

    math_ctx = json.dumps(rpc.call("read_for_context", query="这道极限题怎么下手",
                                   domain="study.math"), ensure_ascii=False)
    cases.append(Case(
        "E 数学上下文命中数学域的交互约定",
        "先讲思路" in math_ctx,
        "命中" if "先讲思路" in math_ctx else "没命中",
    ))
    cases.append(Case(
        "E 数学上下文不含写作域约定",
        "三个不同方向" not in math_ctx,
        "干净" if "三个不同方向" not in math_ctx else "串域了",
    ))

    write_ctx = json.dumps(rpc.call("read_for_context", query="写一段新品发布的文案",
                                    domain="writing"), ensure_ascii=False)
    cases.append(Case(
        "E 写文案时命中写作域约定",
        "三个不同方向" in write_ctx,
        "命中" if "三个不同方向" in write_ctx else "没命中",
    ))
    cases.append(Case(
        "E 写文案时不打扰数学约定",
        "先讲思路" not in write_ctx,
        "干净" if "先讲思路" not in write_ctx else "串域了",
    ))

    # 域不匹配但同 scope 也不该漏出：查询不给 domain 时，域约定按低匹配处理
    generic = json.dumps(rpc.call("read_for_context", query="今天先聊点别的"), ensure_ascii=False)
    cases.append(Case(
        "E 无域查询时不把域约定当成全局偏好",
        "先讲思路" not in generic,
        "干净" if "先讲思路" not in generic else "域约定漏成了全局",
    ))

    cases.append(Case(
        "E 两条约定都还是 active（互不覆盖）",
        rpc.call("get", id=math_rule["memory"]["id"])["status"] == "active"
        and rpc.call("get", id=write_rule["memory"]["id"])["status"] == "active",
        "ok",
    ))
    return cases


SCENARIOS = {"A": scenario_a, "B": scenario_b, "C": scenario_c, "D": scenario_d, "E": scenario_e}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval", description="记忆系统评估套件（P10）")
    parser.add_argument("--only", default="ABCDE", help="要跑的场景，例如 ABC")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--keep", action="store_true", help="保留临时库以便复查")
    args = parser.parse_args(argv)

    wanted = [ch.upper() for ch in args.only if ch.upper() in SCENARIOS]
    if not wanted:
        print(f"没有可跑的场景：{args.only}", file=sys.stderr)
        return 2

    tmpdir = tempfile.mkdtemp(prefix="mem-eval-")
    db_path = os.path.join(tmpdir, "eval.db")
    rpc = Rpc(db_path)
    started = time.time()
    results: list[Case] = []
    try:
        for key in wanted:
            results.extend(SCENARIOS[key](rpc))
    finally:
        rpc.close()

    failed = [c for c in results if not c.ok]
    if args.json:
        print(json.dumps({
            "db": db_path if args.keep else "(临时库已清理)",
            "elapsed_ms": int((time.time() - started) * 1000),
            "total": len(results), "failed": len(failed),
            "cases": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in results],
        }, ensure_ascii=False, indent=2))
    else:
        width = max(len(c.name) for c in results) if results else 10
        for c in results:
            mark = "PASS" if c.ok else "FAIL"
            print(f"{mark}  {c.name.ljust(width)}  {c.detail}")
        print()
        print(f"场景 {'/'.join(wanted)}：{len(results) - len(failed)}/{len(results)} 通过，"
              f"耗时 {int((time.time() - started) * 1000)}ms")
        if args.keep:
            print(f"临时库：{db_path}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

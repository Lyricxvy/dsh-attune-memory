"""JSON-RPC over stdio —— 给 Node 侧插件用的最小协议。

为什么是子进程 + stdio，而不是 HTTP：记忆读写要快、要能背压，也不能把 Python
嵌进宿主进程。进程崩了插件 fail-open（记忆暂时失效），不牵连 DSH 本身。

请求：``{"id": 1, "op": "search", "args": {...}}``
响应：``{"id": 1, "ok": true, "result": ...}`` 或 ``{"id": 1, "ok": false, "error": "..."}``
"""
from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from . import const
from .manager import Memory
from .store import InvariantError
from .write import CandidateError

#: 引擎的机器契约只有一处 —— 下面这张 OPS 表。以前 _SPEC / _required / _call / WRITE_OPS
#: 分散在四个地方，于是出现了"dispatch 有 preview、白名单没有"这种漂移（T-002 第 1 条）。
#: 现在 params/required/writes/handler 同处声明，其余全部派生。

@dataclass(frozen=True)
class OpSpec:
    """一个 op 的完整契约：允许什么参数、哪些必填、是否写操作、怎么执行。

    四者必须同处声明 —— 这正是 T-002 暴露的那类漂移（``_call`` 有 preview、
    ``_SPEC`` 没有）在结构上不可能再发生的原因：白名单与 dispatch 都从这张表派生。
    """

    params: tuple[str, ...]
    required: tuple[str, ...]
    writes: bool
    handler: Callable[["Memory", dict[str, Any]], Any]


def _rest(args: dict[str, Any], *taken: str) -> dict[str, Any]:
    """取「除这几个键之外」的其余参数，交给 manager 的 **kwargs。"""
    return {k: v for k, v in args.items() if k not in taken}


def _op_add(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.add(**a)


def _op_preview(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.preview(**a)


def _op_update(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.update(a["id"], reason=a.get("reason", "模型修正"), **dict(a["fields"]))


def _op_supersede(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.supersede(a["id"], a["content"], **_rest(a, "id", "content"))


def _op_merge(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.merge(a["ids"], a["content"], **_rest(a, "ids", "content"))


def _op_archive(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.archive(a["id"], reason=a.get("reason", "不再需要"))


def _op_forget(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.forget(a["id"], reason=a.get("reason", "用户要求忘记"))


def _op_restore(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.restore(a["id"], reason=a.get("reason", "恢复"))


def _op_purge(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.purge(a["id"], reason=a.get("reason", "物理删除"))


def _op_get(mem: Memory, a: dict[str, Any]) -> Any:
    # 查不到不是错误：get 的契约就是"有就返回，没有就 null"。
    # 让这里抛异常会把 404 变成 400，调用方永远分不清"没有"和"出错"。
    return mem.get(a["id"])


def _op_explain(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.explain(a["id"])


def _op_list(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.list(**a)


def _op_search(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.search(**a)


def _op_find_similar(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.find_similar(**a)


def _op_read_for_context(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.read_for_context(**a)


def _op_recent(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.recent(a["since"], a.get("limit", 50))


def _op_stats(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.stats()


def _op_tick(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.tick()


def _op_scan(mem: Memory, a: dict[str, Any]) -> Any:
    limit = a.get("limit", 500)
    return mem.scan(limit=limit if isinstance(limit, int) and limit > 0 else 500)


def _op_record(mem: Memory, a: dict[str, Any]) -> Any:
    ids = a.get("injected_ids")
    return mem.record(
        a.get("counters"),
        a.get("observation"),
        ids if isinstance(ids, list) else None,
    )


def _op_telemetry(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.telemetry(limit=a.get("limit", 20))


def _op_reads(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.reads(limit=a.get("limit", 20))


def _op_discarded(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.discarded(limit=a.get("limit", 20))


def _op_metrics(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.metrics(limit=a.get("limit", 20))


def _op_purge_observations(mem: Memory, a: dict[str, Any]) -> Any:
    return mem.purge_observations(a.get("kind"))


def _op_describe(mem: Memory, a: dict[str, Any]) -> Any:
    return describe_ops()


#: **唯一**的 op 契约表。新增 op 只改这里；白名单、必填、写标志、dispatch 全部派生。
OPS: dict[str, OpSpec] = {
    "add": OpSpec(
        ("content", "type", "subtype", "subject_type", "subject_name", "domain_key",
         "scope", "importance", "injection_policy", "confidence", "keywords", "explicit",
         "source_message_id", "source_conversation_id", "valid_from", "valid_until", "metadata"),
        ("content", "type", "subtype"), True, _op_add),
    "preview": OpSpec(
        ("content", "type", "subtype", "subject_type", "subject_name", "domain_key",
         "scope", "importance", "injection_policy", "confidence", "keywords", "explicit",
         "source_message_id", "source_conversation_id", "valid_from", "valid_until", "metadata"),
        ("content", "type", "subtype"), False, _op_preview),
    "update": OpSpec(("id", "fields", "reason"), ("id", "fields"), True, _op_update),
    "supersede": OpSpec(
        ("id", "content", "reason", "confidence", "importance", "keywords",
         "injection_policy", "valid_until", "metadata"),
        ("id", "content"), True, _op_supersede),
    "merge": OpSpec(("ids", "content", "reason", "importance", "keywords"),
                    ("ids", "content"), True, _op_merge),
    "archive": OpSpec(("id", "reason"), ("id",), True, _op_archive),
    "delete": OpSpec(("id", "reason"), ("id",), True, _op_forget),
    "forget": OpSpec(("id", "reason"), ("id",), True, _op_forget),
    "restore": OpSpec(("id", "reason"), ("id",), True, _op_restore),
    "purge": OpSpec(("id", "reason"), ("id",), True, _op_purge),
    "get": OpSpec(("id",), ("id",), False, _op_get),
    "explain": OpSpec(("id",), ("id",), False, _op_explain),
    "list": OpSpec(("status", "type", "subtype", "domain_key", "policy", "limit", "offset"),
                   (), False, _op_list),
    "search": OpSpec(("query", "domain", "type", "subtypes", "policies", "limit", "task_key",
                      "include_expired", "include_superseded", "include_never"),
                     (), False, _op_search),
    "find_similar": OpSpec(("content", "type", "subtype", "subject_type", "subject_name",
                            "domain_key", "limit"), ("content",), False, _op_find_similar),
    "read_for_context": OpSpec(("query", "domain", "task_key"), (), False, _op_read_for_context),
    "recent": OpSpec(("since", "limit"), ("since",), False, _op_recent),
    "stats": OpSpec((), (), False, _op_stats),
    "tick": OpSpec((), (), True, _op_tick),
    "scan": OpSpec(("limit",), (), False, _op_scan),
    # record / purge_observations 会改库，但改的是遥测表，不是记忆本身。
    # 它们不进 WRITE_OPS：注入记账每轮都发生，给它套幂等回放只会让 memory_request 膨胀。
    "record": OpSpec(("counters", "observation", "injected_ids"), (), False, _op_record),
    "metrics": OpSpec(("limit",), (), False, _op_metrics),
    "telemetry": OpSpec(("limit",), (), False, _op_telemetry),
    "reads": OpSpec(("limit",), (), False, _op_reads),
    "discarded": OpSpec(("limit",), (), False, _op_discarded),
    "purge_observations": OpSpec(("kind",), (), False, _op_purge_observations),
    "describe": OpSpec((), (), False, _op_describe),
}

#: 派生视图：给测试与兼容旧代码用，不是第二份事实源。
#: ``writes`` 的语义刻意收窄成"要不要幂等语义"，不是"会不会改库"——遥测写入不进这张名单。
_SPEC: dict[str, tuple[str, ...]] = {op: spec.params for op, spec in OPS.items()}
WRITE_OPS: frozenset[str] = frozenset(op for op, spec in OPS.items() if spec.writes)


def describe_ops() -> list[dict[str, Any]]:
    """返回每个 op 的机器契约（JS 侧启动自检就比对这份）。"""
    return [
        {"op": op, "params": list(spec.params), "required": list(spec.required), "writes": spec.writes}
        for op, spec in OPS.items()
    ]


class RpcError(ValueError):
    pass


def _check_args(op: str, args: dict) -> None:
    """唯一门：任何不在 params 里的参数直接拒。

    在 handler 之前拦，handler 就永远不可能"静默吃下一个未声明参数"。
    """
    spec = OPS[op]
    extra = set(args) - set(spec.params)
    if extra:
        raise RpcError(f"{op} 不接受这些参数：{sorted(extra)}；可用：{list(spec.params)}")
    missing = [k for k in spec.required if k not in args]
    if missing:
        raise RpcError(f"{op} 缺少必填参数：{missing}")


def _call(mem: Memory, op: str, args: dict[str, Any]) -> Any:
    spec = OPS.get(op)
    if spec is None:
        raise RpcError(f"未知操作：{op}；可用：{sorted(OPS)}")
    _check_args(op, args)
    return spec.handler(mem, args)


def handle(mem: Memory, req: dict) -> dict:
    rid = req.get("id")
    op = req.get("op")
    args = req.get("args") or {}
    if not isinstance(args, dict):
        return {"id": rid, "ok": False, "error": "args 必须是对象"}

    # 幂等（模块 2）：写类请求可能因为超时/进程重启被重发。带同一个 dedupe 键时，
    # 第二次起原样返回第一次的结果，而不是再写一遍。只缓存成功结果——失败允许重试。
    cfg = dict(args)
    dedupe = cfg.pop("__dedupe", None)
    if dedupe and op in WRITE_OPS:
        saved = mem.store.request_get(str(dedupe))
        if saved is not None:
            return {"id": rid, "ok": True, "result": json.loads(saved["response"]),
                    "deduped": True}

    try:
        result = _call(mem, str(op), cfg)
        if dedupe and op in WRITE_OPS:
            try:
                mem.store.request_put(str(dedupe), str(op), json.dumps(result, ensure_ascii=False))
            except Exception:  # noqa: BLE001 - 记幂等键失败不能把成功的写入变成失败
                pass
        return {"id": rid, "ok": True, "result": result}
    except (CandidateError, InvariantError, RpcError, KeyError) as exc:
        # 这四类是"用户/调用方能纠正的错误"：直接给原因，不附 traceback。
        # 这些都算"调用方问题"，不计入 rpc_errors（模块 6 只数引擎侧意外）。
        return {"id": rid, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 边界处必须兜住一切，否则插件会挂
        try:
            mem.store.counter_bump("rpc_errors")
        except Exception:  # noqa: BLE001 - 计数失败绝不能再把错误分支搞崩
            pass
        return {
            "id": rid,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=6),
        }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    db = argv[0] if argv else ":memory:"
    # Windows 上 sys.stdin/stdout 默认跟控制台代码页走（中文机器是 cp936），
    # 而调用方（Node 插件）永远按 UTF-8 读写管道。不显式 reconfig 的话，
    # 中文请求会被解码成孤立代理字符，然后 json.dumps 抛 UnicodeEncodeError，
    # 整个记忆读写全挂 —— 报错还长这样：'utf-8' codec can't encode '\udca6'。
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非 TextIOWrapper
            pass
    mem = Memory(db)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out.write(json.dumps({"id": None, "ok": False, "error": "请求不是合法 JSON"}) + "\n")
            out.flush()
            continue
        resp = handle(mem, req)
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()
    mem.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

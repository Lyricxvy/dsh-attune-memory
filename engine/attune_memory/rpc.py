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
from typing import Any, Callable

from . import const
from .manager import Memory
from .store import InvariantError
from .write import CandidateError

#: 每个 op 允许的参数。多传的参数直接报错——静默忽略会让调用方以为生效了。
_SPEC: dict[str, tuple[str, ...]] = {
    "add": ("content", "type", "subtype", "subject_type", "subject_name", "domain_key",
            "scope", "importance", "injection_policy", "confidence", "keywords", "explicit",
            "source_message_id", "source_conversation_id", "valid_from", "valid_until", "metadata"),
    "update": ("id", "fields", "reason"),
    "supersede": ("id", "content", "reason", "confidence", "importance", "keywords",
                  "injection_policy", "valid_until", "metadata"),
    "merge": ("ids", "content", "reason", "importance", "keywords"),
    "archive": ("id", "reason"),
    "delete": ("id", "reason"),
    "forget": ("id", "reason"),
    "restore": ("id", "reason"),
    "purge": ("id", "reason"),
    "get": ("id",),
    "explain": ("id",),
    "list": ("status", "type", "subtype", "domain_key", "policy", "limit", "offset"),
    "search": ("query", "domain", "type", "subtypes", "policies", "limit", "task_key",
               "include_expired", "include_superseded", "include_never"),
    "find_similar": ("content", "type", "subtype", "subject_type", "subject_name",
                     "domain_key", "limit"),
    "read_for_context": ("query", "domain", "task_key"),
    "recent": ("since", "limit"),
    "stats": (),
    "tick": (),
    "scan": ("limit",),
    "record": ("counters", "observation", "injected_ids"),
    "metrics": ("limit",),
    "telemetry": ("limit",),
    "reads": ("limit",),
    "discarded": ("limit",),
    "purge_observations": ("kind",),
}


class RpcError(ValueError):
    pass


def _check_args(op: str, args: dict) -> None:
    allowed = _SPEC[op]
    extra = set(args) - set(allowed)
    if extra:
        raise RpcError(f"{op} 不接受这些参数：{sorted(extra)}；可用：{list(allowed)}")
    missing = [k for k in ("id", "content", "type", "subtype", "ids", "fields")
               if k in allowed and k not in args and _required(op, k)]
    if missing:
        raise RpcError(f"{op} 缺少必填参数：{missing}")


def _required(op: str, key: str) -> bool:
    return {
        "add": {"content", "type", "subtype"},
        "preview": {"content", "type", "subtype"},
        "update": {"id", "fields"},
        "supersede": {"id", "content"},
        "merge": {"ids", "content"},
        "archive": {"id"},
        "delete": {"id"},
        "forget": {"id"},
        "restore": {"id"},
        "purge": {"id"},
        "get": {"id"},
        "explain": {"id"},
        "find_similar": {"content"},
    }.get(op, set()).__contains__(key)


def _call(mem: Memory, op: str, args: dict[str, Any]) -> Any:
    _check_args(op, args)
    if op == "preview":
        return mem.preview(**args)
    if op == "add":
        return mem.add(args.pop("content"), args.pop("type"), args.pop("subtype"), **args)
    if op == "update":
        fields = dict(args.pop("fields"))
        reason = args.pop("reason", "模型修正")
        return mem.update(args.pop("id"), reason=reason, **fields)
    if op == "supersede":
        mid = args.pop("id")
        return mem.supersede(mid, args.pop("content"), **args)
    if op == "merge":
        ids = args.pop("ids")
        return mem.merge(ids, args.pop("content"), **args)
    if op == "archive":
        return mem.archive(args["id"], reason=args.get("reason", "不再需要"))
    if op in ("delete", "forget"):
        return mem.forget(args["id"], reason=args.get("reason", "用户要求忘记"))
    if op == "restore":
        return mem.restore(args["id"], reason=args.get("reason", "恢复"))
    if op == "purge":
        return mem.purge(args["id"], reason=args.get("reason", "物理删除"))
    if op == "get":
        # 查不到不是错误：get 的契约就是"有就返回，没有就 null"。
        # 让这里抛异常会把 404 变成 400，调用方永远分不清"没有"和"出错"。
        return mem.get(args["id"])
    if op == "explain":
        return mem.explain(args["id"])
    if op == "list":
        return mem.list(**args)
    if op == "search":
        return mem.search(**args)
    if op == "find_similar":
        return mem.find_similar(**args)
    if op == "read_for_context":
        return mem.read_for_context(**args)
    if op == "recent":
        return mem.recent(args["since"], args.get("limit", 50))
    if op == "stats":
        return mem.stats()
    if op == "tick":
        return mem.tick()
    if op == "scan":
        limit = args.get("limit", 500)
        return mem.scan(limit=limit if isinstance(limit, int) and limit > 0 else 500)
    if op == "record":
        ids = args.get("injected_ids")
        return mem.record(
            args.get("counters"),
            args.get("observation"),
            ids if isinstance(ids, list) else None,
        )
    if op == "telemetry":
        return mem.telemetry(limit=args.get("limit", 20))
    if op == "reads":
        return mem.reads(limit=args.get("limit", 20))
    if op == "discarded":
        return mem.discarded(limit=args.get("limit", 20))
    if op == "metrics":
        return mem.metrics(limit=args.get("limit", 20))
    if op == "purge_observations":
        return mem.purge_observations(args.get("kind"))
    raise RpcError(f"未知操作：{op}；可用：{sorted(_SPEC)}")


WRITE_OPS = {"add", "update", "supersede", "merge", "forget", "purge", "restore",
             "archive", "delete", "tick"}


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

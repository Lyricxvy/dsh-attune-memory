#!/usr/bin/env python3
"""memctl —— 记忆库命令行管理工具（P9）。

设计约束（来自任务书 P9）：

* **不允许裸 SQL 暴露给用户**。这里的每条命令都只调 `Memory` 的既有 API
  （`list/get/explain/update/forget/restore/stats/tick`），不拼一条 SQL、不暴露表名。
  这样"用户能做的操作"与"写管线允许的操作"是同一套语义：忘记是软删（可恢复），
  真要物理删除必须显式 `forget --hard`，而它仍然只接受已删除的条目。
* **一次操作一个可读结论**。改完打印改了什么，忘记打印"还能恢复"，explain 打印
  版本时间轴。命令行是给人看的，不是给日志看的。

用法::

    cd engine
    python -m attune_memory.memctl list --type profile
    python -m attune_memory.memctl get m-mwfx8m1v-13
    python -m attune_memory.memctl explain m-mwfx8m1v-13
    python -m attune_memory.memctl update m-mwfx8m1v-13 --content "..." --reason "用户更正"
    python -m attune_memory.memctl forget m-mwfx8m1v-13 --reason "用户要求"
    python -m attune_memory.memctl restore m-mwfx8m1v-13
    python -m attune_memory.memctl stats

数据库默认取 ``$DSH_HOME/attune-memory/memory.db``，可用 ``--db`` 覆盖。
所有命令都支持 ``--json`` 输出，便于脚本与人眼各取所需。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持 `python memctl.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from attune_memory import Memory  # noqa: E402


def default_db() -> str:
    home = os.environ.get("DSH_HOME")
    if home:
        return str(Path(home) / "memory" / "memory.db")
    return str(Path.cwd() / "memory.db")


def _cut(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(空)"
    head = ("id", "type", "subtype", "状态", "重要性", "注入", "内容")
    widths = (24, 8, 12, 10, 8, 10)
    out = [
        "  ".join(h.ljust(w) for h, w in zip(head, widths)),
        "  ".join("-" * w for w in widths),
    ]
    for r in rows:
        cells = (
            str(r.get("id", "")),
            str(r.get("type", "")),
            str(r.get("subtype", "")),
            str(r.get("status", "")),
            str(r.get("importance", "")),
            str(r.get("injection_policy", "")),
        )
        out.append(
            "  ".join(c.ljust(w) for c, w in zip(cells, widths))
            + "  "
            + _cut(str(r.get("content", "")), 60)
        )
    return "\n".join(out)


def _explain_text(data: dict[str, Any]) -> str:
    cur = data.get("current") or {}
    lines = [
        f"条目 {cur.get('id')}",
        f"  内容    : {cur.get('content')}",
        f"  类型    : {cur.get('type')}/{cur.get('subtype')}  "
        f"主体={cur.get('subject_type')}"
        + (f":{cur['subject_name']}" if cur.get("subject_name") else ""),
        f"  状态    : {cur.get('status')}（{data.get('state_reason', '')}）",
        f"  注入策略: {cur.get('injection_policy')}   重要性: {cur.get('importance')}   "
        f"置信度: {cur.get('confidence')}",
        f"  作用域  : {cur.get('scope')}   领域: {cur.get('domain_key') or '-'}",
        f"  有效期  : {cur.get('valid_from') or '-'} → {cur.get('valid_until') or '现在'}",
    ]
    counts = data.get("counts") or {}
    lines.append(f"  被注入 {counts.get('injected', 0)} 次，被检索 {counts.get('used', 0)} 次")

    timeline = data.get("timeline") or []
    lines.append("")
    lines.append(f"变更历史（{len(timeline)} 条）:")
    for c in timeline:
        diff = c.get("diff") or {}
        if diff:
            # explain() 的 diff 形如 {"content": {"from": "...", "to": "..."}}
            detail = "；".join(
                f"{k}: {v.get('from')!r} → {v.get('to')!r}" if isinstance(v, dict)
                else f"{k}: {v!r}"
                for k, v in diff.items()
            )
        else:
            detail = "（新建）"
        lines.append(
            f"  {c.get('at')}  {c.get('action')}  by {c.get('actor')}  "
            f"理由: {c.get('reason') or '-'}"
        )
        lines.append(f"      {detail}")

    versions = data.get("versions") or []
    if versions:
        lines.append("")
        lines.append(f"历史版本（{len(versions)} 次内容变更）:")
        for v in versions:
            old = v.get("from")
            new = v.get("to")
            old_text = old.get("content", "") if isinstance(old, dict) else str(old or "")
            new_text = new.get("content", "") if isinstance(new, dict) else str(new or "")
            lines.append(f"  {v.get('at')}  {_cut(old_text, 50)} → {_cut(new_text, 50)}")

    related = data.get("related") or {}

    def _label(item: Any) -> str:
        if isinstance(item, dict):
            return f"{item.get('id')}（{_cut(str(item.get('content', '')), 30)}）"
        return str(item)

    rel: list[str] = []
    if related.get("superseded_by"):
        rel.append("被 " + _label(related["superseded_by"]) + " 取代")
    for item in related.get("supersedes") or []:
        rel.append("取代了 " + _label(item))
    merged = related.get("merged_from") or []
    if merged:
        rel.append("由 " + "、".join(_label(m) for m in merged) + " 合并而来")
    for item in related.get("same_slot_active") or []:
        rel.append("同槽位仍在生效: " + _label(item))
    if rel:
        lines.append("")
        lines.append("关系:")
        lines.extend("  " + r for r in rel)
    return "\n".join(lines)


def _fields_from_args(args: argparse.Namespace) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in ("content", "importance", "injection_policy", "confidence", "scope",
                 "domain_key", "subtype", "keywords"):
        value = getattr(args, name.replace("-", "_"), None)
        if value is None:
            continue
        if name == "keywords":
            value = [k.strip() for k in str(value).split(",") if k.strip()]
        if name == "confidence":
            value = float(value)
        fields[name] = value
    if getattr(args, "reason", None):
        fields["reason"] = args.reason
    return fields


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memctl", description="DeepSeek Harness 记忆库管理")
    parser.add_argument("--db", default=default_db(), help="SQLite 路径（默认 $DSH_HOME/attune-memory/memory.db）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--status", default="active", help="active/archived/deleted/superseded/all")
    p_list.add_argument("--type", default=None)
    p_list.add_argument("--subtype", default=None)
    p_list.add_argument("--domain", default=None)
    p_list.add_argument("--policy", default=None)
    p_list.add_argument("--limit", type=int, default=30)

    p_get = sub.add_parser("get", help="看一条记忆的原始行")
    p_get.add_argument("id")

    p_ex = sub.add_parser("explain", help="解释一条记忆：为什么是现在这样、变过几次")
    p_ex.add_argument("id")

    p_up = sub.add_parser("update", help="改一条记忆（同一件事换说法）")
    p_up.add_argument("id")
    p_up.add_argument("--content", default=None)
    p_up.add_argument("--importance", default=None)
    p_up.add_argument("--injection-policy", default=None)
    p_up.add_argument("--confidence", default=None)
    p_up.add_argument("--scope", default=None)
    p_up.add_argument("--domain-key", default=None)
    p_up.add_argument("--subtype", default=None)
    p_up.add_argument("--keywords", default=None, help="逗号分隔")
    p_up.add_argument("--reason", default="用户手动修改")

    p_forget = sub.add_parser("forget", help="忘记（默认软删，可恢复）")
    p_forget.add_argument("id")
    p_forget.add_argument("--hard", action="store_true", help="物理删除（不可恢复）")
    p_forget.add_argument("--reason", default="用户要求忘记")

    p_restore = sub.add_parser("restore", help="恢复被忘记/归档的记忆")
    p_restore.add_argument("id")
    p_restore.add_argument("--reason", default="用户恢复")

    p_tel = sub.add_parser("telemetry", help="运行时计数与最近观测（只读）")
    p_tel.add_argument("--limit", type=int, default=20)

    p_reads = sub.add_parser("reads", help="最近几次检索的过程数字")
    p_reads.add_argument("--limit", type=int, default=10)

    p_disc = sub.add_parser("discarded", help="被丢弃的候选与理由")
    p_disc.add_argument("--limit", type=int, default=10)
    p_disc.add_argument("--purge", action="store_true", help="清空丢弃日志")

    sub.add_parser("migrate", help="W-3.5：补时态四列并回填（幂等，可反复跑）")
    sub.add_parser("stats", help="库概况")
    sub.add_parser("tick", help="跑一次惰性维护（只归档过期 state 与老 superseded）")
    scan = sub.add_parser("scan", help="维护扫描：列出重复/冲突/可归纳/已过期/同槽多位（只读）")
    scan.add_argument("--limit", type=int, default=500)

    args = parser.parse_args(argv)
    status = args.status if getattr(args, "status", None) not in (None, "all") else None

    with Memory(args.db) as mem:
        if args.cmd == "list":
            rows = mem.list(status=status, type=args.type, subtype=args.subtype,
                            domain_key=args.domain, policy=args.policy, limit=args.limit)
            print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _table(rows))
            return 0

        if args.cmd == "get":
            row = mem.get(args.id)
            if row is None:
                print(f"没有这条记忆: {args.id}", file=sys.stderr)
                return 1
            print(json.dumps(row, ensure_ascii=False, indent=2) if args.json else _table([row]))
            return 0

        if args.cmd == "explain":
            try:
                data = mem.explain(args.id)
            except KeyError:
                print(f"没有这条记忆: {args.id}", file=sys.stderr)
                return 1
            print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else _explain_text(data))
            return 0

        if args.cmd == "update":
            fields = _fields_from_args(args)
            reason = fields.pop("reason")
            try:
                row = mem.update(args.id, reason=reason, **fields)
            except (KeyError, ValueError) as err:
                print(f"改不了: {err}", file=sys.stderr)
                return 1
            print(json.dumps(row, ensure_ascii=False, indent=2) if args.json
                  else f"已改 {row['id']}（{row['status']}）：{row['content']}")
            return 0

        if args.cmd == "forget":
            try:
                if args.hard:
                    mem.forget(args.id, reason=args.reason)
                    mem.purge(args.id, reason=args.reason)
                    print(f"已物理删除 {args.id}（不可恢复）")
                else:
                    row = mem.forget(args.id, reason=args.reason)
                    print(f"已忘记 {row['id']}（状态 {row['status']}，可用 restore 恢复）")
            except (KeyError, ValueError) as err:
                print(f"忘不了: {err}", file=sys.stderr)
                return 1
            return 0

        if args.cmd == "restore":
            try:
                row = mem.restore(args.id, reason=args.reason)
            except (KeyError, ValueError) as err:
                print(f"恢复不了: {err}", file=sys.stderr)
                return 1
            print(f"已恢复 {row['id']}（状态 {row['status']}）：{row['content']}")
            return 0

        if args.cmd == "telemetry":
            data = mem.telemetry(limit=args.limit)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print("计数：")
                for name, value in sorted((data.get("counters") or {}).items()):
                    print(f"  {name:<18} {value}")
                obs = data.get("observations") or []
                print(f"最近观测：{len(obs)} 条")
                for row in obs[:10]:
                    print(f"  {row['at']}  {row['kind']:<10} {row.get('subject') or ''}  {row.get('detail') or ''}")
            return 0

        if args.cmd == "reads":
            rows = mem.reads(limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    groups = row.get("groups") or {}
                    groups_text = "/".join(f"{k}{v}" for k, v in groups.items()) or "-"
                    print(f"{row['at']}  查询「{row.get('query') or ''}」")
                    print(f"  候选 {row.get('candidates', 0)} 条 → 命中 {row.get('hits', 0)} 条 → 注入 {row.get('injected', 0)} 条（{groups_text}）")
                    dropped = row.get("dropped") or []
                    if dropped:
                        head = "；".join(f"{d.get('id')} {d.get('why')}" for d in dropped[:3])
                        print(f"  丢弃 {len(dropped)} 条：{head}")
            return 0

        if args.cmd == "discarded":
            if args.purge:
                n = mem.purge_observations("discarded")
                print(f"已清空丢弃日志（{n} 条）")
                return 0
            rows = mem.discarded(limit=args.limit)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['at']}  「{row.get('content') or ''}」")
                    print(f"  理由：{row.get('reason') or '-'}")
            return 0

        if args.cmd == "migrate":
            from .db import migrate_data

            result = migrate_data(mem.store.conn)
            mem.store.ensure_gram_df()
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    "迁移完成：补列 {added_columns} 个，回填 temporal_kind {filled_temporal_kind} 行，"
                    "回填 invalidation {filled_invalidation} 行（重跑无副作用）".format(**result)
                )
            return 0

        if args.cmd == "stats":
            print(json.dumps(mem.stats(), ensure_ascii=False, indent=2) if args.json
                  else json.dumps(mem.stats(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "tick":
            print(json.dumps(mem.tick(), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "scan":
            report = mem.scan(limit=args.limit)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0
            print(f"扫描 {report['scanned']} 条 active：")
            for key, label in (("duplicates", "重复"), ("conflicts", "可能矛盾"),
                               ("multi_active", "同槽多位"), ("stale", "已过期")):
                items = report.get(key) or []
                print(f"  {label}: {len(items)}")
                for item in items[:5]:
                    print(f"    - {item}")
            print(f"  不变式问题: {len(report['problems'])}")
            return 0
            return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

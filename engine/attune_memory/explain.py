"""可解释性（对齐 MEMORY_LIFECYCLE.md §4）。

``explain(id)`` 回答四个问题：现在是什么、为什么是这个状态、什么时候变的、旧版长什么样。
**所有答案都来自落库数据**，不在这里临时推断——``state_reason`` 是唯一生成物，
因为它是"把时间轴翻译成人话"，不该再存一份副本（否则迟早不一致）。
"""
from __future__ import annotations

from typing import Any

from .store import Store


def _fmt_change(ch: dict) -> dict:
    old = ch.get("old_snapshot") or {}
    new = ch.get("new_snapshot") or {}
    diff: dict[str, Any] = {}
    for key in ("content", "status", "confidence", "importance", "injection_policy",
                "valid_until", "superseded_by", "keywords"):
        a, b = old.get(key), new.get(key)
        if a != b:
            diff[key] = {"from": a, "to": b}
    return {
        "action": ch["action"],
        "at": ch["at"],
        "reason": ch["reason"],
        "actor": ch["actor"],
        "diff": diff,
    }


def _state_reason(mem: dict, timeline: list[dict]) -> str:
    status = mem["status"]
    last = next((t for t in reversed(timeline) if t["diff"].get("status")), None)
    if status == "active":
        if not timeline:
            return "新写入，当前生效中。"
        return "当前生效中。" + (f"最近一次变更：{last['reason']}（{last['at']}）" if last else "")
    if status == "superseded":
        target = mem.get("superseded_by") or "未知条目"
        why = last["reason"] if last else "事实发生变化"
        return f"已被 {target} 取代（{mem.get('valid_until')} 起失效）：{why}"
    if status == "archived":
        why = last["reason"] if last else "不再处于活跃状态"
        return f"已归档，不再参与检索与注入：{why}"
    if status == "deleted":
        why = last["reason"] if last else "用户要求忘记"
        return f"已删除（可恢复）：{why}"
    return f"状态：{status}"


def explain(store: Store, memory_id: str) -> dict:
    mem = store.get(memory_id)
    if mem is None:
        raise KeyError(f"记忆不存在：{memory_id}")
    timeline = [_fmt_change(c) for c in store.changes(memory_id)]

    versions = []
    for ch in timeline:
        d = ch["diff"].get("content")
        # 只看"改措辞"的版本；新增那一条 from 为 None，不算历史版本
        if d and d.get("from"):
            versions.append({"at": ch["at"], "from": d["from"], "to": d["to"],
                             "reason": ch["reason"]})

    related: dict[str, Any] = {}
    if mem.get("superseded_by"):
        related["superseded_by"] = store.get(mem["superseded_by"])
    supersedes = store.conn.execute(
        "SELECT id FROM memories WHERE superseded_by=?", (memory_id,)
    ).fetchall()
    if supersedes:
        related["supersedes"] = [store.get(r["id"]) for r in supersedes]
    merged_from = (mem.get("metadata") or {}).get("merged_from")
    if merged_from:
        related["merged_from"] = store.get_many(merged_from)
    siblings = store.same_slot(
        subject_type=mem["subject_type"], subject_name=mem["subject_name"],
        type=mem["type"], subtype=mem["subtype"], domain_key=mem["domain_key"],
        exclude_id=memory_id, user_id=mem["user_id"],
    )
    if siblings:
        related["same_slot_active"] = siblings

    return {
        "current": mem,
        "state_reason": _state_reason(mem, timeline),
        "timeline": timeline,
        "versions": versions,
        "related": related,
        "counts": {"injected": mem["inject_count"], "used": mem["use_count"]},
    }

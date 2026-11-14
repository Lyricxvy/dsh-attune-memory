"""Memory Manager：对外唯一入口。

Engine 不认识的 harness、不认识 LLM。它只认识"一条记忆"和"一次检索"，
上面这两件事都由 ``Memory`` 这一个门面暴露（P7 的 API 契约）。
"""
from __future__ import annotations

import json

from typing import Any, Iterable

from . import const
from .db import connect, init_schema
from .explain import explain as _explain
from .read import read_for_context as _read_for_context
from .search import find_similar as _find_similar
from .search import search as _search
from .store import InvariantError, Store
from .util import now_iso
from .write import Candidate, Decision, consider, forget, merge as _merge
from .write import purge as _purge
from .write import preview as _preview
from .write import update as _update


class Memory:
    def __init__(self, path: str = ":memory:", *, user_id: str = "default"):
        self.conn = connect(path)
        init_schema(self.conn)
        self.store = Store(self.conn)
        self.user_id = user_id

    # ------------------------------------------------------------ 生命周期

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 写

    def add(self, content: str, type: str, subtype: str, **kwargs: Any) -> dict:  # noqa: A002
        """走完整写入管线。返回 ``Decision.as_dict()``（action 可能是 add/touch/supersede/ignore）。"""
        kwargs.setdefault("user_id", self.user_id)
        cand = Candidate(content=content, type=type, subtype=subtype, **kwargs)
        decision = consider(self.store, cand)
        return decision.as_dict()

    def preview(self, content: str, type: str, subtype: str, **kwargs: Any) -> dict:
        """只算不写：这条候选会被怎么处理（W2 的候选阶段）。"""
        cand = Candidate(content=content, type=type, subtype=subtype, user_id=self.user_id, **kwargs)
        return _preview(self.store, cand)

    def update(self, memory_id: str, **fields: Any) -> dict:
        reason = fields.pop("reason", "用户/模型修正")
        return _update(self.store, memory_id, fields, reason=reason)

    def supersede(self, memory_id: str, content: str, **kwargs: Any) -> dict:
        """用一条新事实取代旧条目（冲突是 feature：旧条目留档并标注被谁取代）。"""
        old = self.get(memory_id)
        if old is None:
            raise InvariantError(f"记忆不存在：{memory_id}")
        payload = dict(
            type=old["type"], subtype=old["subtype"],
            subject_type=old["subject_type"], subject_name=old["subject_name"],
            domain_key=old["domain_key"], scope=old["scope"],
            importance=kwargs.pop("importance", old["importance"]),
            injection_policy=kwargs.pop("injection_policy", old["injection_policy"]),
            confidence=kwargs.pop("confidence", old["confidence"]),
            metadata=kwargs.pop("metadata", old["metadata"]),
            user_id=old["user_id"],
        )
        payload.update(kwargs)
        reason = payload.pop("reason", f"取代 {memory_id}")
        cand = Candidate(content=content, supersede_id=memory_id, **payload)
        with self.store.tx():
            decision = consider(self.store, cand)
        out = decision.as_dict()
        out["reason"] = reason
        return out

    def merge(self, ids: Iterable[str], content: str, *, reason: str = "合并重复事实",
              **kwargs: Any) -> dict:
        return _merge(self.store, list(ids), content=content, reason=reason, **kwargs)

    def archive(self, memory_id: str, *, reason: str = "不再需要") -> dict:
        return self.store.set_status(memory_id, "archived", reason=reason, actor="user", action="archive")

    def delete(self, memory_id: str, *, reason: str = "用户要求忘记") -> dict:
        return forget(self.store, memory_id, reason=reason)

    def forget(self, memory_id: str, *, reason: str = "用户要求忘记") -> dict:
        return self.delete(memory_id, reason=reason)

    def restore(self, memory_id: str, *, reason: str = "恢复") -> dict:
        row = self.store.get(memory_id)
        if row is None:
            raise InvariantError(f"记忆不存在：{memory_id}")
        if row["status"] not in ("archived", "deleted"):
            raise InvariantError(f"{memory_id} 当前 status={row['status']}，只有 archived/deleted 可恢复")
        extra = {"updated_at": now_iso()}
        return self.store.set_status(memory_id, "active", reason=reason, actor="user",
                                     action="restore", extra=extra)

    def purge(self, memory_id: str, *, reason: str = "物理删除") -> dict:
        return _purge(self.store, memory_id, reason=reason)

    # ------------------------------------------------------------ 读

    def search(self, query: str | None = None, **kwargs: Any) -> list[dict]:
        kwargs.setdefault("user_id", self.user_id)
        hits = _search(self.store, query, **kwargs)
        return [
            {
                "id": h["memory"]["id"],
                "type": h["memory"]["type"],
                "subtype": h["memory"]["subtype"],
                "content": h["memory"]["content"],
                "status": h["memory"]["status"],
                "importance": h["memory"]["importance"],
                "injection_policy": h["memory"]["injection_policy"],
                "scope": h["memory"]["scope"],
                "domain_key": h["memory"]["domain_key"],
                "updated_at": h["memory"]["updated_at"],
                "score": h["score"],
                "factors": h["factors"],
                "matched_terms": h["matched_terms"],
                "note": h["note"],
            }
            for h in hits
        ]

    def find_similar(self, content: str, **kwargs: Any) -> list[dict]:
        kwargs.setdefault("user_id", self.user_id)
        hits = _find_similar(self.store, content, **kwargs)
        return [
            {
                "id": h["memory"]["id"],
                "content": h["memory"]["content"],
                "type": h["memory"]["type"],
                "subtype": h["memory"]["subtype"],
                "relation": h["relation"],
                "reason": h["reason"],
            }
            for h in hits
        ]

    def get(self, memory_id: str) -> dict | None:
        return self.store.get(memory_id)

    def list(
        self,
        *,
        status: str | tuple[str, ...] | None = "active",
        type: str | None = None,  # noqa: A002
        subtype: str | None = None,
        domain_key: str | None = None,
        policy: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        rows = self.store.query(
            user_id=self.user_id, status=status, type=type, subtype=subtype,
            domain_key=domain_key, policy=policy, limit=None,
            order="updated_at DESC",
        )
        return rows[offset: offset + limit]

    def count(self, **kwargs: Any) -> int:
        return len(self.list(limit=100000, **kwargs))

    def explain(self, memory_id: str) -> dict:
        return _explain(self.store, memory_id)

    def read_for_context(self, query: str | None = None, **kwargs: Any) -> dict:
        kwargs.setdefault("user_id", self.user_id)
        return _read_for_context(self.store, query, **kwargs)

    def recent(self, since: str, limit: int = 50) -> list[dict]:
        """某个时刻之后的变更（供宿主判断这一轮模型有没有自己写过记忆）。"""
        return self.store.changes_since(since, limit)

    # ------------------------------------------------------------ 维护

    def stats(self) -> dict:
        rows = self.store.conn.execute(
            "SELECT status, type, count(*) AS n FROM memories WHERE user_id=? GROUP BY status, type",
            (self.user_id,),
        ).fetchall()
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
            by_type[r["type"]] = by_type.get(r["type"], 0) + r["n"]
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_type": by_type,
            "problems": self.store.verify_invariants(),
            "schema_version": self.store.meta_get("schema_version"),
        }

    # ------------------------------------------------------------ 遥测与观测

    def record(
        self,
        counters: dict[str, int] | None = None,
        observation: dict | None = None,
        injected_ids: list[str] | None = None,
    ) -> dict:
        """一次 RPC 把计数器、观测、注入记账都记上（减少插件与引擎之间的往返）。

        计数器只做加法；观测只追加；`injected_ids` 是"插件已经把这几条 splice 进
        决策"的回执。三者都不参与读写决策 —— 遥测只观测，不改行为，这是
        "可观测性"与"自适应"的分界线。
        """
        with self.store.tx():
            for name, delta in (counters or {}).items():
                self.store.counter_bump(str(name), int(delta))
            if observation:
                self.store.observe(
                    str(observation.get("kind") or "note"),
                    observation.get("subject"),
                    observation.get("detail"),
                )
            marked = self._mark_injected_in_tx(injected_ids or [])
        return {"counters": self.store.counters(), "marked": marked}

    def _mark_injected_in_tx(self, ids: list[str]) -> int:
        """`inject_count` 自增的实际动作（调用方负责事务）。

        `store.bump` 对不存在的 id 是静默 no-op（UPDATE 命中 0 行），所以必须先
        确认条目存在，否则"记了多少条"会虚高。
        """
        n = 0
        for mid in ids:
            if not mid:
                continue
            if not self.store.get(str(mid)):
                continue
            self.store.bump(str(mid), "inject_count")
            n += 1
        return n

    def mark_injected(self, ids: list[str]) -> int:
        """把"这次真的进了上下文"的条目记账。

        为什么不在 read_for_context 里自增：那个函数也会被"预览"调用（插件刷新缓存时
        传空 query），把预览算成注入会让 inject_count 变成一个没人能解释的数字。
        只有插件在 splice 之后明确回报的那一批 id 才算数。
        """
        with self.store.tx():
            return self._mark_injected_in_tx(ids or [])

    def metrics(self, *, limit: int = 20) -> dict:
        stats = self.stats()
        return {
            "counters": self.store.counters(),
            "observations": self.store.observations(limit=limit),
            "memories": stats,
        }

    def purge_observations(self, kind: str | None = None) -> int:
        return self.store.purge_observations(kind)

    def telemetry(self, *, limit: int = 20) -> dict:
        """模块 6：只读的运行时计数 + 最近观测。"""
        return {
            "counters": self.store.counters(),
            "observations": self.store.observations(limit=limit),
            "memories": self.stats(),
        }

    def reads(self, *, limit: int = 20) -> list[dict]:
        """模块 7：最近几次检索的过程数字（候选数/命中数/被丢弃原因）。"""
        out = []
        for row in self.store.observations(limit=limit, kind="read"):
            try:
                detail = json.loads(row.get("detail") or "{}")
            except Exception:  # noqa: BLE001 - 历史脏数据不该让查询失败
                detail = {}
            out.append({"at": row["at"], "query": row.get("subject"), **detail})
        return out

    def discarded(self, *, limit: int = 20) -> list[dict]:
        """模块 8：被丢弃的候选与理由（只给人看）。"""
        return [
            {"at": r["at"], "content": r.get("subject"), "reason": r.get("detail")}
            for r in self.store.observations(limit=limit, kind="discarded")
        ]

    def request_gc(self, *, keep_days: int = 7, now: str | None = None) -> int:
        return self.store.request_gc(keep_days=keep_days, now=now)

    def scan(self, *, now: str | None = None, limit: int = 500) -> dict:
        """M1 维护扫描：只产出"值得人看一眼"的清单，不改任何数据。

        判定全部是确定性的（不做相似度阈值猜测）：
        重复 = 归一化正文完全相同；矛盾 = 同槽位且命中极性词/同属性不同取值；
        可归纳 = 同槽位多条 active；已过期 = valid_until 早于现在。
        自动维护只允许可逆操作（归档），所以扫描本身只读。
        """
        from .search import _exclusive
        from .text import normalize
        from .util import days_between

        now = now or now_iso()
        rows = self.store.query(user_id=self.user_id, status="active", limit=limit) or []
        by_content: dict[str, list[dict]] = {}
        for m in rows:
            by_content.setdefault(normalize(m["content"]), []).append(m)
        duplicates = [
            {"content": group[0]["content"], "ids": [m["id"] for m in group]}
            for group in by_content.values() if len(group) > 1
        ]
        slots: dict[tuple, list[dict]] = {}
        for m in rows:
            key = (m["subject_type"], m["subject_name"], m["type"], m["subtype"], m["domain_key"])
            slots.setdefault(key, []).append(m)
        conflicts: list[dict] = []
        multi_active: list[dict] = []
        for key, group in slots.items():
            if len(group) < 2:
                continue
            multi_active.append({"slot": list(key), "ids": [m["id"] for m in group]})
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if _exclusive(group[i], group[j]):
                        conflicts.append({"a": group[i]["id"], "b": group[j]["id"],
                                          "reason": "同槽位且取值互斥"})
        stale = [
            {"id": m["id"], "valid_until": m["valid_until"], "content": m["content"][:60]}
            for m in rows if m.get("valid_until") and days_between(m["valid_until"], now) > 0
        ]
        # M3 Formation（只给候选）：共享关键词的条目连成一片，就是"同一个主题"。
        # 只生成候选清单，不新建记忆、不改状态（Agentic Formation 留给 V2）。
        parent: dict[str, str] = {m["id"]: m["id"] for m in rows}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        from .text import bigrams

        grams_of: dict[str, set[str]] = {m["id"]: set(bigrams(m["content"])) for m in rows}
        index: dict[str, list[str]] = {}
        for mid, grams in grams_of.items():
            for g in grams:
                index.setdefault(g, []).append(mid)
        # 出现在所有条目里的片段（比如"用户"）不区分任何东西，不能拿来聚类。
        n = len(rows)
        for ids in index.values():
            if len(ids) < 2 or len(ids) == n:
                continue
            for other in ids[1:]:
                union(ids[0], other)
        groups: dict[str, list[str]] = {}
        for m in rows:
            groups.setdefault(find(m["id"]), []).append(m["id"])
        formations = []
        for ids in groups.values():
            if len(ids) < 3:
                continue
            shared: dict[str, int] = {}
            for mid in ids:
                for g in grams_of[mid]:
                    shared[g] = shared.get(g, 0) + 1
            top = sorted((k for k, c in shared.items() if c >= max(2, len(ids) // 2)),
                         key=lambda k: -shared[k])[:6]
            formations.append({"keywords": top, "ids": ids, "size": len(ids)})
        formations.sort(key=lambda f: (-f["size"], f["keywords"]))
        formations = formations[:10]
        report = {
            "at": now,
            "scanned": len(rows),
            "duplicates": duplicates[:20],
            "conflicts": conflicts[:20],
            "multi_active": multi_active[:20],
            "stale": stale[:20],
            "formations": formations,
            "problems": self.store.verify_invariants(),
        }
        try:
            self.store.observe("maintenance", subject="scan",
                               detail=json.dumps({k: (len(v) if isinstance(v, list) else v)
                                                  for k, v in report.items()}, ensure_ascii=False),
                               now=now)
        except Exception:  # noqa: BLE001
            pass
        return report

    def tick(self, *, now: str | None = None) -> dict:
        """惰性维护（P6 §5）：只做两条绝对安全的自动转换，绝不自动删/合并/降权。"""
        now = now or now_iso()
        archived: list[str] = []
        # state 过期 7 天后归档（给"刚过期但还在讨论"留出余量）
        for m in self.store.query(user_id=self.user_id, status="active", type="state"):
            if not m.get("valid_until"):
                continue
            from .util import days_between

            if days_between(m["valid_until"], now) >= 7:
                self.store.set_status(m["id"], "archived",
                                      reason="状态已过期 7 天", actor="system", action="archive")
                archived.append(m["id"])
        # superseded 满 180 天归档
        for m in self.store.query(user_id=self.user_id, status="superseded"):
            from .util import days_between

            if m.get("valid_until") and days_between(m["valid_until"], now) >= 180:
                self.store.set_status(m["id"], "archived",
                                      reason="被取代满 180 天", actor="system", action="archive")
                archived.append(m["id"])
        self.store.meta_set("last_maintenance_at", now)
        return {"archived": archived, "at": now}


__all__ = ["Memory", "Candidate", "Decision", "const"]

"""存储层：memories / memory_change / memory_meta 的读写与不变式校验。

这一层不认识"语义"（不管该不该记、该不该取代），只保证：
1. 写进去的行满足 P2 §7 的五条不变式；
2. 每次状态/内容变化都留一行 ``memory_change``；
3. 读出来的行形状稳定（``metadata`` 永远是 dict）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from . import const
from .util import now_iso


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务：``isolation_level=None`` 下 ``with conn`` 不会开事务。

    P3 §9 要求"写记忆 + 写时间轴"在同一事务里；并发写入靠 ``BEGIN IMMEDIATE``
    串行化（不是靠检查锁文件——那是 TOCTOU）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

FIELDS = (
    "id", "user_id", "type", "subtype", "subject_type", "subject_name", "domain_key",
    "content", "keywords", "confidence", "importance", "injection_policy", "scope",
    "status", "source_message_id", "source_conversation_id", "valid_from", "valid_until",
    "superseded_by", "created_at", "updated_at", "metadata", "inject_count", "use_count",
)


class InvariantError(ValueError):
    """违反 P2 §7 的不变式（或调用了非法状态转移）。"""


def _dump_meta(meta: dict | str | None) -> str:
    if meta is None:
        return "{}"
    if isinstance(meta, str):
        return meta
    return json.dumps(meta, ensure_ascii=False, sort_keys=True)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("metadata") or "{}"
    try:
        d["metadata"] = json.loads(raw)
    except (TypeError, ValueError):
        d["metadata"] = {}
    return d


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._depth = 0

    @contextmanager
    def tx(self):
        """可重入事务：嵌套调用合并进最外层事务。

        写入管线需要"旧条目标 superseded + 新条目 insert"原子完成，
        而每个 Store 方法自己也开事务，所以必须可重入。
        """
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    # ------------------------------------------------------------ 读

    def get(self, memory_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return row_to_dict(row) if row else None

    def get_many(self, ids: Iterable[str]) -> list[dict]:
        ids = list(ids)
        if not ids:
            return []
        q = "SELECT * FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids))
        return [row_to_dict(r) for r in self.conn.execute(q, ids).fetchall()]

    def query(
        self,
        *,
        user_id: str = "default",
        status: str | tuple[str, ...] | None = "active",
        type: str | None = None,
        subtype: str | None = None,
        subject_type: str | None = None,
        subject_name: str | None = None,
        domain_key: str | None = None,
        policy: str | None = None,
        limit: int | None = None,
        order: str = "updated_at DESC",
    ) -> list[dict]:
        sql = ["SELECT * FROM memories WHERE user_id=?"]
        args: list[Any] = [user_id]
        if status:
            if isinstance(status, tuple):
                sql.append("AND status IN (%s)" % ",".join("?" * len(status)))
                args.extend(status)
            else:
                sql.append("AND status=?")
                args.append(status)
        for col, val in (
            ("type", type),
            ("subtype", subtype),
            ("subject_type", subject_type),
            ("subject_name", subject_name),
            ("domain_key", domain_key),
            ("injection_policy", policy),
        ):
            if val is not None:
                sql.append(f"AND {col}=?")
                args.append(val)
        sql.append(f"ORDER BY {order}")
        if limit:
            sql.append("LIMIT ?")
            args.append(limit)
        return [row_to_dict(r) for r in self.conn.execute(" ".join(sql), args).fetchall()]

    def same_slot(
        self,
        *,
        subject_type: str,
        subject_name: str | None,
        type: str,
        subtype: str,
        domain_key: str | None,
        exclude_id: str | None = None,
        user_id: str = "default",
    ) -> list[dict]:
        """同槽位 + active 的全部条目（P3 §2.2 的槽位定义）。"""
        sql = (
            "SELECT * FROM memories WHERE user_id=? AND subject_type=? AND status='active'"
            " AND type=? AND subtype=? AND domain_key IS ?"
        )
        args: list[Any] = [user_id, subject_type, type, subtype, domain_key]
        if subject_name is None:
            sql += " AND subject_name IS NULL"
        else:
            sql += " AND subject_name=?"
            args.append(subject_name)
        if exclude_id:
            sql += " AND id<>?"
            args.append(exclude_id)
        return [row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def near_keywords(
        self, terms: list[str], *, limit: int = 20, exclude_id: str | None = None,
        user_id: str = "default",
    ) -> list[dict]:
        """关键词近邻：任何一条 term 出现在 keywords 或 content 里（Python 侧过滤）。"""
        if not terms:
            return []
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE user_id=? AND status='active'"
            + (" AND id<>?" if exclude_id else ""),
            ([user_id, exclude_id] if exclude_id else [user_id]),
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            hay = (d.get("keywords") or "") + " " + (d.get("content") or "")
            if any(t and t in hay for t in terms):
                out.append(d)
        return out[:limit]

    # ------------------------------------------------------------ 写

    def insert(self, row: dict, *, reason: str, actor: str) -> dict:
        data = dict(row)
        data["metadata"] = _dump_meta(data.get("metadata"))
        data.setdefault("user_id", "default")
        data.setdefault("inject_count", 0)
        data.setdefault("use_count", 0)
        cols = ",".join(FIELDS)
        marks = ",".join("?" * len(FIELDS))
        with self.tx():
            self.conn.execute(f"INSERT INTO memories ({cols}) VALUES ({marks})", [data[c] for c in FIELDS])
            self._add_change(
                memory_id=data["id"], action="add", old=None, new=data,
                reason=reason, actor=actor, source_message_id=data.get("source_message_id"),
            )
        saved = self.get(data["id"])
        assert saved is not None
        return saved

    def update_fields(self, memory_id: str, fields: dict, *, reason: str, actor: str,
                      action: str = "update") -> dict:
        before = self.get(memory_id)
        if before is None:
            raise InvariantError(f"记忆不存在：{memory_id}")
        payload = dict(fields)
        if "metadata" in payload:
            payload["metadata"] = _dump_meta(payload["metadata"])
        if not payload:
            return before
        sets = ",".join(f"{k}=?" for k in payload)
        with self.tx():
            self.conn.execute(
                f"UPDATE memories SET {sets} WHERE id=?", [*payload.values(), memory_id]
            )
            after = self.get(memory_id)
            assert after is not None
            self.check_row(after)
            self._add_change(
                memory_id=memory_id, action=action, old=before, new=after, reason=reason,
                actor=actor, source_message_id=after.get("source_message_id"),
            )
        return self.get(memory_id)  # type: ignore[return-value]

    def set_status(self, memory_id: str, status: str, *, reason: str, actor: str,
                   extra: dict | None = None, action: str | None = None) -> dict:
        if status not in const.STATUSES:
            raise InvariantError(f"非法状态：{status}")
        before = self.get(memory_id)
        if before is None:
            raise InvariantError(f"记忆不存在：{memory_id}")
        fields = {"status": status, "updated_at": now_iso()}
        if extra:
            fields.update(extra)
        return self.update_fields(
            memory_id, fields, reason=reason, actor=actor, action=action or _status_action(status)
        )

    def bump(self, memory_id: str, field: str) -> None:
        if field not in ("inject_count", "use_count"):
            raise InvariantError(f"不可自增的字段：{field}")
        with self.tx():
            self.conn.execute(
                f"UPDATE memories SET {field}={field}+1 WHERE id=?", (memory_id,)
            )

    # ------------------------------------------------------------ 变更流水

    def _add_change(self, *, memory_id: str, action: str, old: dict | None, new: dict | None,
                    reason: str, actor: str, source_message_id: str | None) -> None:
        self.conn.execute(
            "INSERT INTO memory_change(memory_id, action, old_snapshot, new_snapshot, reason, actor,"
            " source_message_id, at) VALUES (?,?,?,?,?,?,?,?)",
            (
                memory_id,
                action,
                json.dumps(_snapshot(old), ensure_ascii=False) if old else None,
                json.dumps(_snapshot(new), ensure_ascii=False) if new else None,
                reason,
                actor,
                source_message_id,
                now_iso(),
            ),
        )

    def changes(self, memory_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM memory_change WHERE memory_id=? ORDER BY id", (memory_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("old_snapshot", "new_snapshot"):
                if d[k]:
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            out.append(d)
        return out

    # ------------------------------------------------------------ meta

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT v FROM memory_meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def changes_since(self, since: str, limit: int = 50) -> list[dict]:
        """某时刻之后的变更流水（不含快照，只给插件判断"这轮模型自己写过没有"用）。

        为什么要按 actor 区分：主通道（模型工具）写的是 `actor='user'`，兜底抽取写的是
        `actor='inferred'`。同一轮里模型已经记过的东西，抽取通道就不该再补一遍 ——
        这是 P8 真跑时发现的重复写入（同一条身份记了两遍，一个 0.95 一个 0.65）。
        """
        rows = self.conn.execute(
            "SELECT id, memory_id, action, reason, actor, source_message_id, at"
            " FROM memory_change WHERE at >= ? ORDER BY id DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def meta_set(self, key: str, value: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO memory_meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )

    # ------------------------------------------------------------ 遥测

    def counter_bump(self, name: str, delta: int = 1, now: str | None = None) -> int:
        """累加一个计数器并返回新值（只做加法，永不重算，避免"估算"污染事实）。"""
        from .util import now_iso

        with self.tx():
            self.conn.execute(
                "INSERT INTO memory_counter(name, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET value = value + excluded.value, updated_at = excluded.updated_at",
                (name, int(delta), now or now_iso()),
            )
        row = self.conn.execute("SELECT value FROM memory_counter WHERE name=?", (name,)).fetchone()
        return int(row["value"]) if row else 0

    def counters(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT name, value FROM memory_counter ORDER BY name").fetchall()
        return {r["name"]: int(r["value"]) for r in rows}

    def observe(self, kind: str, subject: str | None = None, detail: str | None = None, now: str | None = None) -> None:
        """记一条观测（只增不改，不进模型上下文）。

        为什么单独一张表而不是写日志文件：观测要能和记忆变更一起被审计
        （比如"注入消失了，但当时没有压缩"这条结论必须可查），且要能一键清空。
        """
        from .util import now_iso

        with self.tx():
            self.conn.execute(
                "INSERT INTO memory_observation(kind, subject, detail, at) VALUES (?,?,?,?)",
                (kind, subject, detail, now or now_iso()),
            )

    def observations(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.conn.execute(
                "SELECT id, kind, subject, detail, at FROM memory_observation WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, kind, subject, detail, at FROM memory_observation ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 幂等表

    def request_get(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT key, op, response, created_at FROM memory_request WHERE key=?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def request_put(self, key: str, op: str, response: str, now: str | None = None) -> None:
        """记下一次成功写入的响应，供同一 dedupe 键的重试原样取回。"""
        from .util import now_iso

        with self.tx():
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_request(key, op, response, created_at) VALUES (?,?,?,?)",
                (key, op, response, now or now_iso()),
            )

    def request_gc(self, keep_days: int = 7, now: str | None = None) -> int:
        """清掉过期的幂等记录：它不是审计账本，只是"重试窗口"的记忆。"""
        from datetime import timedelta

        from .util import now_iso, parse_iso

        current = parse_iso(now) if now else parse_iso(now_iso())
        cutoff = (current - timedelta(days=int(keep_days))).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.tx():
            cur = self.conn.execute("DELETE FROM memory_request WHERE created_at < ?", (cutoff,))
        return cur.rowcount

    def trim_observations(self, kind: str, keep: int = 200) -> int:
        """只保留某类观测最近的 N 条。

        观测是"最近发生了什么"，不是审计账本；它会长到几十万条的唯一原因是没人裁。
        每次写入后裁一次，代价是一条 DELETE，换来库不随时间膨胀。
        """
        with self.tx():
            cur = self.conn.execute(
                "DELETE FROM memory_observation WHERE kind=? AND id NOT IN "
                "(SELECT id FROM memory_observation WHERE kind=? ORDER BY id DESC LIMIT ?)",
                (kind, kind, int(keep)),
            )
        return cur.rowcount

    def purge_observations(self, kind: str | None = None) -> int:
        with self.tx():
            if kind:
                cur = self.conn.execute("DELETE FROM memory_observation WHERE kind=?", (kind,))
            else:
                cur = self.conn.execute("DELETE FROM memory_observation")
        return cur.rowcount

    # ------------------------------------------------------------ 不变式

    def check_row(self, row: dict) -> None:
        """P2 §7 的五条不变式 + 四条语义约束。违反即抛，不静默修。"""
        if row["type"] not in const.TYPES:
            raise InvariantError(f"非法 type：{row['type']}")
        if row["subtype"] not in const.SUBTYPES[row["type"]]:
            raise InvariantError(f"{row['type']} 不支持 subtype：{row['subtype']}")
        if row["subject_type"] not in const.SUBJECT_TYPES:
            raise InvariantError(f"非法 subject_type：{row['subject_type']}")
        if row["importance"] not in const.IMPORTANCE:
            raise InvariantError(f"非法 importance：{row['importance']}")
        if row["injection_policy"] not in const.POLICIES:
            raise InvariantError(f"非法 injection_policy：{row['injection_policy']}")
        if row["scope"] not in const.SCOPES:
            raise InvariantError(f"非法 scope：{row['scope']}")
        if row["status"] not in const.STATUSES:
            raise InvariantError(f"非法 status：{row['status']}（created/updated 不是状态值，见 P6 §1）")
        if not (0.0 <= float(row["confidence"]) <= 1.0):
            raise InvariantError("confidence 必须在 [0,1]")
        if row["status"] == "superseded" and not (row.get("superseded_by") and row.get("valid_until")):
            raise InvariantError("superseded 必须同时有 superseded_by 与 valid_until")
        if row["type"] == "state" and not row.get("valid_from"):
            raise InvariantError("state 必须有 valid_from")
        if row["subject_type"] != "user" and row["injection_policy"] == "always":
            raise InvariantError("非用户主体的记忆不允许 always 注入（防污染 User Model）")
        if row["scope"] == "domain" and not row.get("domain_key"):
            raise InvariantError("scope=domain 时 domain_key 必填")
        if row["scope"] == "task" and not (row.get("metadata") or {}).get("task_key"):
            raise InvariantError("scope=task 时 metadata.task_key 必填")

    def verify_invariants(self) -> list[str]:
        """全库扫描，返回违规描述列表（测试与管理界面用）。"""
        problems: list[str] = []
        for row in self.conn.execute("SELECT * FROM memories").fetchall():
            d = row_to_dict(row)
            try:
                self.check_row(d)
            except InvariantError as exc:
                problems.append(f"{d['id']}: {exc}")
        for row in self.conn.execute(
            "SELECT m.id, m.superseded_by, s.status AS target_status FROM memories m"
            " LEFT JOIN memories s ON s.id = m.superseded_by"
            " WHERE m.superseded_by IS NOT NULL"
        ).fetchall():
            if row["target_status"] not in ("active", "archived"):
                problems.append(
                    f"{row['id']}: superseded_by 指向 {row['superseded_by']}"
                    f"（status={row['target_status']}），链条必须指向 active/archived"
                )
        for row in self.conn.execute(
            "SELECT subject_type, subject_name, type, subtype, domain_key, count(*) AS n"
            " FROM memories WHERE status='active' AND type='state'"
            " GROUP BY subject_type, subject_name, type, subtype, domain_key"
            " HAVING n > 1"
        ).fetchall():
            problems.append(
                f"state 槽位存在多条 active：{row['subject_name']}/{row['subtype']}/{row['domain_key']}"
                f"（n={row['n']}），同槽位互斥现状只能有一条"
            )
        return problems


def _snapshot(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: row.get(k) for k in FIELDS}
    if isinstance(out.get("metadata"), dict):
        out["metadata"] = json.dumps(out["metadata"], ensure_ascii=False, sort_keys=True)
    return out


def _status_action(status: str) -> str:
    return {
        "active": "restore",
        "superseded": "supersede",
        "archived": "archive",
        "deleted": "delete",
    }[status]

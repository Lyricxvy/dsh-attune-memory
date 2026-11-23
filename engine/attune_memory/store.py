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
from .text import grams
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
    # v0.3.0（W-3.1）
    "temporal_kind", "invalidation", "event_date", "last_verified",
)


# 单个二元组的倒排链最多取这么多候选：df 已经是最小的那个 gram，真命中必然在链上，
# 取前 MAX_POSTING 条是为了给"极端公共词"设一个硬上限，避免退化时读整张表。
MAX_POSTING = 5000

# df 超过这个数就不走倒排链，改走"最近优先"扫描（见 near_keywords）。
COMMON_DF = 200


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
        task_key: str | None = None,
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
        if task_key is not None:
            # task 作用域的键存在 metadata.task_key 里（不变式要求必填）。
            # 用 json_extract 而不是 LIKE：LIKE 会把 task_key=A 匹配到 task_key=AB。
            sql.append("AND (scope<>'task' OR json_extract(metadata,'$.task_key')=?)")
            args.append(task_key)
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
        limit: int | None = None,
    ) -> list[dict]:
        """同槽位 + active 的条目（P3 §2.2 的槽位定义）。

        ``limit`` 按最近更新取前 N 条（W-2.3）：槽位是"同一主体同一类事实"，
        随时间能长到几百上千条；调用方（写入决策）只需要看最近的那些，
        不封顶就会退化成 O(n²) 的整槽位读取 + 排序。
        """
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
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def count_slot(
        self,
        *,
        subject_type: str,
        subject_name: str | None,
        type: str,
        subtype: str,
        domain_key: str | None,
        user_id: str = "default",
    ) -> int:
        """同槽位 active 的总条数（不用把行读出来，O(log n)）。"""
        sql = (
            "SELECT count(*) AS n FROM memories WHERE user_id=? AND subject_type=?"
            " AND status='active' AND type=? AND subtype=? AND domain_key IS ?"
        )
        args: list[Any] = [user_id, subject_type, type, subtype, domain_key]
        sql += " AND subject_name IS NULL" if subject_name is None else " AND subject_name=?"
        if subject_name is not None:
            args.append(subject_name)
        return int(self.conn.execute(sql, args).fetchone()["n"])

    def _index_terms(self, row: dict) -> set[str]:
        """一条记忆的索引项：keywords 与正文的二元组（normalized 后的）。"""
        hay = f"{row.get('keywords') or ''} {row.get('content') or ''}"
        return set(grams(hay)) or set()

    # ------------------------------------------------------ 倒排（W-2.3）
    #
    # 倒排本身只让"多字 gram 的查找"变成 O(log n + 命中数)，但 near_keywords
    # 原先按 term 做 `gram IN (所有二元组) GROUP BY memory_id HAVING count=len`，
    # SQLite 仍要扫完**每一个**二元组的整个倒排链：模板正文里的"要点""独立"
    # 这类公共二元组行数随 n 线性增长 → 每次写入 O(n)（实测 2000 条时 7.8 ms/写）。
    #
    # 取"每个 term 里 df（文档频次）最小的那个二元组"做候选入口即可：
    # 真命中必然包含该二元组（是子串的必要条件），候选集是超集，
    # 再交给后面的 instr 子串校验。df 存在 memory_gram_df 里，O(1) 查。

    def _df_inc(self, terms: Iterable[str]) -> None:
        rows = [(g,) for g in terms]
        if rows:
            self.conn.executemany(
                "INSERT INTO memory_gram_df(gram, df) VALUES (?, 1)"
                " ON CONFLICT(gram) DO UPDATE SET df = df + 1",
                rows,
            )

    def _df_dec(self, terms: Iterable[str]) -> None:
        rows = [(g,) for g in terms]
        if rows:
            self.conn.executemany("UPDATE memory_gram_df SET df = df - 1 WHERE gram = ?", rows)
            self.conn.execute("DELETE FROM memory_gram_df WHERE df <= 0")

    def _reindex(self, memory_id: str) -> None:
        """重建一条记忆的倒排项（insert/update 时调用，必须在 tx 内）。"""
        old = [
            r["gram"]
            for r in self.conn.execute(
                "SELECT gram FROM memory_gram WHERE memory_id=?", (memory_id,)
            ).fetchall()
        ]
        if old:
            self.conn.execute("DELETE FROM memory_gram WHERE memory_id=?", (memory_id,))
            self._df_dec(old)
        row = self.get(memory_id)
        if row is None:
            return
        terms = self._index_terms(row)
        if terms:
            self.conn.executemany(
                "INSERT OR IGNORE INTO memory_gram(gram, memory_id, user_id) VALUES (?,?,?)",
                [(g, memory_id, row["user_id"]) for g in terms],
            )
            self._df_inc(sorted(terms))

    def rebuild_index(self) -> int:
        """全量重建倒排（索引可从 memories 重建，坏了不丢数据）。返回索引条数。"""
        with self.tx():
            self.conn.execute("DELETE FROM memory_gram")
            self.conn.execute("DELETE FROM memory_gram_df")
            ids = [r["id"] for r in self.conn.execute("SELECT id FROM memories").fetchall()]
            for memory_id in ids:
                self._reindex(memory_id)
        return len(self.conn.execute("SELECT 1 FROM memory_gram").fetchall())

    def ensure_gram_df(self) -> bool:
        """老库（v4 之前）没有 df 表时补一次重建；已就绪时是 O(1)。"""
        if self.conn.execute("SELECT 1 FROM memory_gram_df LIMIT 1").fetchone() is not None:
            return False
        if self.conn.execute("SELECT 1 FROM memory_gram LIMIT 1").fetchone() is None:
            return False
        self.rebuild_index()
        return True

    def gram_df_map(self, grams_list: Iterable[str]) -> dict[str, int]:
        """批量取二元组的 df（行数）。缺省视为 0（没进过倒排）。"""
        wanted = sorted({g for g in grams_list if g})
        out: dict[str, int] = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                f"SELECT gram, df FROM memory_gram_df WHERE gram IN ({ph})", chunk
            ).fetchall():
                out[r["gram"]] = int(r["df"])
        return out

    def candidates_by_terms(
        self,
        terms: Iterable[str],
        *,
        user_id: str = "default",
        limit: int = 200,
        max_df: int = COMMON_DF,
        statuses: Iterable[str] = ("active",),
    ) -> list[dict]:
        """按查询词从倒排取候选（W-2.3 收尾：检索也走索引）。

        旧实现 ``build_candidates`` 取 ``ORDER BY updated_at DESC LIMIT 200``——库里
        三万条时，200 条全是"最近写的"，真正相关的旧记忆根本不在候选集里。本地复现：
        501 条时 query ``What degree did I graduate with?`` 命中 0 条。

        这里改成"按词查倒排、按共现 gram 数排序"。``max_df`` 以上的公共 gram 不进
        ``IN (...)``——那条链接近整张表，与 ``near_keywords`` 的 COMMON_DF 同一思路。
        """
        terms = [t for t in dict.fromkeys(terms) if t]
        if not terms:
            return []
        per_term = [(t, sorted(set(grams(t)))) for t in terms]
        all_grams = sorted({g for _t, gs in per_term for g in gs})
        df = self.gram_df_map(all_grams)
        wanted = {g for g in all_grams if 0 < df.get(g, 0) <= max_df}
        if not wanted:
            return []
        grams_list = sorted(wanted)
        holes = ",".join("?" * len(grams_list))
        sql = (
            "SELECT memory_id, COUNT(DISTINCT gram) AS c FROM memory_gram "
            f"WHERE user_id = ? AND gram IN ({holes}) "
            "GROUP BY memory_id ORDER BY c DESC, memory_id DESC LIMIT ?"
        )
        ids = [
            r["memory_id"]
            for r in self.conn.execute(sql, (user_id, *grams_list, int(limit))).fetchall()
        ]
        if not ids:
            return []
        allowed = set(statuses)
        by_id = {m["id"]: m for m in self.get_many(ids) if m["status"] in allowed}
        return [by_id[i] for i in ids if i in by_id]

    def near_keywords(
        self, terms: list[str], *, limit: int = 20, exclude_id: str | None = None,
        user_id: str = "default",
    ) -> list[dict]:
        """关键词近邻：任何一条 term 出现在 keywords 或 content 里。

        v0.3.0（W-2.3）从"全表扫 + Python 子串匹配"换成倒排索引：
        term 拆成二元组 → 找"这些二元组全都有"的记忆 → 回表做一次子串校验。
        结果与全表扫一致（二元组全命中是子串的必要条件），但不再读整张表——
        这是 LongMemEval 完整 haystack 单题 >3 分钟的直接原因（#38）。
        """
        terms = [t for t in (terms or []) if t]
        if not terms:
            return []

        self.ensure_gram_df()
        # 每个 term 只用"df 最小的那个二元组"作入口（见上方注释），并且**在 SQL 里
        # 就地做 instr 子串校验**：不要先把候选 id 拉成 Python 集合再回表——那样
        # 公共二元组的倒排链会把几百上千个 id 拽回来，每次写入都要扫一遍。
        pairs = [(t, sorted(set(grams(t)))) for t in terms]
        wanted = sorted({g for _t, gs in pairs for g in gs})
        df: dict[str, int] = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                f"SELECT gram, df FROM memory_gram_df WHERE gram IN ({ph})", chunk
            ).fetchall():
                df[r["gram"]] = int(r["df"])

        candidates: set[str] = set()
        for t, gs in pairs:
            if len(t) == 1:
                # 单字词进不了二元组索引，只能兜底；用 instr 让它走同一套判据。
                for r in self.conn.execute(
                    "SELECT id FROM memories WHERE user_id=? AND status='active'"
                    " AND (instr(keywords, ?) > 0 OR instr(content, ?) > 0)"
                    " ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (user_id, t, t, int(limit)),
                ).fetchall():
                    candidates.add(r["id"])
                continue
            if not gs:
                continue
            scored = [(df.get(g, 0), g) for g in gs]
            best_df, best = min(scored)
            if best_df <= 0:
                continue
            if best_df > COMMON_DF:
                # 太公共的二元组（比如"要点"出现在每条正文里）不该走倒排链：
                # 那条链就是整张表。改走 idx_mem_updated 的"最近优先"扫描，
                # 命中率极高，凑够 limit 条就停，代价是 O(limit) 而不是 O(df)。
                rows = self.conn.execute(
                    "SELECT id FROM memories INDEXED BY idx_mem_updated"
                    " WHERE user_id = ? AND status = 'active'"
                    " AND (instr(keywords, ?) > 0 OR instr(content, ?) > 0)"
                    " ORDER BY updated_at DESC, id DESC LIMIT ?",
                    (user_id, t, t, int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT m.id FROM memory_gram g JOIN memories m ON m.id = g.memory_id"
                    " WHERE g.gram = ? AND m.user_id = ? AND m.status = 'active'"
                    " AND (instr(m.keywords, ?) > 0 OR instr(m.content, ?) > 0)"
                    " ORDER BY m.updated_at DESC, m.id DESC LIMIT ?",
                    (best, user_id, t, t, int(limit)),
                ).fetchall()
            for r in rows:
                candidates.add(r["id"])

        if not candidates:
            return []
        if exclude_id:
            candidates.discard(exclude_id)
        if not candidates:
            return []
        ids = list(candidates)
        ph = ",".join("?" * len(ids))
        out = [
            row_to_dict(r)
            for r in self.conn.execute(
                f"SELECT * FROM memories WHERE user_id=? AND status='active'"
                f" AND id IN ({ph}) ORDER BY updated_at DESC, id DESC LIMIT ?",
                [user_id, *ids, int(limit)],
            ).fetchall()
        ]
        out.sort(key=lambda m: (m.get("updated_at") or "", m["id"]), reverse=True)
        return out[:limit]

    # ------------------------------------------------------------ 写

    def insert(self, row: dict, *, reason: str, actor: str) -> dict:
        data = dict(row)
        data["metadata"] = _dump_meta(data.get("metadata"))
        data.setdefault("user_id", "default")
        data.setdefault("inject_count", 0)
        data.setdefault("use_count", 0)
        data.setdefault("temporal_kind", "current")
        data.setdefault("invalidation", None)
        data.setdefault("event_date", None)
        data.setdefault("last_verified", None)
        cols = ",".join(FIELDS)
        marks = ",".join("?" * len(FIELDS))
        with self.tx():
            self.conn.execute(f"INSERT INTO memories ({cols}) VALUES ({marks})", [data[c] for c in FIELDS])
            self._reindex(data["id"])
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
            if {"content", "keywords"} & set(payload):
                self._reindex(memory_id)
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
        """全库扫描，返回违规描述列表（测试与管理界面用）。

        v0.3.0（W-2.4）：三条修正——
        1. 所有检查按 ``user_id`` 分组（#37：多用户时 A 的槽位不该和 B 的串在一起比）；
        2. "同槽位多条 active"从 state 扩展到"单值语义"的类型（identity/decision），
           但 preference 这类天然多值的不算问题（同一个槽位本来就允许并存多条偏好）；
        3. 倒排索引完整性（索引坏了不丢数据，但要能发现）。
        """
        problems: list[str] = []
        for row in self.conn.execute("SELECT * FROM memories").fetchall():
            d = row_to_dict(row)
            try:
                self.check_row(d)
            except InvariantError as exc:
                problems.append(f"{d['id']}: {exc}")

        # 取代链：目标必须属于同一个 user（跨用户指向说明写入时串了人）
        for row in self.conn.execute(
            "SELECT m.id, m.user_id, m.superseded_by, s.status AS target_status,"
            " s.user_id AS target_user FROM memories m"
            " LEFT JOIN memories s ON s.id = m.superseded_by"
            " WHERE m.superseded_by IS NOT NULL"
        ).fetchall():
            if row["target_status"] not in ("active", "archived"):
                problems.append(
                    f"{row['id']}: superseded_by 指向 {row['superseded_by']}"
                    f"（status={row['target_status']}），链条必须指向 active/archived"
                )
            elif row["target_user"] != row["user_id"]:
                problems.append(
                    f"{row['id']}: superseded_by 指向了另一个用户的记忆"
                    f"（{row['user_id']} → {row['target_user']}）"
                )

        # 单值槽位（W-4.1）：同 user 同槽位的多条 active，只有**取值互斥**才算问题。
        #
        # 旧口径是"该槽位只能有一条"——可 state 槽位天然可以同时有多条真状态
        # （备考数学一 + 在外地实习）。真正要抓的是"数学 120 分"与"数学 130 分"
        # 这种同一属性两个值。判据复用 search._exclusive（属性级关键值 + 极性词）。
        from .search import _exclusive as _mutually_exclusive  # 延迟导入：search 依赖 store

        groups: dict[tuple, list[dict]] = {}
        for row in self.conn.execute(
            "SELECT * FROM memories WHERE status='active'"
            " AND (type='state' OR (type='profile' AND subtype='identity')"
            "      OR (type='domain' AND subtype='decision'))"
        ).fetchall():
            d = row_to_dict(row)
            groups.setdefault(
                (d["user_id"], d["subject_type"], d["subject_name"], d["type"],
                 d["subtype"], d["domain_key"]),
                [],
            ).append(d)
        for key, rows_in_slot in groups.items():
            if len(rows_in_slot) < 2:
                continue
            # 两类槽位分开判：
            # - profile/identity、domain/decision 是**单值**语义（一个人只有一个名字、
            #   一个项目只有一个最终决策），两条 active 本身就是问题；
            # - state 允许多条并存（备考数学一 + 在外地实习是两件不同的事），
            #   只有"同一属性的两个取值"（数学 120 vs 数学 130）才算互斥。
            single_typed = (key[3], key[4]) in (("profile", "identity"), ("domain", "decision"))
            if single_typed:
                problems.append(
                    f"用户 {key[0]} 的单值槽位 {key[3]}/{key[4]} {key[2] or ''} /{key[5] or '-'}"
                    f" 有 {len(rows_in_slot)} 条 active：{[m['id'] for m in rows_in_slot]}"
                )
                continue
            for i in range(len(rows_in_slot)):
                for j in range(i + 1, len(rows_in_slot)):
                    a, b = rows_in_slot[i], rows_in_slot[j]
                    if _mutually_exclusive(a, b):
                        problems.append(
                            f"用户 {a['user_id']} 的槽位 {key[3]}/{key[4]} {key[2] or ''} /"
                            f"{key[5] or '-'} 里 {a['id']} 与 {b['id']} 取值互斥却同时 active"
                        )

        # 倒排索引完整性：active 记忆必须有索引项；索引项必须指向存在的记忆
        missing = self.conn.execute(
            "SELECT count(*) AS n FROM memories m WHERE m.status='active'"
            " AND length(m.content) >= 2 AND NOT EXISTS"
            " (SELECT 1 FROM memory_gram g WHERE g.memory_id=m.id)"
        ).fetchone()["n"]
        if missing:
            problems.append(f"{missing} 条 active 记忆没有倒排索引项（可 rebuild_index 修复）")
        orphans = self.conn.execute(
            "SELECT count(*) AS n FROM memory_gram g"
            " WHERE NOT EXISTS (SELECT 1 FROM memories m WHERE m.id=g.memory_id)"
        ).fetchone()["n"]
        if orphans:
            problems.append(f"{orphans} 条倒排索引指向不存在的记忆（可 rebuild_index 修复）")
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

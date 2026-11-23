#!/usr/bin/env python3
"""外部数据集 → 统一内部结构（只读数据，不含任何引擎调用）。

两个数据集的边界（见文档 §2）：
  LongMemEval  A-R：单个 case 一个 haystack，标准答案 = answer_session_ids。
  LoCoMo       A-R：一个会话一个库，标准答案 = evidence 里的 dia_id。

子集选择是确定性的：按类别轮转 + 固定种子排序，结果写进 run.json，
任何人可以用同样的 --n 复现同一批题。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .harness import Unit

DEFAULT_SEED = 20261119


@dataclass
class Case:
    case_id: str
    query: str
    gold: list[str]                 # 标准答案单元（session id / dia_id）
    category: str
    unit_ids: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class Shard:
    """一个独立的记忆库：先把 units 灌进去，再回答 cases。"""
    shard_id: str
    units: list[Unit]
    cases: list[Case]


# ------------------------------------------------------------------ 子集选择
def select_subset(items: list, n: int | None, key, category, seed: int = DEFAULT_SEED) -> list:
    """确定性分层抽样：每类轮转取，类内按 (sha256(seed+key)) 排序。"""
    if not n or n >= len(items):
        return items
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(str(category(it)), []).append(it)

    def rank(it) -> str:
        return hashlib.sha256(f"{seed}:{key(it)}".encode()).hexdigest()

    for g in groups.values():
        g.sort(key=rank)
    picked: list = []
    cats = sorted(groups)
    i = 0
    while len(picked) < n:
        progressed = False
        for c in cats:
            if i < len(groups[c]) and len(picked) < n:
                picked.append(groups[c][i]); progressed = True
        if not progressed:
            break
        i += 1
    return picked


def sha256_file(path: str, *, limit_bytes: int | None = None) -> str:
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(1 << 20)
            if not buf:
                break
            h.update(buf)
            read += len(buf)
            if limit_bytes and read >= limit_bytes:
                break
    return h.hexdigest()


# ------------------------------------------------------------------ LongMemEval
def _lme_units(q: dict) -> list[Unit]:
    units: list[Unit] = []
    for sid, sess, date in zip(q["haystack_session_ids"], q["haystack_sessions"], q["haystack_dates"]):
        lines = []
        for turn in sess:
            role = turn.get("role", "user")
            content = (turn.get("content") or "").strip()
            if content:
                lines.append(f"{role}: {content}")
        units.append(Unit(unit_id=sid, text=f"[{date}] " + " ".join(lines), date=date))
    return units


def _trim_haystack(q: dict, keep: int, seed: int) -> dict:
    """把 500 个 session 的 haystack 缩到 keep 个：证据 session 必留，其余按哈希序取。

    这是**已知偏差**，不是"全量 LongMemEval-S"：报告里必须写明 --haystack N。
    缩的理由是产品写入路在同一 slot 上是线性查重（见文档 §7），全量灌 500 个
    session 会把单题耗时推到不可接受；keep 取到与 MAX_CANDIDATES=200 同量级。
    """
    ids = q["haystack_session_ids"]
    if keep <= 0 or len(ids) <= keep:
        return q
    gold = set(q.get("answer_session_ids") or [])
    order = sorted(range(len(ids)), key=lambda i: hashlib.sha256(
        f"{seed}:{q['question_id']}:{ids[i]}".encode()).hexdigest())
    keep_idx = [i for i in order if ids[i] in gold]
    for i in order:
        if len(keep_idx) >= keep:
            break
        if i not in keep_idx:
            keep_idx.append(i)
    keep_idx = sorted(keep_idx)
    out = dict(q)
    out["haystack_session_ids"] = [ids[i] for i in keep_idx]
    out["haystack_sessions"] = [q["haystack_sessions"][i] for i in keep_idx]
    out["haystack_dates"] = [q["haystack_dates"][i] for i in keep_idx]
    return out


def longmemeval_shards(path: str, n: int | None = None, seed: int = DEFAULT_SEED,
                       haystack: int | None = None) -> list[Shard]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    picked = select_subset(data, n, lambda q: q["question_id"], lambda q: q["question_type"], seed)
    shards: list[Shard] = []
    for q in picked:
        q = _trim_haystack(q, haystack or 0, seed)
        units = _lme_units(q)
        case = Case(
            case_id=q["question_id"],
            query=q["question"],
            gold=list(q.get("answer_session_ids") or []),
            category=q["question_type"],
            unit_ids=[u.unit_id for u in units],
            meta={"question_date": q.get("question_date"), "n_sessions": len(units)},
        )
        shards.append(Shard(shard_id=q["question_id"], units=units, cases=[case]))
    return shards


# ------------------------------------------------------------------ LoCoMo
def _locomo_turn_units(conv: dict) -> list[Unit]:
    units: list[Unit] = []
    for key, val in conv.items():
        if not key.startswith("session_") or key.endswith("date_time"):
            continue
        if not isinstance(val, list):
            continue
        date = conv.get(key + "_date_time", "")
        for turn in val:
            dia = turn.get("dia_id")
            text = (turn.get("text") or "").strip()
            if not dia or not text:
                continue
            speaker = turn.get("speaker") or "speaker"
            units.append(Unit(unit_id=dia, text=f"{speaker}: {text}", date=date))
    return units


def _locomo_numeric(conv: dict) -> list[Unit]:
    for key in ("session_1", "session_2"):
        val = conv.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def locomo_shards(path: str, n: int | None = None, seed: int = DEFAULT_SEED) -> list[Shard]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    convs = select_subset(data, n, lambda c: c.get("sample_id", ""), lambda c: "conv", seed)
    shards: list[Shard] = []
    for conv in convs:
        units = _locomo_turn_units(conv["conversation"])
        known = {u.unit_id for u in units}
        cases: list[Case] = []
        for i, qa in enumerate(conv.get("qa") or []):
            gold = [e for e in (qa.get("evidence") or []) if e in known]
            cases.append(Case(
                case_id=f"{conv.get('sample_id','conv')}#{i}",
                query=qa.get("question", ""),
                gold=gold,
                category=f"cat{qa.get('category')}",
                unit_ids=sorted(known),
                meta={"answer": qa.get("answer")},
            ))
        shards.append(Shard(shard_id=str(conv.get("sample_id", "conv")), units=units, cases=cases))
    return shards


# ------------------------------------------------------------------ 判分侧视图
def load_cases(bench: str, path: str, n: int | None = None, seed: int = DEFAULT_SEED,
               haystack: int | None = None) -> list[Case]:
    if bench == "longmemeval":
        shards = longmemeval_shards(path, n=n, seed=seed, haystack=haystack)
    else:
        shards = locomo_shards(path, n=n, seed=seed)
    return [c for s in shards for c in s.cases]

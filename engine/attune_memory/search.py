"""检索与打分（对齐 MEMORY_RETRIEVAL_V1.md）。

Final = Relevance × Confidence × Importance × Freshness × ContextMatch × ConflictPenalty

刻意不做的事：向量化、FTS5、bm25、bigram 索引。它们在 V2，见 V2_V3_RESERVED.md。
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

from . import const
from .store import Store
from .text import bigrams, match_vocabulary, normalize, segments, split_keywords
from .util import days_between, now_iso

# 关键值 =（属性名, 取值）。只比"同一个属性"才谈得上冲突：
# "每天学 3 小时" 与 "数学 120 分" 数值不同，但不是同一属性，不能判互斥。
_KEY_VALUE_PATTERNS = (
    # (属性名, 正则, 取哪一组；0=整体)
    ("版本", re.compile(r"\b\d+\.\d+(?:\.\d+)?\b"), 0),
    ("路径", re.compile(r"[a-z]:\\[^\s，。；;]+"), 0),
    ("端口", re.compile(r"端口\s*(?:是|为|[:：])?\s*(\d{2,5})"), 1),
    ("数值", re.compile(
        r"(\d+(?:\.\d+)?)\s*(个|条|次|天|周|月|年|gb|mb|kb|毫秒|秒|分钟|小时|%|℃)", re.I
    ), 0),
    # 常见"同一属性"的口语写法：分数。属性名带上科目，避免"数学 120 分"和"英语 120 分"互相打架。
    ("分数", re.compile(r"(数学|英语|政治|专业课|总分)\s*(?:考了?|得了?|是|有)?\s*(\d{2,3})\s*分"), 2),
    # 中文数字 + 单位（"三小时""两天"）：中文里数字经常不写成阿拉伯数字。
    ("数值", re.compile(r"([一二三四五六七八九十两半]+)\s*(个|条|次|天|周|月|年|小时|分钟|小时)"), 0),
)


def key_values(text: str) -> dict[str, str]:
    """抽关键值：``{属性: 取值}``。同一属性不同取值才算冲突（W7 精度修正）。"""
    out: dict[str, str] = {}
    low = text.lower()
    for name, pat, group in _KEY_VALUE_PATTERNS:
        for m in pat.finditer(low):
            value = m.group(group).strip()
            if name == "分数":
                out[f"分数:{m.group(1)}"] = value
            else:
                out.setdefault(name, value)
    return out


def domain_match(memory_domain: str | None, current: str | None) -> float:
    """ContextMatch 的 domain 部分（P5 §2.3 的表，逐行对应）。"""
    if current is None:
        return 0.9 if memory_domain is None else 0.5
    if memory_domain is None:
        return 0.9
    if memory_domain == current:
        return 1.0
    if current.startswith(memory_domain + ".") or memory_domain.startswith(current + "."):
        return 0.85
    return 0.5


def freshness(memory: dict, now: str) -> float:
    base = memory.get("valid_from") if memory["type"] == "state" else memory.get("updated_at")
    base = base or memory.get("updated_at") or now
    try:
        age = max(0.0, days_between(base, now))
    except ValueError:
        return const.FRESHNESS_FLOOR
    half = const.HALF_LIFE_DAYS.get(memory["type"], 365.0)
    return max(const.FRESHNESS_FLOOR, 0.5 ** (age / half))


def is_expired(memory: dict, now: str) -> bool:
    vu = memory.get("valid_until")
    return bool(vu) and vu <= now


def _term_in_content(term: str, content: str) -> bool:
    """拉丁词按词边界匹配，中文按子串匹配。

    "sq" 不该命中 "sqlite"（否则任何两字母组合都能召回）；中文没有词边界，
    只能按子串，这也是 V1 中文检索精度上限所在（V2 用 bigram/FTS5 解决）。
    """
    if re.fullmatch(r"[a-z0-9_.+#-]+", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", content) is not None
    return term in content



def relevance(query_terms_a: list[str], query_terms_b: list[str], memory: dict) -> tuple[float, list[str]]:
    """词表路权重 = len(term)；片段路 = 0.75×len(term) 且必须出现在 content 里。

    片段路给 0.75 而不是 0.5：一个"逐字出现在正文里"的 3 字词，其证据强度
    和词表命中差不了多少；压太低会让单关键词查询永远够不到 MIN_SCORE。
    """
    raw = 0.0
    hits: list[str] = []
    kws = set(split_keywords(memory.get("keywords")))
    content = normalize(memory.get("content") or "")
    for t in query_terms_a:
        raw += len(t)
        hits.append(t)
    for t in query_terms_b:
        if t in kws:
            continue  # 已被词表路计过
        if _term_in_content(t, content):
            raw += 0.75 * len(t)
            hits.append(t)
    # R2：中文二元组兜底。词表与片段都没命中时，用"查询里的二元组有多少出现在正文里"
    # 给出一个下限——否则"用北京查北京市"这种跨词写法永远检索不到（V1 没有词典）。
    if raw == 0:
        qgrams = set(bigrams(" ".join(query_terms_a + query_terms_b)))
        if qgrams:
            content_grams = set(bigrams(memory.get("content") or ""))
            matched = qgrams & content_grams
            if matched:
                coverage = len(matched) / len(qgrams)
                raw = const.BIGRAM_WEIGHT * coverage
                hits.extend(sorted(matched))
    rel = raw / (raw + const.RELEVANCE_SATURATION) if raw > 0 else 0.0
    return rel, hits


def conflict_penalty(memory: dict, siblings: list[dict], now: str) -> tuple[float, str | None]:
    """同槽位还有别的 active 且互斥 → 0.7；过期/被取代但被显式查询 → 0.5。"""
    if memory["status"] == "superseded":
        return 0.5, "已过时（被取代）"
    if is_expired(memory, now):
        return 0.5, "可能已过期"
    for other in siblings:
        if other["id"] == memory["id"]:
            continue
        if _exclusive(memory, other):
            return 0.7, f"与 {other['id']} 存在矛盾记录"
    return 1.0, None


def _exclusive(a: dict, b: dict) -> bool:
    """互斥判定：命中极性词对，或两边都有关键值且完全不重合。"""
    ta, tb = a.get("content") or "", b.get("content") or ""
    for pos, neg in const.POLARITY_PAIRS:
        a_pos, a_neg = pos in ta, neg in ta
        b_pos, b_neg = pos in tb, neg in tb
        if (a_pos and b_neg) or (a_neg and b_pos):
            return True
    ka, kb = key_values(ta), key_values(tb)
    # 只比同名属性：都提到"端口"且取值不同 → 冲突；一个说"3 小时"、另一个说"120 分"
    # 不是同一属性，不能判互斥（旧实现"完全不重合即互斥"会把它们判成矛盾）。
    for name in set(ka) & set(kb):
        if ka[name] != kb[name]:
            return True
    return False


def _drop(dropped: list[dict] | None, memory: dict, why: str) -> None:
    """把"为什么没进候选"记下来（模块 7）。只记 id 与理由，不记正文。"""
    if dropped is not None:
        dropped.append({"id": memory["id"], "why": why})


def build_candidates(
    store: Store,
    *,
    user_id: str = "default",
    domain: str | None = None,
    type: str | None = None,
    subtypes: Iterable[str] | None = None,
    policies: Iterable[str] | None = None,
    include_expired: bool = False,
    include_superseded: bool = False,
    include_never: bool = False,
    task_key: str | None = None,
    dropped: list[dict] | None = None,
    now: str | None = None,
) -> list[dict]:
    """阶段①：结构化过滤。走索引，再用 Python 处理前缀匹配与 scope。"""
    now = now or now_iso()
    statuses: tuple[str, ...] = ("active", "superseded") if include_superseded else ("active",)
    rows = store.query(user_id=user_id, status=statuses, type=type, order="updated_at DESC")
    pol = set(policies) if policies else None
    sub = set(subtypes) if subtypes else None
    out: list[dict] = []
    for m in rows:
        if m["status"] == "active" and not include_expired and is_expired(m, now):
            _drop(dropped, m, "已过期")
            continue
        if pol is not None and m["injection_policy"] not in pol:
            _drop(dropped, m, f"注入策略是 {m['injection_policy']}，不属于本次要注入的层级")
            continue
        if sub is not None and m["subtype"] not in sub:
            _drop(dropped, m, f"子类型是 {m['subtype']}，不在本次筛选范围")
            continue
        if m["injection_policy"] == "never" and not include_never:
            _drop(dropped, m, "注入策略是 never（只保存不注入）")
            continue
        if m["scope"] == "task":
            if not task_key or (m.get("metadata") or {}).get("task_key") != task_key:
                _drop(dropped, m, "task 作用域，当前没有匹配的任务键")
                continue
        if m["domain_key"] is not None and domain is not None:
            if domain_match(m["domain_key"], domain) < 0.85:
                _drop(dropped, m, f"领域不匹配（{m['domain_key']} vs {domain}）")
                continue
        elif m["domain_key"] is not None and domain is None:
            # 当前没有领域线索时，领域专属记忆仍可作候选，但会被 ContextMatch 降权
            pass
        out.append(m)
        if len(out) >= const.MAX_CANDIDATES:
            break
    return out


# ---------------------------------------------------------------- 三阶段（R4）
#
# V1 的排序是"一堆乘法因子拍在一起"，加一个新因子时没人说得清它该进哪一步。
# 这里把检索显式拆成三段，**新因子只许进第 ② 段**：
#
#   ① 资格（eligibility）：结构化过滤 + "有没有命中"的硬闸门 + 命中质量门槛。
#      这一段决定"能不能出现"，与重不重要无关。
#   ② 排序（ranking）：六因子相乘，只决定"先出现谁"。
#   ③ 预算（budget）：截断到 limit（读管线还有分桶预算，那在 read.py）。
#
# 这样"过时降权"永远不会变成"过时消失"（降权是②，消失只能是①）。


def stage_eligibility(
    cands: list[dict],
    terms_a: list[str],
    terms_b: list[str],
    query: str | None,
) -> list[tuple[dict, float, list[str]]]:
    """① 资格：有查询时要求至少命中一个词；无查询时全部通过（按结构化条件取）。"""
    out: list[tuple[dict, float, list[str]]] = []
    for m in cands:
        rel, hits = relevance(terms_a, terms_b, m)
        if query and not hits:
            continue
        out.append((m, rel, hits))
    return out


def stage_ranking(
    eligible: list[tuple[dict, float, list[str]]],
    slots: dict[tuple, list[dict]],
    domain: str | None,
    now: str,
) -> list[dict]:
    """② 排序：六因子相乘。这里只决定顺序，不做淘汰。"""
    scored: list[dict] = []
    for m, rel, hits in eligible:
        conf = float(m["confidence"])
        imp = const.IMPORTANCE_VALUE[m["importance"]]
        fresh = freshness(m, now)
        ctx = domain_match(m["domain_key"], domain)
        siblings = slots[(m["subject_type"], m["subject_name"], m["type"], m["subtype"], m["domain_key"])]
        pen, note = conflict_penalty(m, siblings, now)
        score = rel * conf * imp * fresh * ctx * pen
        scored.append(
            {
                "memory": m,
                "score": round(score, 6),
                "factors": {
                    "relevance": round(rel, 4),
                    "confidence": conf,
                    "importance": imp,
                    "freshness": round(fresh, 4),
                    "context": ctx,
                    "conflict": pen,
                },
                "matched_terms": hits,
                "note": note,
            }
        )
    scored.sort(key=lambda x: (-x["score"], x["memory"]["updated_at"], x["memory"]["id"]))
    return scored


def stage_budget(
    scored: list[dict],
    limit: int,
    debug: dict | None = None,
) -> list[dict]:
    """③ 预算：先过命中质量闸门，再截断。闸门卡的都是①里的量，不是最终分。"""
    passed = [
        s for s in scored
        if s["factors"]["relevance"] * s["factors"]["confidence"] * s["factors"]["context"]
        >= const.MIN_SCORE
    ]
    if debug is not None:
        debug.setdefault("stages", {}).update(
            {"ranked": len(scored), "gate_passed": len(passed), "selected": min(len(passed), limit)}
        )
    return passed[:limit]


def search(
    store: Store,
    query: str | None = None,
    *,
    user_id: str = "default",
    domain: str | None = None,
    type: str | None = None,
    subtypes: Iterable[str] | None = None,
    policies: Iterable[str] | None = None,
    limit: int = const.K_DEFAULT,
    include_expired: bool = False,
    include_superseded: bool = False,
    include_never: bool = False,
    task_key: str | None = None,
    now: str | None = None,
    count_use: bool = True,
    debug: dict | None = None,
) -> list[dict]:
    """返回 ``[{memory, score, factors, matched_terms, note}]``，按分降序。

    ``debug`` 非空时会被填上本次检索的过程数字（候选数、被结构化过滤丢掉的原因、
    命中数）——给"运行时读日志"用，不影响排序。
    """
    now = now or now_iso()
    limit = max(1, min(int(limit), const.K_HARD_MAX))
    dropped = debug.setdefault("dropped", []) if debug is not None else None
    cands = build_candidates(
        store, user_id=user_id, domain=domain, type=type, subtypes=subtypes,
        policies=policies, include_expired=include_expired,
        include_superseded=include_superseded, include_never=include_never,
        task_key=task_key, now=now, dropped=dropped,
    )
    if debug is not None:
        debug["candidates"] = len(cands)
        debug["dropped_total"] = len(dropped or [])
    store.counter_bump("search_candidates", len(cands))
    if not cands:
        return []

    vocab: list[str] = []
    for m in cands:
        vocab.extend(split_keywords(m.get("keywords")))
    vocab = list(dict.fromkeys(vocab))

    terms_a = match_vocabulary(query, vocab) if query else []
    terms_b = segments(query) if query else []

    # 同槽位分组，用于冲突惩罚
    slots: dict[tuple, list[dict]] = {}
    for m in cands:
        slots.setdefault(
            (m["subject_type"], m["subject_name"], m["type"], m["subtype"], m["domain_key"]), []
        ).append(m)

    eligible = stage_eligibility(cands, terms_a, terms_b, query)
    if debug is not None:
        debug.setdefault("stages", {}).update({"candidates": len(cands), "eligible": len(eligible)})
    scored = stage_ranking(eligible, slots, domain, now)
    top = stage_budget(scored, limit, debug)
    store.counter_bump("search_hits", len(top))

    if count_use:
        for s in top:
            store.bump(s["memory"]["id"], "use_count")
    return top


def find_similar(
    store: Store,
    content: str,
    *,
    type: str | None = None,
    subtype: str | None = None,
    subject_type: str = "user",
    subject_name: str | None = None,
    domain_key: str | None = None,
    user_id: str = "default",
    limit: int = 10,
    now: str | None = None,
) -> list[dict]:
    """写入前的查重/冲突探测（P3 §5.1）。返回带 ``relation`` 的候选。"""
    now = now or now_iso()
    result: list[dict] = []

    if type and subtype:
        slot = store.same_slot(
            subject_type=subject_type, subject_name=subject_name, type=type,
            subtype=subtype, domain_key=domain_key, user_id=user_id,
        )
        for m in slot:
            result.append(
                {"memory": m, "relation": _relation(content, m), "reason": _relation_reason(content, m)}
            )

    terms = [t for t in segments(content) if len(t) >= 2][:8]
    kws = [t for t in (split_keywords(content) or []) if len(t) >= 2][:8]
    near = store.near_keywords(list(dict.fromkeys(terms + kws)), limit=limit, user_id=user_id)
    seen = {r["memory"]["id"] for r in result}
    for m in near:
        if m["id"] in seen:
            continue
        result.append({"memory": m, "relation": _relation(content, m), "reason": _relation_reason(content, m)})

    # 等价 > 互斥 > 互补 > 无关
    order = {"equivalent": 0, "exclusive": 1, "complementary": 2, "unrelated": 3}
    result.sort(key=lambda r: (order[r["relation"]], r["memory"]["updated_at"]))
    return result[:limit]


def _relation(content: str, memory: dict) -> str:
    a = normalize(content)
    b = normalize(memory.get("content") or "")
    if a == b:
        return "equivalent"
    ka = set(split_keywords(memory.get("keywords")))
    ta = set(segments(content))
    if ta and ka:
        inter = len(ta & ka) / max(1, len(ta | ka))
        if inter >= 0.8:
            return "equivalent"
    if _exclusive({"content": content}, memory):
        return "exclusive"
    overlap = len(set(segments(content)) & set(segments(memory.get("content") or "")))
    return "complementary" if overlap >= 1 else "unrelated"


def _relation_reason(content: str, memory: dict) -> str:
    rel = _relation(content, memory)
    return {
        "equivalent": "视为同一条事实（归一化后相同或关键词高度重合）",
        "exclusive": "同槽位且互斥（极性词或关键值冲突）",
        "complementary": "同槽位且内容有交集，可考虑合并",
        "unrelated": "仅关键词邻近，不构成同一事实",
    }[rel]

"""写入管线（对齐 MEMORY_WRITE_PIPELINE.md）。

流程：Candidate → 校验 → Value Gate → Explicit/Inferred 约束 → 同槽位检索
→ 决策表 → 单事务落库（记忆行 + 时间轴）。

一条铁律：**不确定就不写，或者写成更弱的形态**。宁可漏记，不可记错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import const
from .search import _exclusive, find_similar
from .store import InvariantError, Store
from .text import auto_keywords, join_keywords, normalize, split_keywords
from .util import now_iso, new_id, parse_iso

#: 内容长度上下界（P3 §2.1：太短没有信息量，太长是聊天记录不是记忆）
MIN_CONTENT_CHARS = 4
MAX_CONTENT_CHARS = 300

#: 推断出来的记忆 confidence 上限（P3 §4）
INFERRED_CONFIDENCE_CAP = 0.7

#: TOUCH 每次 +0.05，封顶 0.95（永远不给 1.0，1.0 意味着"不可错"）
CONFIDENCE_STEP = 0.05
CONFIDENCE_CAP = 0.95
SUPERSEDE_AUTO_CONFIDENCE = 0.8   # 显式 + ≥0.8 才允许自动取代旧条目
SUPERSEDE_KEEP_CONFIDENCE = 0.6   # 0.6–0.8：并存 + 标注冲突


class CandidateError(ValueError):
    """候选本身不合法。消息里必须包含"合法值清单"，否则模型只会反复重试。"""


@dataclass
class Candidate:
    content: str
    type: str
    subtype: str
    subject_type: str = "user"
    subject_name: str | None = None
    domain_key: str | None = None
    scope: str = "global"
    importance: str = "normal"
    injection_policy: str = "contextual"
    confidence: float = 0.8
    keywords: list[str] | None = None
    explicit: bool = True
    source_message_id: str | None = None
    source_conversation_id: str | None = None
    valid_from: str | None = None
    #: 已经知道什么时候失效（例如"这学期在准备考研"），没有就留空
    valid_until: str | None = None
    metadata: dict | None = None
    user_id: str = "default"
    #: 调用方明确要取代的条目（memory_update/_supersede 传入）
    supersede_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise CandidateError(
                f"未知字段：{sorted(unknown)}；可用字段：{sorted(known)}"
            )
        return cls(**data)


@dataclass
class Decision:
    """一次写入尝试的结论。``action`` 与 P3 §5.3 决策表同名。"""

    action: str
    reason: str
    memory: dict | None = None
    related: list[dict] = field(default_factory=list)
    merge_suggestion: dict | None = None

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "memory": self.memory,
            "related": [
                {"id": r["memory"]["id"], "relation": r["relation"], "reason": r["reason"]}
                for r in self.related
            ],
            "merge_suggestion": self.merge_suggestion,
        }


# ---------------------------------------------------------------- 校验


def validate(cand: Candidate) -> None:
    if cand.type not in const.TYPES:
        raise CandidateError(f"非法 type：{cand.type}；合法值：{list(const.TYPES)}")
    if cand.subtype not in const.SUBTYPES[cand.type]:
        raise CandidateError(
            f"{cand.type} 不支持 subtype：{cand.subtype}；合法值：{list(const.SUBTYPES[cand.type])}"
        )
    if cand.subject_type not in const.SUBJECT_TYPES:
        raise CandidateError(
            f"非法 subject_type：{cand.subject_type}；合法值：{list(const.SUBJECT_TYPES)}"
        )
    if cand.importance not in const.IMPORTANCE:
        raise CandidateError(f"非法 importance：{cand.importance}；合法值：{list(const.IMPORTANCE)}")
    if cand.injection_policy not in const.POLICIES:
        raise CandidateError(
            f"非法 injection_policy：{cand.injection_policy}；合法值：{list(const.POLICIES)}"
        )
    if cand.scope not in const.SCOPES:
        raise CandidateError(f"非法 scope：{cand.scope}；合法值：{list(const.SCOPES)}")
    if not (0.0 <= float(cand.confidence) <= 1.0):
        raise CandidateError("confidence 必须在 [0,1]")

    content = (cand.content or "").strip()
    if not content:
        raise CandidateError("content 不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise CandidateError(
            f"content 超过 {MAX_CONTENT_CHARS} 字（{len(content)}）；请压缩成一条可复用的事实"
        )

    # W4 ①：明确第一人称（"我/我的/本人"开头）必须记在 user 名下。
    # 反过来"我朋友喜欢红色"必须以 person 落库——两侧都拦，主体才不会坍缩。
    if (cand.subject_type != "user" and content.startswith(("我", "本人"))
            and not any(m in content for m in const.THIRD_PARTY_MARKERS)):
        raise CandidateError(
            f"content 以第一人称开头（{content[:6]}…），但 subject_type={cand.subject_type}；"
            "如果这条讲的是用户本人，用 subject_type=user；如果是别人，请把话改成以对方为主语"
        )

    # 主体防坍缩：说"我朋友"却挂在 user 名下，是 V1 最危险的错误
    if cand.subject_type == "user":
        for marker in const.THIRD_PARTY_MARKERS:
            if marker in content:
                raise CandidateError(
                    f"content 出现「{marker}」但 subject_type=user；"
                    "这是第三方主体的记忆，请把 subject_type 设为 person/project/org/other "
                    "并用 subject_name 指明是谁"
                )
    if cand.subject_type != "user" and not (cand.subject_name or "").strip():
        raise CandidateError(f"subject_type={cand.subject_type} 时 subject_name 必填")

    # W6：always 是"每轮都注入"的名额，必须有类型白名单，否则 important 会被误当成 always。
    # 只有"长期怎么跟这个人说话/长期约束/身份"值得每轮出现；领域内的做事方式只该在
    # 那个领域出现（scope=domain），一次性任务更不该常驻。
    if cand.injection_policy == "always":
        allowed = const.ALWAYS_ALLOWED.get(cand.type, ())
        if cand.subtype not in allowed:
            raise CandidateError(
                f"{cand.type}/{cand.subtype} 不允许 always 注入；"
                f"always 只给 {[(t, list(s)) for t, s in const.ALWAYS_ALLOWED.items()]}。"
                "可以用 contextual（按话题命中时注入）"
            )
        if cand.scope != "global":
            raise CandidateError(
                f"scope={cand.scope} 的记忆不允许 always（它只在一个领域/任务里成立）；"
                "改用 contextual"
            )

    if cand.scope == "domain" and not cand.domain_key:
        raise CandidateError("scope=domain 时 domain_key 必填")
    if cand.scope == "task" and not (cand.metadata or {}).get("task_key"):
        raise CandidateError("scope=task 时 metadata.task_key 必填")
    # W1：valid_until 是"这条事实什么时候失效"，只有 state 有起止语义。
    # profile 挂一个到期时间会让"偏好"突然消失，episodic 更不该有——直接报错比默默忽略好。
    if cand.valid_until and cand.type != "state":
        raise CandidateError(
            f"valid_until 只对 state（有起止的计划/活动）有意义，当前 type={cand.type}。"
            "如果要表达\"这个偏好只在这段时间成立\"，请写成 state，或去掉 valid_until"
        )

    if cand.type == "state" and not cand.valid_from:
        # state 的"从什么时候开始成立"是唯一能判断它有没有过期的依据
        cand.valid_from = now_iso()

    # valid_until 允许调用方指定（"这学期到 2026-01 结束"），但必须是可比较的 ISO 时间：
    # 它会被拿去跟 now 比字符串，格式错一次，这条记忆就永远不过期或立刻过期。
    if cand.valid_until:
        try:
            parse_iso(cand.valid_until)
        except (TypeError, ValueError) as exc:
            raise CandidateError(
                f"valid_until 不是合法的 ISO 时间：{cand.valid_until!r}；形如 2026-01-31T00:00:00Z"
            ) from exc
        if cand.valid_from and cand.valid_until <= cand.valid_from:
            raise CandidateError(
                f"valid_until({cand.valid_until}) 必须晚于 valid_from({cand.valid_from})"
            )


DISCARDED_PREVIEW = 120  # 只留前 120 字：丢弃日志要能解释"为什么没记"，不需要全文


def _discarded(cand: Candidate, reason: str, store: Store | None) -> str:
    """写一条丢弃观测（模块 8）。只给人看，永不进模型上下文。

    这是"不存聊天记录"原则的一个有意识例外：不记下被丢弃的原话，用户就无法回答
    "我明明说过，为什么没记住"。代价是库里多了用户原话片段，所以只留前 120 字、
    保留 14 天、可 `--purge-discarded` 一键清空。
    """
    if store is not None:
        try:
            store.observe(
                "discarded",
                subject=(cand.content or "")[:DISCARDED_PREVIEW],
                detail=reason,
            )
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响写入决策
            pass
    return reason


def type_hint(cand: Candidate) -> str | None:
    """类型可能选错时的提示（W1）。**只提示，不拦**——类型判断最终仍是模型的活。

    真实缺陷 #10 就是"备考被写成 profile/identity"：profile 是"他是谁"，备考是
    "他这段时间在做什么"，后者有起点也会结束。这里用最保守的句式线索给一句提醒，
    让写管线把它记进 Decision.reason，并计数供事后统计误判率。
    """
    content = (cand.content or "")
    if cand.type == "profile" and any(w in content for w in ("备考", "正在准备考", "复习", "考研", "找工作", "实习")):
        return "content 看起来是「有计划、会结束」的状态，type 可能应该是 state"
    if cand.type == "state" and any(w in content for w in ("总是", "从不", "习惯", "一律", "每次")):
        return "content 看起来是长期习惯，type 可能应该是 profile"
    return None


def _starts_like_question(raw: str) -> str | None:
    """疑问词出现在**句首**才算提问（W3）。

    旧实现是"出现问词就拒"——"为什么天气一冷膝盖就疼"会被当提问拒掉，可它明明是一条
    身体事实。句式化之后：只有 `怎么/为什么/如何…` 打头，或整句以问号结尾，才算提问。
    """
    head = raw.lstrip("，,。.、；;：: 　")
    for pat in const.QUESTION_PATTERNS:
        if head.startswith(pat):
            return pat
    return None


def _starts_like_task(raw: str) -> str | None:
    """祈使句只在句首才算任务指令（W3）。

    "帮我"出现在句中（"这个习惯帮我省了很多时间"）不是任务；打头才是。
    """
    head = raw.lstrip("，,。.、；;：: 　")
    for pat in const.TASK_PATTERNS:
        if head.startswith(pat):
            return pat
    return None


def value_gate(cand: Candidate, *, store: Store | None = None) -> str | None:
    """Value Gate：返回 IGNORE 的理由；None 表示值得记。

    判断顺序是先"形态"（是不是一句可复用的事实陈述）再"内容"（是不是时效/任务）。
    模型**显式**调用 `memory_remember` 的候选（``cand.explicit``）不受低级词表限制：
    词表只该拦兜底抽取（那是正则猜的），不该拦"模型明确说这值得记"。唯一例外是
    时效性内容——"今天天气不错"再显式也不该进长期记忆。
    """
    norm = normalize(cand.content)
    if len(norm) < MIN_CONTENT_CHARS:
        return _discarded(cand, "内容过短，不含可复用信息", store)
    raw = (cand.content or "").strip()

    for pat in const.TRANSIENT_PATTERNS:
        if pat in raw:
            return _discarded(cand, f"时效性内容（命中「{pat}」），明天就没用了", store)

    # 问号与祈使句是结构，不是词表：哪怕模型显式要记，这两类也不该进库。
    if raw.endswith(("?", "？")):
        return _discarded(cand, "这是一个问题，不是一条事实", store)
    task = _starts_like_task(raw)
    if task:
        return _discarded(cand, f"一次性任务指令（句首「{task}」），不是关于用户的事实", store)
    # 显式写入跳过的只是"词表容易误伤"的两类（句首疑问词、指代开头）。
    if cand.explicit:
        return None
    pat = _starts_like_question(raw)
    if pat:
        return _discarded(cand, f"这是一个提问（句首「{pat}」），不是一条事实", store)
    for word in const.ANAPHORA:
        if raw.startswith(word):
            return _discarded(cand, f"以指代词「{word}」开头，脱离上下文读不懂", store)
    return None


# ---------------------------------------------------------------- 决策


def consider(store: Store, cand: Candidate, *, now: str | None = None) -> Decision:
    """完整写入管线。``action == 'ignore'`` 时不落库。"""
    now = now or now_iso()
    # 推断出来的记忆：降 confidence、禁 always（P3 §4）
    if not cand.explicit:
        cand.confidence = min(float(cand.confidence), INFERRED_CONFIDENCE_CAP)
        if cand.injection_policy == "always":
            cand.injection_policy = "contextual"
        # W5：正则抽出来的 interaction 常常是"这一章用这种方法讲"这种任务局部要求，
        # 记成 global 常驻偏好的代价是每次对话都背着它。默认收窄成 on_demand：
        # 需要时能被检索到，但不主动注入；等模型明确判定为长期偏好再升级。
        if (cand.type == "profile" and cand.subtype == "interaction"
                and cand.scope == "global" and cand.injection_policy == "contextual"):
            cand.injection_policy = "on_demand"
            cand.metadata = dict(cand.metadata or {})
            cand.metadata["scope_hint"] = "inferred-interaction-narrowed"

    validate(cand)
    store.counter_bump("candidates_total")
    # W1：类型可能选错时留痕（不拦）。挂在 metadata 上，explain 里能看到；同时计数，
    # 事后可以统计"模型把 state 写成 profile"的比例，而不是靠人肉回忆。
    hint = type_hint(cand)
    if hint:
        cand.metadata = dict(cand.metadata or {})
        cand.metadata["type_hint"] = hint
        store.counter_bump("type_hint_suspected")

    reason = value_gate(cand, store=store)
    if reason:
        store.counter_bump("gate_rejected")
        return Decision(action="ignore", reason=reason)

    related = find_similar(
        store, cand.content, type=cand.type, subtype=cand.subtype,
        subject_type=cand.subject_type, subject_name=cand.subject_name,
        domain_key=cand.domain_key, user_id=cand.user_id, now=now,
    )
    slot = store.same_slot(
        subject_type=cand.subject_type, subject_name=cand.subject_name,
        type=cand.type, subtype=cand.subtype, domain_key=cand.domain_key,
        user_id=cand.user_id,
    )

    # ① 显式取代（memory_supersede / memory_update 指定了 id）
    if cand.supersede_id:
        return _commit_supersede(store, cand, cand.supersede_id, related, now,
                                 reason="调用方显式指定取代")

    # ② 同槽位等价 → TOUCH：只加固，不新增
    for old in slot:
        if _equivalent(cand.content, old):
            touched = _touch(store, old, cand, now)
            _count_op(store, "touch")
            return Decision(
                action="touch",
                reason=f"与已有记忆 {old['id']} 等价，只提升置信度",
                memory=touched, related=related,
            )

    # ③ 同槽位互斥 → 按置信度分级处理（模块 5）。
    #
    # 为什么不再一律自动取代：自动取代是不可逆的语义变更（旧条目标成"已被取代"，
    # 默认不再注入）。一个置信度 0.65 的推断把一条用户亲口说的 0.95 覆盖掉，
    # 用户只会觉得"它怎么忘了"。所以按"谁说的、多确定"分三档：
    #   - 显式且 ≥0.8  → 自动 SUPERSEDE（用户刚刚明确纠正过）
    #   - 0.6–0.8       → 并存，新条目打 metadata.conflict_with（冲突是 feature）
    #   - <0.6          → 丢弃并记日志（宁可不记，也别制造一条可疑记忆）
    # 另外 inferred 永不自动取代任何东西。
    for old in slot:
        if not _exclusive({"content": cand.content}, old):
            continue
        old_conf = float(old.get("confidence") or 0.0)
        explicit = bool(cand.explicit)
        conf = float(cand.confidence)
        if explicit and conf >= SUPERSEDE_AUTO_CONFIDENCE:
            return _commit_supersede(
                store, cand, old["id"], related, now,
                reason=(f"事实变了：用户明确纠正（置信度 {conf:.2f}），"
                        f"与 {old['id']}（置信度 {old_conf:.2f}）互斥"),
            )
        if conf >= SUPERSEDE_KEEP_CONFIDENCE:
            cand.metadata = dict(cand.metadata or {})
            cand.metadata["conflict_with"] = old["id"]
            saved = _insert(store, cand, now, reason=f"与 {old['id']} 冲突，置信不足，两条并存")
            _count_op(store, "add")
            return Decision(
                action="add",
                reason=(f"与 {old['id']} 互斥，但置信度 {conf:.2f} 不足以自动取代；"
                        "已并存并标注冲突，等用户确认"),
                memory=saved, related=related,
            )
        _discarded(cand, f"与 {old['id']} 冲突且置信度 {conf:.2f} 过低，未写入", store)
        store.counter_bump("supersede_low_confidence")
        return Decision(
            action="ignore",
            reason=f"与已有记忆 {old['id']} 冲突，且置信度 {conf:.2f} 太低，不写入",
            related=related,
        )

    # ④ 同槽位互补且已有 ≥2 条 → 仍 ADD（不丢新信息），附合并建议
    suggestion = None
    if len(slot) >= 2:
        suggestion = {
            "ids": [m["id"] for m in slot] + ["<new>"],
            "hint": "同槽位已有 ≥2 条互补事实，可用 memory_merge 收成一条",
        }

    saved = _insert(store, cand, now, reason="新增")
    _count_op(store, "add")
    return Decision(
        action="add",
        reason="新事实，且同槽位没有等价或互斥条目",
        memory=saved, related=related, merge_suggestion=suggestion,
    )


def _equivalent(content: str, old: dict) -> bool:
    a, b = normalize(content), normalize(old.get("content") or "")
    if a == b:
        return True
    ta = set(split_keywords(auto_keywords(content)))
    tb = set(split_keywords(old.get("keywords")))
    if not ta or not tb:
        return False
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.8


def _new_row(cand: Candidate, now: str) -> dict:
    return {
        "id": new_id(),
        "user_id": cand.user_id,
        "type": cand.type,
        "subtype": cand.subtype,
        "subject_type": cand.subject_type,
        "subject_name": cand.subject_name,
        "domain_key": cand.domain_key,
        "content": cand.content.strip(),
        "keywords": join_keywords(cand.keywords or auto_keywords(cand.content)),
        "confidence": round(float(cand.confidence), 3),
        "importance": cand.importance,
        "injection_policy": cand.injection_policy,
        "scope": cand.scope,
        "status": "active",
        "source_message_id": cand.source_message_id,
        "source_conversation_id": cand.source_conversation_id,
        "valid_from": cand.valid_from or now,
        "valid_until": cand.valid_until,
        "superseded_by": None,
        "created_at": now,
        "updated_at": now,
        "metadata": cand.metadata or {},
        "inject_count": 0,
        "use_count": 0,
    }


def _insert(store: Store, cand: Candidate, now: str, *, reason: str) -> dict:
    row = _new_row(cand, now)
    store.check_row(row)
    return store.insert(row, reason=reason, actor=_actor(cand))


def _actor(cand: Candidate) -> str:
    return "user" if cand.explicit else "inferred"


def _touch(store: Store, old: dict, cand: Candidate, now: str) -> dict:
    conf = min(CONFIDENCE_CAP, float(old["confidence"]) + CONFIDENCE_STEP)
    fields = {"confidence": round(conf, 3), "updated_at": now}
    if cand.source_message_id:
        fields["source_message_id"] = cand.source_message_id
    return store.update_fields(
        old["id"], fields, reason="等价重复出现，加固置信度", actor=_actor(cand),
        action="touch",
    )


def _count_op(store: Store, action: str) -> None:
    """写操作分布计数（模块 6）。只在真的落库之后调用。"""
    store.counter_bump(f"op_{action}")


def _commit_supersede(
    store: Store, cand: Candidate, old_id: str, related: list[dict], now: str, *, reason: str,
) -> Decision:
    """旧条目 → superseded（valid_until/superseded_by），新条目 → active，同一事务。"""
    with store.tx():
        old = store.get(old_id)
        if old is None:
            raise InvariantError(f"要取代的记忆不存在：{old_id}")
        if old["status"] != "active":
            raise InvariantError(
                f"{old_id} 当前 status={old['status']}，只能取代 active 条目"
            )
        new_row = _new_row(cand, now)
        store.check_row(new_row)
        # 先插新行：memories.superseded_by 是指向 memories(id) 的外键，
        # 新行还不存在时给旧行写 superseded_by 会直接 FOREIGN KEY constraint failed。
        saved = store.insert(new_row, reason=reason, actor=_actor(cand))
        store.update_fields(
            old_id,
            {
                "status": "superseded",
                "valid_until": now,
                "superseded_by": saved["id"],
                "updated_at": now,
            },
            reason=reason,
            actor=_actor(cand),
            action="supersede",
        )
    _count_op(store, "supersede")
    return Decision(action="supersede", reason=reason, memory=saved, related=related)


# ---------------------------------------------------------------- 显式操作


def preview(store: Store, cand: Candidate, *, now: str | None = None) -> dict:
    """只算不写：给"候选 ≠ 记忆"留出第一道接口（W2）。

    写入管线的前半段是"判断"（归一化 → 校验 → 价值闸门 → 查重/冲突），后半段才是
    "落库"。这里把前半段单独暴露出来，用于未来 Memory Formation（先攒候选、再决策），
    也让调用方能在写之前问一句"这条会被怎么处理"。

    ``would`` 的取值与 ``consider()`` 的 action 对齐：add / touch / supersede / ignore。
    注意这里**不写计数、不写观测、不写丢弃日志**——预览本身不应产生副作用。
    """
    now = now or now_iso()
    if not cand.explicit:
        cand.confidence = min(float(cand.confidence), INFERRED_CONFIDENCE_CAP)
        if cand.injection_policy == "always":
            cand.injection_policy = "contextual"
        if (cand.type == "profile" and cand.subtype == "interaction"
                and cand.scope == "global" and cand.injection_policy == "contextual"):
            cand.injection_policy = "on_demand"

    validate(cand)
    reason = value_gate(cand, store=None)
    if reason:
        return {"would": "ignore", "reason": reason, "duplicates": [], "conflicts": [], "slot_active": 0}

    related = find_similar(
        store, cand.content, type=cand.type, subtype=cand.subtype,
        subject_type=cand.subject_type, subject_name=cand.subject_name,
        domain_key=cand.domain_key, user_id=cand.user_id, now=now,
    )
    slot = store.same_slot(
        subject_type=cand.subject_type, subject_name=cand.subject_name,
        type=cand.type, subtype=cand.subtype, domain_key=cand.domain_key,
        user_id=cand.user_id,
    )
    duplicates = [r for r in related if r["relation"] == "equivalent"]
    conflicts = [r for r in related if r["relation"] == "exclusive"]

    if duplicates:
        return {"would": "touch", "reason": f"与 {duplicates[0]['memory']['id']} 等价，只会提升置信度",
                "duplicates": duplicates, "conflicts": conflicts, "slot_active": len(slot)}
    if cand.supersede_id:
        return {"would": "supersede", "reason": "调用方显式指定取代",
                "duplicates": [], "conflicts": conflicts, "slot_active": len(slot)}
    if conflicts and cand.explicit and float(cand.confidence) >= SUPERSEDE_AUTO_CONFIDENCE:
        return {"would": "supersede",
                "reason": f"与 {conflicts[0]['memory']['id']} 互斥且置信度足够，会自动取代",
                "duplicates": [], "conflicts": conflicts, "slot_active": len(slot)}
    return {"would": "add", "reason": "没有等价或可自动取代的条目",
            "duplicates": [], "conflicts": conflicts, "slot_active": len(slot)}


def update(store: Store, memory_id: str, fields: dict, *, reason: str) -> dict:
    """同一条事实改措辞/补细节。不改"事实本身"，事实变了要用 supersede。"""
    # subtype 可以改（同一角色内的细分修正，例如 identity → preference 是错的，
    # 但 "identity" 写错成别的 profile 子类是常见笔误）；type 与 subject_type 不能改——
    # 那两类变了说明"这是另一条事实"，必须新增一条，否则历史会读成"同一条改了口径"。
    allowed = {"content", "keywords", "importance", "injection_policy", "confidence",
               "subtype", "domain_key", "scope", "metadata", "subject_name", "valid_until"}
    bad = set(fields) - allowed
    if bad:
        raise CandidateError(
            f"update 不允许改这些字段：{sorted(bad)}；"
            f"可用：{sorted(allowed)}（type / subject_type 变了请新增一条）"
        )
    old = store.get(memory_id)
    if old is None:
        raise InvariantError(f"记忆不存在：{memory_id}")
    if "subtype" in fields and fields["subtype"] not in const.SUBTYPES.get(old["type"], ()):
        raise CandidateError(
            f"subtype 只能是 {'/'.join(const.SUBTYPES.get(old['type'], ()))} 之一"
            f"（{old['type']} 类型下），收到 {fields['subtype']!r}"
        )
    if old["status"] != "active":
        raise InvariantError(f"{memory_id} 当前 status={old['status']}，只能修改 active 条目")
    payload = dict(fields)
    # 枚举先在这里校验一遍再落库：让 SQLite 的 CHECK 报错漏到界面上，等于把表结构
    # 暴露给用户，而且用户看不懂 "CHECK constraint failed: importance IN (...)"。
    for key, allowed_values in (("importance", const.IMPORTANCE),
                                ("injection_policy", const.POLICIES),
                                ("scope", const.SCOPES)):
        if key in payload and payload[key] not in allowed_values:
            raise CandidateError(
                f"{key} 只能是 {'/'.join(allowed_values)} 之一，收到 {payload[key]!r}"
            )
    if "confidence" in payload:
        try:
            payload["confidence"] = float(payload["confidence"])
        except (TypeError, ValueError) as exc:
            raise CandidateError(f"confidence 必须是 0–1 的小数，收到 {payload['confidence']!r}") from exc
        if not 0.0 <= payload["confidence"] <= 1.0:
            raise CandidateError(f"confidence 必须在 0–1 之间，收到 {payload['confidence']!r}")
    if "content" in payload:
        payload["content"] = str(payload["content"]).strip()
        if len(payload["content"]) < MIN_CONTENT_CHARS:
            raise CandidateError(f"内容太短（至少 {MIN_CONTENT_CHARS} 个字）")
        if len(payload["content"]) > MAX_CONTENT_CHARS:
            raise CandidateError(f"内容太长（最多 {MAX_CONTENT_CHARS} 个字）")
        if "keywords" not in payload:
            payload["keywords"] = join_keywords(auto_keywords(payload["content"]))
    if "keywords" in payload:
        # 面板/工具传进来可能是数组（["李四","考研"]），库里存的是空格分隔的字符串；
        # 不归一化会让 sqlite3 抛 "Error binding parameter: type 'list' is not supported"。
        payload["keywords"] = join_keywords(payload["keywords"])
    payload["updated_at"] = now_iso()
    return store.update_fields(memory_id, payload, reason=reason, actor="tool", action="update")


def merge(
    store: Store,
    ids: list[str],
    *,
    content: str,
    reason: str,
    importance: str | None = None,
    keywords: list[str] | None = None,
) -> dict:
    """把同槽位的多条收成一条（MERGE ≠ UPDATE）。

    新的那条是 active，被合并的条目转 archived（**不是 deleted**，
    因为它们的信息没作废，只是被概括了）。
    """
    if len(ids) < 2:
        raise CandidateError("merge 至少需要 2 条记忆")
    rows = store.get_many(ids)
    if len(rows) != len(set(ids)):
        raise InvariantError("有 id 不存在")
    for r in rows:
        if r["status"] != "active":
            raise InvariantError(f"{r['id']} 当前 status={r['status']}，只能合并 active 条目")
    slots = {(r["subject_type"], r["subject_name"], r["type"], r["subtype"], r["domain_key"])
             for r in rows}
    if len(slots) != 1:
        raise InvariantError("merge 只能作用于同一槽位的条目（主体/类型/子类型/领域必须一致）")

    first = rows[0]
    now = now_iso()
    cand = Candidate(
        content=content,
        type=first["type"],
        subtype=first["subtype"],
        subject_type=first["subject_type"],
        subject_name=first["subject_name"],
        domain_key=first["domain_key"],
        scope=first["scope"],
        importance=importance or max((r["importance"] for r in rows),
                                     key=lambda i: const.IMPORTANCE_VALUE[i]),
        injection_policy=max((r["injection_policy"] for r in rows),
                             key=lambda p: const.POLICIES.index(p)),
        confidence=max(float(r["confidence"]) for r in rows),
        keywords=keywords,
        source_message_id=first["source_message_id"],
        source_conversation_id=first["source_conversation_id"],
        metadata={"merged_from": ids},
        user_id=first["user_id"],
    )
    with store.tx():
        saved = _insert(store, cand, now, reason=f"合并 {len(ids)} 条：{reason}")
        for r in rows:
            store.set_status(
                r["id"], "archived",
                reason=f"已合并进 {saved['id']}",
                actor="tool", action="merge",
                extra={"updated_at": now},
            )
    _count_op(store, "merge")
    return saved


def forget(store: Store, memory_id: str, *, reason: str, hard: bool = False) -> dict:
    """用户说"忘掉这个"。默认软删（可 restore），hard=True 才物理清除。"""
    if hard:
        return purge(store, memory_id, reason=reason)
    return store.set_status(memory_id, "deleted", reason=reason, actor="user", action="delete")


def purge(store: Store, memory_id: str, *, reason: str) -> dict:
    """物理删除：**只允许对已 deleted 的条目做**，防止手滑。"""
    row = store.get(memory_id)
    if row is None:
        raise InvariantError(f"记忆不存在：{memory_id}")
    if row["status"] != "deleted":
        raise InvariantError(
            f"{memory_id} 当前 status={row['status']}；物理删除前必须先 forget（status=deleted）"
        )
    store.update_fields(memory_id, {}, reason=reason, actor="user", action="purge")
    store.conn.execute("DELETE FROM memory_change WHERE memory_id=?", (memory_id,))
    store.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return row

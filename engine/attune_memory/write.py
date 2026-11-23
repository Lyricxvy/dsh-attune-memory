"""写入管线（对齐 MEMORY_WRITE_PIPELINE.md）。

流程：Candidate → 校验 → Value Gate → Explicit/Inferred 约束 → 同槽位检索
→ 决策表 → 单事务落库（记忆行 + 时间轴）。

一条铁律：**不确定就不写，或者写成更弱的形态**。宁可漏记，不可记错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import const
from .search import _exclusive, find_similar, key_values
from .store import InvariantError, Store
from .text import auto_keywords, join_keywords, normalize, segments, split_keywords
from .util import now_iso, new_id, parse_iso

#: 内容长度上下界（P3 §2.1：太短没有信息量，太长是聊天记录不是记忆）
MIN_CONTENT_CHARS = 4
MAX_CONTENT_CHARS = 300

#: 推断出来的记忆 confidence 上限（P3 §4）
INFERRED_CONFIDENCE_CAP = 0.7

#: TOUCH 每次 +0.05，封顶 0.95（永远不给 1.0，1.0 意味着"不可错"）
CONFIDENCE_STEP = 0.05
CONFIDENCE_CAP = 0.95
SUPERSEDE_AUTO_CONFIDENCE = 0.8
#: 单次写入最多回看多少条同槽位记忆（W-2.3：槽位可以是几百条）
SLOT_SCAN_LIMIT = 30   # 显式 + ≥0.8 才允许自动取代旧条目
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
    #: 用户这一轮**明确陈述**过、且授权本次写入在置信度≥0.8 时取代冲突旧条。
    #: 默认 False：模型不主动声明，就按"推断"处理（压 0.7 / 禁 always / 禁自动取代）。
    #: 这是 deliberate opt-in 的深思屏障（计划书 §5.2）——它不保证真实性，只把
    #: "误标导致静默覆盖"从默认行为降级成必须主动踩油门。
    explicit: bool = False
    source_message_id: str | None = None
    source_conversation_id: str | None = None
    valid_from: str | None = None
    #: 已经知道什么时候失效（例如"这学期在准备考研"），没有就留空
    valid_until: str | None = None
    metadata: dict | None = None
    user_id: str = "default"
    #: 调用方明确要取代的条目（memory_update/_supersede 传入）
    supersede_id: str | None = None
    #: v0.3.0（W-3.2）时态四字段。temporal_kind 默认 current（"现在仍然成立"），
    #: 说过去的事标 historical，一次性的标 event 并给 event_date，
    #: 拿不准的标 uncertain（读路径会降权，但不假装它不存在）。
    temporal_kind: str = "current"
    #: 覆盖旧条时用的失效方式：superseded = 事实变了；corrected = 原来记错了。
    invalidation: str | None = None
    event_date: str | None = None
    last_verified: str | None = None

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
    if cand.temporal_kind not in const.TEMPORAL_KINDS:
        raise CandidateError(
            f"非法 temporal_kind：{cand.temporal_kind}；合法值：{list(const.TEMPORAL_KINDS)}"
        )
    if cand.invalidation is not None and cand.invalidation not in const.INVALIDATIONS:
        raise CandidateError(
            f"非法 invalidation：{cand.invalidation}；合法值：{list(const.INVALIDATIONS)}"
        )
    for name in ("event_date", "last_verified"):
        value = getattr(cand, name)
        if value is not None:
            try:
                parse_iso(value)
            except ValueError as exc:
                raise CandidateError(f"{name} 不是合法的 ISO 时间：{value}；形如 2026-01-31T00:00:00Z") from exc
    if cand.temporal_kind != "event" and cand.event_date is not None:
        raise CandidateError(
            f"event_date 只对 temporal_kind='event' 有意义（当前 temporal_kind={cand.temporal_kind}）"
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


def intent_gate(cand: Candidate, *, store: Store | None = None) -> str | None:
    """意图准入门（W-1.1）：返回"不该记"的理由；None 表示可以进价值闸门。

    三组确定性规则，放在 :func:`value_gate` **之前**——它们判的不是"这句话好不好"，
    而是"这类东西根本不该进长期记忆"：

    1. **可从文件验证**：项目状态、仓库结构、配置项。这些下次读一遍文件就有，
       记进库里只会过期成噪声（用户还会奇怪"它为什么记这个"）。
    2. **运行时状态 / 一次性安排**：只在当前窗口成立、或者只发生一次的东西，
       是 state 都不该是（它们没有"会持续一段时间"的语义），更不该是全局事实。
    3. **否定极性**：状态动词的否定式（"不考"）意味着**状态变了**。如果照原样落库，
       下次检索会把"不考数学一"当成"考数学一"的同义句召回——记反比不记更坏。
       句子里已经给出"改成什么"（改考/改为/而是…）时放行，交给决策表做取代。

    这三条对 ``explicit=True`` 同样生效：用户说"记住，我在这个窗口负责记忆分析"
    依然不该进库——那不是偏好，是工位。
    """
    content = (cand.content or "").strip()
    low = content.lower()

    for pat in const.RUNTIME_MARKERS:
        if pat in content:
            return _discarded(
                cand, f"运行时状态（命中「{pat}」）只在本会话成立，不该进长期事实层", store
            )
    for pat in const.FILE_VERIFIABLE_PATTERNS:
        if pat.lower() in low:
            return _discarded(
                cand, f"可从文件/仓库直接查到（命中「{pat}」），不需要记成记忆", store
            )
    for pat in const.ONE_OFF_PATTERNS:
        if pat in content:
            return _discarded(cand, f"一次性安排（命中「{pat}」），不是跨时间的长期事实", store)

    if not any(mark in content for mark in const.CORRECTION_MARKERS):
        for verb in const.NEGATION_STATE_VERBS:
            for neg in (f"不{verb}", f"取消{verb}", f"没有{verb}"):
                if neg in content:
                    return _discarded(
                        cand,
                        f"否定句式（「{neg}」）不记反向事实；请改成肯定表述（如「已改为…」）",
                        store,
                    )
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

    reason = intent_gate(cand, store=store)
    if reason:
        store.counter_bump("intent_rejected")
        return Decision(action="ignore", reason=reason)

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
        # W-2.3：槽位会随时间增长到几百条，而"这次要覆盖谁"只可能是最近写的那些。
        # 不封顶的话每次写入都要对整个槽位跑一遍 _equivalent/_exclusive（O(n²)）。
        limit=SLOT_SCAN_LIMIT,
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

    # ②.5 同槽位"换个说法" → 就地 UPDATE（W-4.3）。
    # 不算新增（会留两条几乎一样的句子），也不算取代（事实没变，历史里不该多一条
    # "已被取代"）。只改内容与关键词，置信度取两者较大者。
    for old in slot:
        if _wording_fix(cand.content, old):
            with store.tx():
                saved = store.update_fields(
                    old["id"],
                    {
                        "content": cand.content.strip(),
                        "keywords": join_keywords(cand.keywords or auto_keywords(cand.content)),
                        "confidence": round(max(float(old.get("confidence") or 0), float(cand.confidence)), 3),
                        "updated_at": now,
                    },
                    reason=f"措辞修正：{cand.content.strip()[:40]}",
                    actor=_actor(cand),
                    action="update",
                )
            _count_op(store, "update")
            return Decision(
                action="update",
                reason=f"与 {old['id']} 是同一件事换个说法，就地改措辞",
                memory=saved, related=related,
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


def _lcs_ratio(a: str, b: str) -> float:
    """最长公共子序列占较短串的比例（0–1）。用于判断"是不是同一句话换个说法"。"""
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0]
        for j, other in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ch == other else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1] / min(len(a), len(b))


def _wording_fix(content: str, old: dict) -> bool:
    """同一件事换了个说法（W-4.3）：该就地改，而不是新增一条或取代旧条。

    判据是**机械的**，三条同时成立才算：
    1. 归一化后两个串的 LCS 占较短串 ≥ 0.92（"目前正在备考数学一" vs
       "正在备考数学一"：只多了"目前"两个字；而"备考数学一" vs "备考英语一"
       只到 0.86，不会被合并）；
    2. 不是互斥（有极性对立，或同一属性两个取值 —— 那是事实变了）；
    3. 关键值不冲突（key_values 抽出的同名属性取值必须相同）。
    拿不准就返回 False，走"新增"——宁可多一条，也不要把两件事合成一条。
    """
    a, b = normalize(content), normalize(old.get("content") or "")
    if not a or not b or a == b:
        return False
    if _exclusive({"content": content}, old):
        return False
    if _lcs_ratio(a, b) < 0.92:
        return False
    ka = key_values(content)
    kb = key_values(old.get("content") or "")
    for name in set(ka) & set(kb):
        if ka[name] != kb[name]:
            return False
    return True


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
        "temporal_kind": cand.temporal_kind,
        "invalidation": cand.invalidation,
        "event_date": cand.event_date,
        "last_verified": cand.last_verified,
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
        invalidation = cand.invalidation or "superseded"
        store.update_fields(
            old_id,
            {
                # W-3.3：corrected = 原来记错了 → archived（任何视图都不召回）；
                # superseded = 事实变了 → 仍是 superseded，as-of 查询可召回。
                "status": "archived" if invalidation == "corrected" else "superseded",
                "invalidation": invalidation,
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
        limit=SLOT_SCAN_LIMIT,
    )
    # 预览要报"槽位里一共几条 active"，不能拿回看窗口的大小冒充
    slot_total = store.count_slot(
        subject_type=cand.subject_type, subject_name=cand.subject_name,
        type=cand.type, subtype=cand.subtype, domain_key=cand.domain_key,
        user_id=cand.user_id,
    )
    duplicates = [r for r in related if r["relation"] == "equivalent"]
    conflicts = [r for r in related if r["relation"] == "exclusive"]

    if duplicates:
        return {"would": "touch", "reason": f"与 {duplicates[0]['memory']['id']} 等价，只会提升置信度",
                "duplicates": duplicates, "conflicts": conflicts, "slot_active": slot_total}
    if cand.supersede_id:
        return {"would": "supersede", "reason": "调用方显式指定取代",
                "duplicates": [], "conflicts": conflicts, "slot_active": slot_total}
    if conflicts and cand.explicit and float(cand.confidence) >= SUPERSEDE_AUTO_CONFIDENCE:
        return {"would": "supersede",
                "reason": f"与 {conflicts[0]['memory']['id']} 互斥且置信度足够，会自动取代",
                "duplicates": [], "conflicts": conflicts, "slot_active": slot_total}
    return {"would": "add", "reason": "没有等价或可自动取代的条目",
            "duplicates": [], "conflicts": conflicts, "slot_active": slot_total}


def update(store: Store, memory_id: str, fields: dict, *, reason: str) -> dict:
    """同一条事实改措辞/补细节。不改"事实本身"，事实变了要用 supersede。"""
    # subtype 可以改（同一角色内的细分修正，例如 identity → preference 是错的，
    # 但 "identity" 写错成别的 profile 子类是常见笔误）；type 与 subject_type 不能改——
    # 那两类变了说明"这是另一条事实"，必须新增一条，否则历史会读成"同一条改了口径"。
    allowed = {"content", "keywords", "importance", "injection_policy", "confidence",
               "subtype", "domain_key", "scope", "metadata", "subject_name", "valid_until",
               "temporal_kind", "event_date", "last_verified"}
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
    # W-2.2：改完的整行必须仍然是一条合法记忆。不再单点校验，而是用同一套 validate
    # 复核——否则 update 就成了"绕过写入口校验"的后门（#22）。
    merged = {**old, **{k: v for k, v in payload.items() if k != "keywords"}}
    merged["keywords"] = split_keywords(payload.get("keywords", old.get("keywords")))
    merged["metadata"] = payload.get("metadata", old.get("metadata")) or {}
    validate(Candidate(
        content=merged["content"], type=merged["type"], subtype=merged["subtype"],
        subject_type=merged["subject_type"], subject_name=merged.get("subject_name"),
        domain_key=merged.get("domain_key"), scope=merged["scope"],
        importance=merged["importance"], injection_policy=merged["injection_policy"],
        confidence=float(merged["confidence"]), keywords=merged["keywords"],
        explicit=True, valid_from=merged.get("valid_from"), valid_until=merged.get("valid_until"),
        metadata=merged["metadata"], user_id=merged.get("user_id", "default"),
    ))
    payload["updated_at"] = now_iso()
    with store.tx():
        updated = store.update_fields(memory_id, payload, reason=reason, actor="tool", action="update")
        # 生命周期（#22）：把 valid_until 改到过去，等于当场宣布这条失效。
        # 只"写进去"却继续注入，是最危险的一种不一致（库里有状态、行为上没状态）。
        if payload.get("valid_until") and payload["valid_until"] <= now_iso():
            updated = store.set_status(
                memory_id, "archived",
                reason=f"有效期为 {payload['valid_until']}，已过（update 时触发归档）",
                actor="tool", action="archive", extra={"updated_at": now_iso()},
            )
    return updated


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
    # W-2.2：合并出来的新条也是一条新记忆，必须过和 add 同一套校验（#13/#35）。
    # 以前这里直接 _insert，空内容/非法 importance 都能写进库。
    validate(cand)
    reject = value_gate(cand, store=None)
    if reject:
        raise CandidateError(f"合并后的内容没通过价值闸门：{reject}")
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


def forget(store: Store, memory_id: str, *, reason: str) -> dict:
    """用户说"忘掉这个"。**只能软删**（可 restore）。

    物理删除不在这里：它不可逆、且会连 memory_change 一起抹掉（无审计痕迹）。
    硬删只保留在 CLI（``memctl forget --hard`` = forget 然后 purge）与管理页，
    模型工具没有任何路径能直接触发。
    """
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
    # 被 supersede 链指着的条目不能直接删：superseded_by 是外键，删了会留下断链。
    refs = store.conn.execute(
        "SELECT count(*) AS n FROM memories WHERE superseded_by=?", (memory_id,)
    ).fetchone()["n"]
    if refs:
        raise InvariantError(
            f"{memory_id} 仍被 {refs} 条历史记忆的 superseded_by 指向；"
            "先处理这些历史条目（归档或改写），否则会把取代链删断"
        )
    # W-2.1：记 tombstone + 删主行必须在同一个事务里（#12）。
    # 以前是"更新流水（自带事务）→ 裸删流水 → 裸删记忆"，中途崩溃会留下
    # "流水没了、记忆还在"或者"记忆没了、流水还在"的不一致。
    # 另外流水**不再删除**：那条 action=purge 的记录就是 tombstone，审计链必须留着（#36）。
    with store.tx():
        store._add_change(  # noqa: SLF001 - 同包内复用流水写入，避免再抄一份
            memory_id=memory_id, action="purge", old=row, new=None, reason=reason,
            actor="user", source_message_id=row.get("source_message_id"),
        )
        gone = [
            r["gram"]
            for r in store.conn.execute(
                "SELECT gram FROM memory_gram WHERE memory_id=?", (memory_id,)
            ).fetchall()
        ]
        if gone:
            store.conn.execute("DELETE FROM memory_gram WHERE memory_id=?", (memory_id,))
            store._df_dec(gone)
        store.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return row

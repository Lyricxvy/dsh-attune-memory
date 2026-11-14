"""Memory Engine — 常量与枚举（对齐 MEMORY_SCHEMA_V1.md / MEMORY_RETRIEVAL_V1.md）。

本文件是"魔法数字"的唯一出处：任何地方要用到半衰期、重要性映射、预算，
都必须从这里取，不许就地写死。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 枚举

TYPES = ("profile", "domain", "state", "episodic")

#: type -> 合法 subtype 集合（P2 §1，应用层校验；SQLite 跨列 CHECK 会变脆）
SUBTYPES: dict[str, tuple[str, ...]] = {
    "profile": ("identity", "preference", "interaction", "skill", "constraint"),
    "domain": ("convention", "fact", "decision", "pitfall"),
    "state": ("goal", "activity", "situation"),
    "episodic": ("event", "decision", "lesson"),
}

SUBJECT_TYPES = ("user", "person", "project", "org", "other")

IMPORTANCE = ("key", "important", "normal", "low")

#: 枚举 -> 固定数值（ADR-009：模型只填枚举，不填主观分）
IMPORTANCE_VALUE: dict[str, float] = {
    "key": 1.0,
    "important": 0.8,
    "normal": 0.5,
    "low": 0.25,
}

POLICIES = ("always", "contextual", "on_demand", "never")

SCOPES = ("global", "domain", "task")

STATUSES = ("active", "superseded", "archived", "deleted")

CHANGE_ACTIONS = (
    "add",
    "update",
    "touch",
    "supersede",
    "merge",
    "archive",
    "delete",
    "restore",
    "purge",
)

# ---------------------------------------------------------------- 检索

#: 半衰期（天）—— P5 §2.3
HALF_LIFE_DAYS: dict[str, float] = {
    "profile": 3650.0,
    "domain": 365.0,
    "state": 90.0,
    "episodic": 180.0,
}

#: 新鲜度下限：老记忆只是靠后，不会彻底消失
FRESHNESS_FLOOR = 0.2

#: 候选集上限：超过说明结构化过滤失效，应修写入质量而不是扫更多行
MAX_CANDIDATES = 200

#: 命中闸门：`Relevance × Confidence × ContextMatch` 低于它算"没命中"，不进结果。
#: **不是 Final Score 的闸门**：importance / freshness / conflict 都是乘数，用
#: Final Score 当闸门会让 importance=normal（0.5）或已过期的条目永远出不来——
#: 那样"过时降权"就悄悄变成了"过时消失"，比直接删掉更危险（用户还以为记着）。
#: 这个闸门只管"算不算命中"；上面三个因子只调排序与标注（P0 约束 B）。
MIN_SCORE = 0.15
MIN_RELEVANCE = MIN_SCORE

K_DEFAULT = 5
K_MIN = 4
K_MAX = 6
K_HARD_MAX = 20

#: 相关性饱和参数：relevance = raw / (raw + SATURATION)
RELEVANCE_SATURATION = 2.0
#: 二元组兜底路的权重（R2）：coverage=1.0 时 raw=2.0，rel≈0.5，够过 0.15 命中闸门。
BIGRAM_WEIGHT = 2.0

# ---------------------------------------------------------------- 读取预算（P4 §5）

BUDGET = {
    "stable_max_items": 6,
    "stable_max_chars": 500,
    "state_max_items": 3,
    "state_max_chars": 200,
    # profile 条目绝大多数走 C 通道；只有"太长/置信度不够进 C"的才会落到这里。
    # 给两行的位置，是因为这类条目（比如"这个人在某类题上卡在哪"）往往比领域约定更值钱。
    "profile_max_items": 2,
    "profile_max_chars": 160,
    "domain_max_items": 5,
    "domain_max_chars": 300,
    "total_max_chars": 1000,
}

# ---------------------------------------------------------------- 写入判定

#: 时效性噪声（V1 不成立 -> IGNORE）
#: V1 的时效识别就是这份词表：它不试图理解句子，只拦"说了等于没说"的那几类。
#: 局限写在 P5 §2 的表里——词表外的瞬时状态会被当成事实收下（V2 换成分类器）。
#: 词条要短而泛：判的是"这句话有没有跨时间价值"，不是"有没有出现某个名词"。
TRANSIENT_PATTERNS = (
    "今天天气",
    "天气不错",
    "现在有点",
    "目前有点",
    "刚才",
    "刚刚",
    "这条消息",
    "今天心情",
    "晚饭",
    "睡一觉",
    # 身体/情绪的一过性状态：这句话明天就不成立了，收下来只会污染画像。
    # 只收"明显是此刻"的说法（有点困 / 刚吃完），不收"在忙项目"这种可能是长期状态的。
    "有点困",
    "有点累",
    "有点饿",
    "有点烦",
    "有点冷",
    "有点热",
    "刚吃完",
    "刚吃",
    "正在吃",
    "待会儿",
    "一会儿再",
)

#: 一次性任务指令（V3 不成立 -> IGNORE）
TASK_PATTERNS = (
    "帮我",
    "请把",
    "麻烦",
    "改一下",
    "改这段",
    "运行一下",
    "跑一下",
    "看一下这个",
    "翻译这句",
    "写个函数",
)

#: 提问句式（问题不是事实，一律 IGNORE；问号本身会被 normalize 去掉，所以按词判）
QUESTION_PATTERNS = (
    "怎么做",
    "怎么办",
    "是什么",
    "为什么",
    "什么意思",
    "多少",
    "哪里",
    "哪个",
    "如何",
    "行不行",
    "对不对",
    "能不能",
    "可不可以",
    "是不是",
    "请问",
)

#: 第三方主体标记：content 里出现它们而 subject_type=user 是坍缩信号
THIRD_PARTY_MARKERS = (
    "我朋友",
    "我同事",
    "我妈",
    "我爸",
    "我老板",
    "我对象",
    "我室友",
    "我们组",
    "我老师",
    "我同学",
)

#: 极性词对（V1 只收这些低频互斥词，不用"是/不是"）
POLARITY_PAIRS = (
    ("需要", "不需要"),
    ("支持", "不支持"),
    ("启用", "禁用"),
    ("开启", "关闭"),
    ("保留", "删除"),
    ("加上", "去掉"),
    ("使用", "不使用"),
    ("成功", "失败"),
    ("可用", "不可用"),
    ("推荐", "不推荐"),
    ("必须", "禁止"),
    ("考", "不考"),
)

#: 指代词：content 含它们说明脱离上下文读不懂
ANAPHORA = ("他", "她", "它", "这个", "那个", "上面", "刚才", "该", "此")

#: always 注入的类型白名单（W6）。always 是"每轮都进 system prompt"的名额，
#: 只给跨任务长期成立的交互方式/约束/身份；领域做法只该在该领域出现，
#: 一次性任务更不该常驻。键是 type，值是允许的 subtype。
ALWAYS_ALLOWED = {
    "profile": ("identity", "interaction", "constraint"),
}

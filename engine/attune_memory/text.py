"""文本归一化与词法匹配（对齐 MEMORY_RETRIEVAL_V1.md §2.2）。

V1 的取舍，写清楚以免后人"优化"回 bigram：
* 查询侧**不做自由分词**（无词典分不准，硬分会制造噪声）。
* 用库里的 ``keywords``（写入方填的检索词）去切查询——词表就是"人会用来搜它的词"。
* 兜底路：按标点切片段 + 去停用字，与 content 做子串匹配。
* 2-gram 滑窗 + FTS5 属于 V2，本文件刻意不含。
"""
from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[\s\u3000-\u303f\uff00-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
                       r"!-/:-@\[-`{-~，。、；：？！…—－（）【】《》‘’“”·]+")

#: 片段路要去掉的停用字（单字，直接删除，不切分）
STOP_CHARS = "的了是在我你他她它吗呢吧啊就还也都和与及或把被给对从向于其之这那些个"

_EN_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "and", "or", "in", "on",
    "for", "with", "what", "how", "why", "do", "does", "did", "i", "you", "it", "that",
    "this", "my", "me", "we", "us", "can", "should", "would", "please",
}


def normalize(text: str) -> str:
    """NFKC + 小写 + 去标点，保留字母数字与 CJK。

    用途：让 ``Python``/``ｐｙｔｈｏｎ``/``python`` 等价，让子串匹配不被标点打断。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _PUNCT_RE.sub("", s)
    return s.strip()


def grams(text: str, n: int = 2) -> list[str]:
    """字符 n-gram（默认二元组），给倒排索引做"近似子串"召回。

    为什么索引要存 n-gram 而不是只存 keywords：旧的 ``near_keywords`` 是
    ``term in (keywords + content)`` 的**子串**语义，只索引 keywords 会漏召回
    （"极限题"不在 keywords 里、但出现在正文里）。存二元组能把候选集缩到
    "所有 gram 都命中"的少数行，再回表做一次子串校验，结果与全表扫一致。
    """
    norm = re.sub(r"\s+", "", normalize(text))
    if len(norm) < n:
        return []
    return [norm[i:i + n] for i in range(len(norm) - n + 1)]


def has_gram_index_support(term: str) -> bool:
    """单字词无法用二元组索引召回，交给调用方走兜底路径。"""
    return len(normalize(term)) >= 2


def split_keywords(keywords: str | list[str] | None) -> list[str]:
    """keywords 列是空格分隔的字符串；也接受 list。返回去重、去空的列表。"""
    if not keywords:
        return []
    if isinstance(keywords, (list, tuple)):
        raw = [str(k) for k in keywords]
    else:
        raw = re.split(r"[\s,，;；]+", str(keywords))
    seen: list[str] = []
    for k in raw:
        k = k.strip()
        if k and k not in seen:
            seen.append(k)
    return seen


def join_keywords(keywords: str | list[str] | None) -> str:
    return " ".join(split_keywords(keywords))


_SPLIT_RE = re.compile(r"[\s，。、；：？！,.;:?!（）()【】\[\]“”\"'—·…]+")
_STOP_SET = frozenset(STOP_CHARS)

#: 多字虚词：查询里它们把实词粘成一段，必须当分隔符。
#: 只有单字停用字是不够的——"传热学怎么补"切不出"传热学"就永远搜不到。
_MULTI_STOP = (
    "为什么", "怎么样", "怎么", "什么", "如何", "是否", "能不能", "有没有", "可不可以",
    "不可以", "不需要", "没有", "可以", "请问", "帮我", "帮忙", "一下", "告诉", "记得",
    "知道", "觉得", "看看", "试试", "应该", "我们", "你们", "他们", "这个", "那个",
    "一些", "现在", "之前", "上次", "时候", "关于", "问题", "东西", "事情", "还是",
    "或者", "因为", "所以", "但是", "如果", "然后", "以及", "已经", "一直", "总是",
    "从来", "偶尔", "可能", "大概", "比较", "非常", "特别", "真的", "确实", "其实",
    "反正", "而且", "不过", "虽然", "即使", "除了", "为了", "等到", "最近", "目前",
)
_MULTI_RE = re.compile("|".join(sorted(_MULTI_STOP, key=len, reverse=True)))


def _pieces(text: str) -> list[str]:
    """把一段话切成"实词片段"：标点、虚词、停用字处都断开（不删字符）。

    停用字**当分隔符而不是删除**是关键：把"数学一的复习安排"删成
    "数学一复习安排"会得到一个谁也不匹配的 7 字长串；切开才能得到
    "数学一" 和 "复习安排" 两个可匹配的片段。
    """
    if not text:
        return []
    out: list[str] = []
    for part in _SPLIT_RE.split(text):
        part = normalize(part)
        if not part:
            continue
        for sub in _MULTI_RE.split(part):
            if not sub:
                continue
            buf: list[str] = []
            for ch in sub:
                if ch in _STOP_SET:
                    if buf:
                        out.append("".join(buf))
                        buf = []
                else:
                    buf.append(ch)
            if buf:
                out.append("".join(buf))
    return out


def _chunk(piece: str, size: int = 4) -> list[str]:
    """超长片段按固定长度粗切（不做词典分词，也不是 2-gram 滑窗）。

    中文没有空格，一段连续汉字可能 10 个字没有停用字（"用户主要备考数学一"）。
    整段拿去匹配几乎必然失败，所以按 4 字粗切成"能落地的片段"。
    """
    if len(piece) <= 8:
        return [piece]
    return [piece[i:i + size] for i in range(0, len(piece), size)]


def auto_keywords(content: str, limit: int = 8) -> list[str]:
    """兜底关键词：只在写入方没给 keywords 时用。

    它不会比人填得更好，只是保证"至少有东西可搜"——人填的 keywords 永远是首选。
    """
    if not content:
        return []
    out: list[str] = []
    for piece in _pieces(content):
        for chunk in _chunk(piece):
            if 2 <= len(chunk) and chunk not in out:
                out.append(chunk)
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


def segments(query: str) -> list[str]:
    """查询切片（片段路用）：按标点与停用字切、超长粗切、保留 2 字以上。"""
    if not query:
        return []
    out: list[str] = []
    for piece in _pieces(query):
        if len(piece) < 2:
            continue
        if piece in _EN_STOP:
            continue
        for chunk in _chunk(piece):
            if len(chunk) >= 2 and chunk not in out:
                out.append(chunk)
    return out


def match_vocabulary(query: str, vocab: list[str]) -> list[str]:
    """词表路：返回 ``vocab`` 中作为子串出现在查询里的词，最长优先、去重叠。

    这不是 n-gram：词表来自库里的 ``keywords``，是"已经切好的词"。
    """
    q = normalize(query)
    if not q:
        return []
    hits: list[tuple[int, str]] = []
    for v in vocab:
        nv = normalize(v)
        if len(nv) < 2:
            continue
        idx = q.find(nv)
        if idx >= 0:
            hits.append((idx, nv))
    # 最长优先：同一位置上被更长词覆盖的短词丢弃
    hits.sort(key=lambda t: (-len(t[1]), t[0]))
    kept: list[tuple[int, str]] = []
    for idx, term in hits:
        overlapped = any(not (idx + len(term) <= i or idx >= i + len(t)) for i, t in kept)
        if not overlapped:
            kept.append((idx, term))
    kept.sort(key=lambda t: t[0])
    return [t for _, t in kept]


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def contains_any(text: str, needles) -> str | None:
    for n in needles:
        if n in text:
            return n
    return None

#: 中文二元组的连续码点范围（只对 CJK 段切，英文/数字保持整词）。
_CJK = r"\u4e00-\u9fff"


def bigrams(text: str) -> list[str]:
    """把中文段切成二元组（R2）："北京市" → ["北京","京市"]。

    为什么是二元组而不是更好听的分词：V1 没有词典，二元组在中文里几乎不会切错，
    而且"跨词查询"（用"北京"查"北京市"）靠它就能命中。英文/数字整词保留，
    否则 "me" 会命中 "memory"。
    """
    out: list[str] = []
    for run in re.findall(f"[{_CJK}]+", normalize(text)):
        if len(run) == 1:
            out.append(run)
            continue
        for i in range(len(run) - 1):
            out.append(run[i:i + 2])
    return out

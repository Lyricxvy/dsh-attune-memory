"""小工具：时间、id、base36。与记忆语义无关，纯工程。"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def fmt_iso(dt: datetime) -> str:
    """把 datetime 写成**定宽毫秒** UTC 串：``2026-11-22T13:07:09.042Z``。

    v0.3.0（W-2.4）从秒精度升到毫秒：同一秒内的两次写入，秒级时间戳相等，
    ``ORDER BY updated_at`` 会退化成"随便排"，写入顺序不可复现（#30）。
    定宽（永远 3 位毫秒）是为了让字符串字典序仍然等于时间序。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def now_iso(now: datetime | None = None) -> str:
    """统一的时间表示：UTC、毫秒精度、带 Z。"""
    return fmt_iso(now or datetime.now(timezone.utc))


def parse_iso(s: str) -> datetime:
    """容错解析：既认新毫秒格式，也认旧秒级格式（存量数据不迁移也能读）。"""
    text = str(s).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # 最后的兜底：老库可能只有秒级且没有 Z（手工写入的脏数据）
        dt = datetime.strptime(str(s).strip(), _ISO_FMT)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_iso(s: str) -> str:
    """把任意 ISO 时间规整成定宽毫秒格式（迁移用；语义不变，只统一写法）。"""
    return fmt_iso(parse_iso(s))


def as_iso(value: str | datetime | None, default_now: bool = True) -> str | None:
    if value is None:
        return now_iso() if default_now else None
    if isinstance(value, datetime):
        return now_iso(value)
    return now_iso(parse_iso(value))


def days_between(older: str, newer: str) -> float:
    return (parse_iso(newer) - parse_iso(older)).total_seconds() / 86400.0


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


_counter = 0


def new_id(now: datetime | None = None) -> str:
    """``m-<36进制毫秒>-<4位36进制随机>``，时间有序、可读、不撞。"""
    global _counter
    _counter = (_counter + 1) % 1296
    ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    return f"m-{_base36(ms)}-{_base36(_counter * 36 + random.randrange(36)).rjust(2, '0')}"


def sleep_ms(ms: int) -> None:  # pragma: no cover - 仅调试用
    time.sleep(ms / 1000.0)

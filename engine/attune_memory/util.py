"""小工具：时间、id、base36。与记忆语义无关，纯工程。"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso(now: datetime | None = None) -> str:
    """统一的时间表示：UTC、秒精度、带 Z。

    秒精度是刻意的：``valid_until``/``created_at`` 之间要用字符串比较，
    一旦混入微秒，字典序就不再等于时间序。
    """
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_ISO_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, _ISO_FMT).replace(tzinfo=timezone.utc)


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

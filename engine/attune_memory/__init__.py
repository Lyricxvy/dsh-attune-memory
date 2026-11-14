"""Memory Engine（V1）。

独立工程：只用 Python 标准库 + ``sqlite3``。不 import harness、不 import cordis、
不认识 DSH 的任何概念——它只提供"记忆"这一件事，接入由宿主侧（Node 插件）负责。

对外只暴露两样东西：``Memory`` 门面，和 ``Candidate``/``Decision`` 两个数据结构。
"""
from __future__ import annotations

from .const import (  # noqa: F401
    IMPORTANCE,
    IMPORTANCE_VALUE,
    POLICIES,
    SCOPES,
    SUBJECT_TYPES,
    SUBTYPES,
    TYPES,
)
from .db import SCHEMA_VERSION, connect, init_schema  # noqa: F401
from .manager import Memory  # noqa: F401
from .write import Candidate, CandidateError, Decision  # noqa: F401

__version__ = "0.2.0"

__all__ = [
    "Memory",
    "Candidate",
    "CandidateError",
    "Decision",
    "SCHEMA_VERSION",
    "connect",
    "init_schema",
    "TYPES",
    "SUBTYPES",
    "SUBJECT_TYPES",
    "IMPORTANCE",
    "IMPORTANCE_VALUE",
    "POLICIES",
    "SCOPES",
]

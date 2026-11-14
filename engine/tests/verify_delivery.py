"""一次性交付核验：schema 形状 + 文档必备小节。不参与发布，仅用于自检。"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from attune_memory.db import connect, init_schema  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def schema_shape():
    conn = connect(":memory:")
    init_schema(conn)
    tables = sorted(
        r[0] for r in conn.execute("select name from sqlite_master where type = 'table'")
        if not r[0].startswith("sqlite_")
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
    idx = sorted(
        r[0] for r in conn.execute("select name from sqlite_master where type = 'index'")
        if r[0].startswith("idx_")
    )
    print("tables      :", tables)
    print("memories 列  :", len(cols))
    print("索引        :", len(idx), idx)
    missing = [
        c for c in (
            "id", "user_id", "type", "subtype", "subject_type", "subject_name",
            "domain_key", "content", "confidence", "importance", "status",
            "injection_policy", "scope", "source_message_id", "source_conversation_id",
            "valid_from", "valid_until", "superseded_by", "created_at", "updated_at",
            "metadata",
        ) if c not in cols
    ]
    print("缺列        :", missing or "无")


REQUIRED = {
    "MEMORY_SYSTEM_DESIGN_PRINCIPLES.md": ["User Model", "Interaction Model", "不做"],
    "ADR.md": ["Alternatives", "Rejected because", "Consequences"],
    "CURRENT_ENVIRONMENT_ANALYSIS.md": ["Read", "Write", "压缩"],
    "ADOPTION_DECISION_TABLE.md": ["采用"],
    "UNKNOWN_LIST.md": ["U-"],
    "MEMORY_SCHEMA_V1.md": ["profile", "domain", "state", "episodic", "injection_policy"],
    "MEMORY_WRITE_PIPELINE.md": ["IGNORE", "ADD", "UPDATE", "MERGE", "SUPERSEDE", "ARCHIVE", "DELETE"],
    "MEMORY_READ_PIPELINE.md": ["always", "contextual", "on_demand", "never"],
    "MEMORY_RETRIEVAL_V1.md": ["Top-K", "Final"],
    "MEMORY_LIFECYCLE.md": ["superseded", "archived", "deleted"],
    "HOST_INTEGRATION.md": ["agent/pre-step", "compaction"],
    "MEMORY_ADMIN.md": ["explain", "forget"],
    "MEMORY_EVAL_SUITE.md": ["A", "B", "C", "D", "E"],
    "MEMORY_V1_1_TUNING.md": ["缺陷", "修复"],
    "V2_V3_RESERVED.md": ["V2", "V3"],
}


def docs():
    bad = 0
    for name, needles in REQUIRED.items():
        path = ROOT / name
        if not path.exists():
            print(f"缺失文档: {name}")
            bad += 1
            continue
        text = path.read_text(encoding="utf-8")
        absent = [n for n in needles if n not in text]
        adr = ""
        if name == "ADR.md":
            adr_count = len(re.findall(r"^## ADR-\d+", text, re.M))
            adr = " ADR×%d" % adr_count
        print(f"{'OK ' if not absent else 'BAD'} {name:40s} {len(text):6d} 字符{adr}"
              + (f"  缺: {absent}" if absent else ""))
        bad += bool(absent)
    return bad


if __name__ == "__main__":
    schema_shape()
    print("-" * 60)
    sys.exit(1 if docs() else 0)

from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "evaluation" /
          "memweave-cross-agent-v1" / "compare_naive_fts.py")
SPEC = importlib.util.spec_from_file_location("compare_naive_fts", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def negative_case() -> dict:
    return {
        "case_id": "noise-1",
        "query": "What is the SQLite backup policy?",
        "memory_records": [{
            "id": "memory-1",
            "content": "SQLite WAL mode improves concurrent reads and writes.",
        }],
        "expected": {
            "decision": "abstain",
            "evidence_ids": [],
            "forbidden_ids": ["memory-1"],
            "shadow_probe": "none",
        },
    }


def test_naive_fts_injects_a_lexical_near_miss() -> None:
    case = negative_case()
    emitted, latency = MODULE.naive_fts(case, limit=3)
    row = MODULE.score_case(case, emitted, latency, "shadow-v1")

    assert emitted == ["memory-1"]
    assert row["decision_correct"] is False
    assert row["forbidden_injected"] is True
    assert row["unnecessary_records"] == 1
    assert row["unnecessary_utf8_bytes"] > 0


def test_memweave_abstention_scores_as_clean() -> None:
    row = MODULE.score_case(negative_case(), [], 1.5, "shadow-v1")
    summary = MODULE.summarize([row])

    assert summary["decision_accuracy"] == 1.0
    assert summary["negative_injection_rate"] == 0.0
    assert summary["forbidden_injection_rate"] == 0.0
    assert summary["unnecessary_utf8_bytes"] == 0

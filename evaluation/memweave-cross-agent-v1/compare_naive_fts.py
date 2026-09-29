"""Compare MemWeave with an intentionally naive SQLite FTS5 Top-K baseline.

The baseline uses the same deterministic synthetic cases and query tokeniser as
MemWeave, but deliberately omits project, lifecycle, evidence, supersession,
same-session, and minimum-relevance gates.  Any lexical match may therefore be
injected.  This isolates the value of governance and abstention without making
claims about downstream answer correctness or model task success.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from math import ceil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path[:0] = [str(ROOT), str(REPO / "src")]

from agent_knowledge_bridge.store import retrieval_tokens
from evaluation_profile import PROFILES, expected_for


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, ceil(p * len(ordered)) - 1)], 4)


def naive_fts(case: dict, limit: int) -> tuple[list[str], float]:
    """Return raw lexical Top-K results with no governance gates."""
    with sqlite3.connect(":memory:") as db:
        db.execute(
            "CREATE VIRTUAL TABLE memories USING fts5("
            "evidence_id UNINDEXED, content, tokenize='unicode61')"
        )
        db.executemany(
            "INSERT INTO memories(evidence_id, content) VALUES (?, ?)",
            [(record["id"], record["content"]) for record in case["memory_records"]],
        )
        tokens = retrieval_tokens(case["query"])
        if not tokens:
            return [], 0.0
        expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:64]
        )
        started = time.perf_counter()
        rows = db.execute(
            "SELECT evidence_id FROM memories WHERE memories MATCH ? "
            "ORDER BY bm25(memories) LIMIT ?",
            (expression, limit),
        ).fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000
    return [row[0] for row in rows], elapsed_ms


def summarize(rows: list[dict]) -> dict:
    positives = [row for row in rows if row["positive"]]
    negatives = [row for row in rows if not row["positive"]]
    return {
        "cases": len(rows),
        "decision_accuracy": round(
            sum(row["decision_correct"] for row in rows) / len(rows), 6
        ),
        "positive_evidence_recall": round(
            sum(row["evidence_recall"] for row in positives) / len(positives), 6
        ) if positives else None,
        "negative_injection_rate": round(
            sum(bool(row["emitted_ids"]) for row in negatives) / len(negatives), 6
        ) if negatives else None,
        "forbidden_injection_rate": round(
            sum(row["forbidden_injected"] for row in rows) / len(rows), 6
        ),
        "unnecessary_records": sum(row["unnecessary_records"] for row in rows),
        "unnecessary_utf8_bytes": sum(row["unnecessary_utf8_bytes"] for row in rows),
        "latency_p50_ms": percentile([row["latency_ms"] for row in rows], 0.50),
        "latency_p95_ms": percentile([row["latency_ms"] for row in rows], 0.95),
    }


def score_case(case: dict, emitted_ids: list[str], latency_ms: float, profile: str) -> dict:
    expected = expected_for(case, profile)
    expected_ids = set(expected["evidence_ids"])
    forbidden_ids = set(expected["forbidden_ids"])
    positive = expected["decision"] == "inject"
    emitted = list(dict.fromkeys(emitted_ids))
    emitted_set = set(emitted)
    records = {record["id"]: record for record in case["memory_records"]}
    unnecessary = [key for key in emitted if key not in expected_ids]
    return {
        "case_id": case["case_id"],
        "positive": positive,
        "emitted_ids": emitted,
        "decision_correct": bool(emitted) == positive,
        "evidence_recall": (
            len(emitted_set & expected_ids) / len(expected_ids) if positive else None
        ),
        "forbidden_injected": bool(emitted_set & forbidden_ids),
        "unnecessary_records": len(unnecessary),
        "unnecessary_utf8_bytes": sum(
            len(records[key]["content"].encode("utf-8"))
            for key in unnecessary if key in records
        ),
        "latency_ms": latency_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["calibration", "dev", "test", "all"], default="test")
    parser.add_argument("--profile", choices=PROFILES, default="shadow-v1")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be between 1 and 10")

    cases = [
        json.loads(line)
        for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    predictions = {
        row["case_id"]: row
        for row in (
            json.loads(line)
            for line in args.predictions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    missing = [case["case_id"] for case in cases if case["case_id"] not in predictions]
    if missing:
        raise ValueError(f"missing MemWeave predictions: {missing[:3]}")

    naive_rows: list[dict] = []
    memweave_rows: list[dict] = []
    for case in cases:
        naive_ids, naive_ms = naive_fts(case, args.limit)
        naive_rows.append(score_case(case, naive_ids, naive_ms, args.profile))
        prediction = predictions[case["case_id"]]
        memweave_rows.append(score_case(
            case, prediction.get("emitted_evidence_ids", []),
            float(prediction.get("adapter_ms", 0.0)), args.profile,
        ))

    naive = summarize(naive_rows)
    memweave = summarize(memweave_rows)
    byte_reduction = None
    if naive["unnecessary_utf8_bytes"]:
        byte_reduction = round(
            1 - memweave["unnecessary_utf8_bytes"] / naive["unnecessary_utf8_bytes"],
            6,
        )
    report = {
        "dataset_version": "mw-cross-agent-v1.0.0-candidate",
        "split": args.split,
        "profile": args.profile,
        "cases": len(cases),
        "top_k": args.limit,
        "baseline": (
            "SQLite FTS5/BM25 lexical Top-K using MemWeave retrieval tokens, with "
            "project, lifecycle, evidence, supersession, same-session, and "
            "minimum-relevance gates disabled"
        ),
        "naive_fts": naive,
        "memweave": memweave,
        "unnecessary_context_byte_reduction": byte_reduction,
        "limitations": [
            "Fully synthetic controlled cases; not production traffic.",
            "UTF-8 bytes are deterministic context volume, not model-token counts.",
            "Naive latency measures only an in-memory FTS query; MemWeave latency is the full local adapter path, so latency columns are not an apples-to-apples speed comparison.",
            "No LLM calls, final-answer grading, task-success measurement, or safety score.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

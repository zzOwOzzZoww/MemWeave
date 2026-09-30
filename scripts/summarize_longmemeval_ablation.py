"""Recover provisional metrics after a completed run fails its freeze gate."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import sqlite3

from evaluate_longmemeval_ablation import (
    ARMS, DATA_SHA256, REVISION, digest, metrics, paired, retirement_indices,
    semantic_result, summary, validate_rows, write_report,
)


def load_predictions(rows, path, repeats=2):
    known = {r["question_id"]: r for r in rows}
    grouped = {a: {} for a in ARMS}
    signatures = hashlib.sha256()
    for line in path.read_text(encoding="utf-8").splitlines():
        result = json.loads(line)
        key, arm, repeat = result["case_id"], result["variant"], result["repeat"]
        if key not in known or arm not in ARMS or repeat not in range(repeats):
            raise ValueError("Unknown question, arm or repeat")
        row = known[key]
        if result["gold_session_ids"] != row["answer_session_ids"] or result["question_type"] != row["question_type"]:
            raise ValueError("Gold labels differ from pinned dataset")
        group = grouped[arm].setdefault(key, {})
        if repeat in group:
            raise ValueError("Duplicate prediction")
        group[repeat] = result
        signatures.update(json.dumps(semantic_result(result), sort_keys=True).encode())
    first, timings = {}, {}
    for arm, questions in grouped.items():
        if set(questions) != set(known):
            raise ValueError("Incomplete question coverage")
        first[arm], timings[arm] = [], []
        for row in rows:
            group = questions[row["question_id"]]
            if set(group) != set(range(repeats)):
                raise ValueError("Missing repeat")
            if any(semantic_result(group[r]) != semantic_result(group[0]) for r in range(repeats)):
                raise ValueError("Semantic repeat mismatch")
            first[arm].append(group[0])
            timings[arm].extend(group[r]["recall_ms"] for r in range(repeats))
    return first, timings, signatures.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--changed-source", action="append", required=True)
    args = parser.parse_args()
    if digest(args.dataset) != DATA_SHA256:
        raise ValueError("Dataset hash differs")
    rows = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_rows(rows)
    if len(rows) != 500:
        raise ValueError("Requires complete 500-question run")
    first, timings, signature = load_predictions(rows, args.run / "predictions.jsonl")
    report = {"benchmark": "LongMemEval_S cleaned", "revision": REVISION,
        "source": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
        "dataset_sha256": DATA_SHA256, "status": "provisional_freeze_failed",
        "freeze_passed": False, "changed_sources": args.changed_source,
        "initial_source_manifest_available": (args.run / "manifest.json").is_file(),
        "explanation": "All calls completed and repeats matched, but files changed during the run. Do not cite as a strictly frozen benchmark.",
        "command": "python scripts/evaluate_longmemeval_ablation.py --dataset data/longmemeval/longmemeval_s_cleaned.json --output outputs/longmemeval-ablation-20260930 --repeats 2",
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "sqlite": sqlite3.sqlite_version},
        "retrieval_only": True, "model_calls": 0, "repeats": 2, "questions": len(rows), "calls": 6000,
        "question_types": dict(Counter(r["question_type"] for r in rows)),
        "fixed": {"limit": 5, "slack": 2, "chunk_chars": 8000, "budget_chars": 64000,
                  "retirement": "oldest floor(session_count / 2), ordered by raw timestamps"},
        "semantic_repeat_check": "exact_match", "semantic_sha256": signature,
        "predictions_sha256": digest(args.run / "predictions.jsonl"),
        "variants": {a: summary(rows, first[a], timings[a]) for a in ARMS},
        "paired": {a + "_vs_full": paired(rows, first["full"], first[a]) for a in ARMS if a != "full"}}
    report["paired"]["retired_no_lfhv_vs_retired_lfhv"] = paired(rows, first["retired_lfhv"], first["retired_no_lfhv"])
    report["by_type"] = {kind: {a: metrics([r for r in rows if r["question_type"] == kind],
        [p for r, p in zip(rows, first[a]) if r["question_type"] == kind]) for a in ARMS}
        for kind in sorted({r["question_type"] for r in rows})}
    subset = [r for r in rows if not r["question_id"].endswith("_abs") and any(
        r["haystack_session_ids"][i] in r["answer_session_ids"] for i in retirement_indices(r))]
    selected = {r["question_id"] for r in subset}
    report["retired_evidence_subset"] = {a: metrics(subset,
        [p for r, p in zip(rows, first[a]) if r["question_id"] in selected]) for a in ARMS}
    (args.run / "provisional-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_report(report, args.run / "PROVISIONAL.md")
    path = args.run / "PROVISIONAL.md"
    text = path.read_text(encoding="utf-8").replace("- Verification Status: VERIFIED",
        "- Verification Status: PROVISIONAL (repeat outputs match; source freeze failed)")
    path.write_text("# WARNING: SOURCE FREEZE FAILED; PROVISIONAL ONLY\n\n" + text, encoding="utf-8")
    print(json.dumps({a: v["emitted_at_7"] for a, v in report["variants"].items()}, indent=2))


if __name__ == "__main__":
    main()

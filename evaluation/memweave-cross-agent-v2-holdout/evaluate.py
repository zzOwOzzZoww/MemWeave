"""Freeze or run a topic-held-out, retrieval-only local Adapter evaluation."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "scripts"), str(REPO / "src")]
from build_dataset import VERSION, canonical, validate


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints():
    paths = list((REPO / "src").rglob("*.py")) + [
        HERE / name for name in ("build_dataset.py", "evaluate.py", "cases.jsonl", "dataset-card.json", "PROTOCOL.md")]
    paths += [REPO / "scripts" / "evaluate_retrieval_ablation.py"]
    paths += [REPO / "evaluation" / "memweave-cross-agent-v1" / name
              for name in ("evaluation_profile.py", "compare_naive_fts.py")]
    return {path.relative_to(REPO).as_posix(): digest(path) for path in sorted(paths)}


def environment():
    return {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform()}


def read_rows():
    raw = (HERE / "cases.jsonl").read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    validate(rows)
    card = json.loads((HERE / "dataset-card.json").read_text(encoding="utf-8"))
    if raw != canonical(rows) or card["dataset_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Dataset differs from its frozen canonical digest")
    return rows


def freeze(output):
    rows = read_rows()
    manifest = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "hashes": fingerprints(), "environment": environment(),
                "fixed": {"limit": 3, "slack": 2, "budget_chars": 4000, "variant": "full", "repeats": 2,
                          "timeout_seconds": 1800, "fixture_clock": "2026-09-30T00:00:00+00:00"},
                "model_calls": 0, "test_previously_run": False,
                "kernel_splits": {r["kernel_id"]: r["split"] for r in rows}}
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "prepared_not_run", "manifest": str(output / "manifest.json"),
                      "cases": len(rows), "model_calls": 0}, indent=2))


def check_freeze(manifest):
    if manifest["hashes"] != fingerprints() or manifest["environment"] != environment():
        raise ValueError("Source, dataset or environment changed after freezing; do not silently refreeze test")


def score_case(case, prediction):
    if prediction["case_id"] != case["case_id"]:
        raise ValueError("Prediction/case ID mismatch")
    known = {r["id"] for r in case["memory_records"]}
    for field in ("emitted_ids", "candidate_ids", "returned_ids", "restored_ids"):
        values = prediction[field]
        if not isinstance(values, list) or len(values) != len(set(values)) or not set(values) <= known:
            raise ValueError("Unknown or duplicate evidence ID in " + field)
    if not set(prediction["restored_ids"]) <= set(prediction["emitted_ids"]):
        raise ValueError("Restoration credit without same-turn emission")
    emitted = set(prediction["emitted_ids"])
    expected = case["expected"]
    required = set(expected["evidence_ids"])
    forbidden = emitted & set(expected["forbidden_ids"])
    positive = expected["decision"] == "inject"
    return {"case_id": case["case_id"], "kernel_id": case["kernel_id"], "category": case["category"],
            "positive": positive, "decision_correct": bool(emitted) == positive,
            "evidence_recall": len(emitted & required) / len(required) if positive else None,
            "complete_clean": required <= emitted and not emitted - required,
            "forbidden_injected": bool(forbidden),
            "insufficient_related_candidate": bool(set(prediction["candidate_ids"]) & set(expected["topical_ids"]))
                                              if not positive else None,
            "error_kind": case["forbidden_error_kind"] if forbidden else None,
            "emitted_ids": prediction["emitted_ids"],
            "missing_required_ids": sorted(required - emitted),
            "forbidden_ids_emitted": sorted(forbidden)}


def metrics(details):
    positives = [d for d in details if d["positive"]]
    negatives = [d for d in details if not d["positive"]]
    related = [d for d in details if d["category"] == "insufficient_detail"]
    rate = lambda values: sum(values) / len(values) if values else None
    return {"cases": len(details), "kernels": len({d["kernel_id"] for d in details}),
            "positive_cases": len(positives), "negative_cases": len(negatives),
            "decision_accuracy": rate([d["decision_correct"] for d in details]),
            "positive_evidence_recall": rate([d["evidence_recall"] for d in positives]),
            "complete_clean_rate": rate([d["complete_clean"] for d in details]),
            "abstention_clean_rate": rate([d["decision_correct"] for d in negatives]),
            "forbidden_injection_rate_all_cases": rate([d["forbidden_injected"] for d in details]),
            "insufficient_related_candidate_rate": rate([d["insufficient_related_candidate"] for d in related]),
            "forbidden_error_counts": dict(Counter(d["error_kind"] for d in details if d["error_kind"]))}


def summarize(cases, predictions):
    by_id = {}
    for pred in predictions:
        if pred["case_id"] in by_id:
            raise ValueError("Duplicate prediction")
        by_id[pred["case_id"]] = pred
    if set(by_id) != {c["case_id"] for c in cases}:
        raise ValueError("Missing or out-of-split predictions; absence is not successful abstention")
    details = [score_case(c, by_id[c["case_id"]]) for c in cases]
    groups = defaultdict(list)
    kernels = defaultdict(list)
    for detail in details:
        groups[detail["category"]].append(detail)
        kernels[detail["kernel_id"]].append(detail)
    return {"overall": metrics(details),
            "by_category": {key: metrics(group) for key, group in sorted(groups.items())},
            "by_kernel": {key: metrics(group) for key, group in sorted(kernels.items())},
            "failures": [d for d in details if not d["complete_clean"]]}


def run_case(case, database, project, aliases, fixed):
    from agent_knowledge_bridge.store import KnowledgeStore
    from evaluate_retrieval_ablation import run_arm
    search = KnowledgeStore.search
    generate = KnowledgeStore._generate_candidates
    reverse = {value: key for key, value in aliases.items()}
    candidates, returned, diagnostics = [], [], []

    def measured_generation(store, *args, **kwargs):
        result = generate(store, *args, **kwargs)
        for record in list(result[0]) + list(result[1]):
            candidates.append(reverse[record["id"]])
        return result

    def measured_search(store, *args, **kwargs):
        result = search(store, *args, **kwargs)
        returned.extend(reverse[r["id"]] for r in result["results"])
        raw = result["retrieval_diagnostics"]
        diagnostics.append({"include_retired": bool(kwargs.get("include_retired")),
                            "stages": [{k: v for k, v in stage.items() if k != "elapsed_ms"} for stage in raw["stages"]],
                            "omitted": [{"evidence_id": reverse[o["knowledge_id"]], "reason": o["reason"]}
                                        for o in raw["omitted"]]})
        return result

    # These observation-only patches cannot change the return values or filters.
    with ExitStack() as stack:
        stack.enter_context(patch.object(KnowledgeStore, "_generate_candidates", measured_generation))
        stack.enter_context(patch.object(KnowledgeStore, "search", measured_search))
        result = run_arm(case, database, project, aliases, fixed["variant"],
                         limit=fixed["limit"], budget=fixed["budget_chars"])
    return {**result, "candidate_ids": list(dict.fromkeys(candidates)),
            "returned_ids": list(dict.fromkeys(returned)), "diagnostics": diagnostics}


def semantic(prediction):
    def without_timing(value):
        if isinstance(value, dict):
            return {k: without_timing(v) for k, v in value.items()
                    if k not in ("recall_ms", "elapsed_ms", "repeat")}
        if isinstance(value, list):
            return [without_timing(v) for v in value]
        return value

    return without_timing(prediction)


def run(manifest_path, output, split):
    from evaluate_retrieval_ablation import clone_database, seed_case
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check_freeze(manifest)
    cases = [r for r in read_rows() if r["split"] == split]
    fixed = manifest["fixed"]
    output.mkdir(parents=True, exist_ok=False)
    first = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="memweave-holdout-v2-") as temp, \
            (output / "predictions.jsonl").open("w", encoding="utf-8") as handle:
        for index, case in enumerate(cases):
            folder = Path(temp) / case["case_id"]
            folder.mkdir()
            project, aliases = seed_case(case, folder / "seed.db")
            for repeat in range(fixed["repeats"]):
                if time.monotonic() - started > fixed["timeout_seconds"]:
                    raise TimeoutError("Holdout hard-time budget exceeded; partial output retained")
                database = folder / f"run-{repeat}.db"
                clone_database(folder / "seed.db", database)
                result = run_case(case, database, project, aliases, fixed)
                if repeat == 0:
                    first.append(result)
                elif semantic(first[-1]) != semantic(result):
                    raise ValueError("Semantic repeat mismatch: " + case["case_id"])
                handle.write(json.dumps({**result, "repeat": repeat}, ensure_ascii=False) + "\n")
                handle.flush()
            if (index + 1) % 10 == 0 or index + 1 == len(cases):
                print(f"{split}: {index + 1}/{len(cases)}", flush=True)
    check_freeze(manifest)
    report = {"version": VERSION, "split": split, "source_commit": manifest["source_commit"],
              "dataset_sha256": digest(HERE / "cases.jsonl"), "retrieval_only": True, "model_calls": 0,
              "production_database_accessed": False, "calls": len(cases) * fixed["repeats"],
              "freeze_passed": True, "semantic_repeat_check": "exact_match",
              "semantic_sha256": hashlib.sha256(json.dumps([semantic(p) for p in first], sort_keys=True).encode()).hexdigest(),
              "duration_seconds": round(time.monotonic() - started, 3), **summarize(cases, first),
              "limitations": ["Synthetic assistant-authored labels, not independently adjudicated.",
                              "Topic-held-out development check, not a third-party benchmark or QA accuracy.",
                              "Related candidate is raw pre-filter FTS coverage; returned evidence passed Core filters.",
                              "Validity dates are scoring labels; Core receives lifecycle/supersession and fact content only.",
                              "Cases within a kernel share templates; no statistical significance or production prevalence claimed.",
                              "No model calls, automatic admission, native Hook transport, or final-answer grading."]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "completed", "split": split, "calls": report["calls"],
                      "freeze_passed": True, "overall": report["overall"], "failure_count": len(report["failures"])}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--manifest", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--split", choices=("dev", "test"), required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.output)
    else:
        run(args.manifest, args.output, args.split)


if __name__ == "__main__":
    main()

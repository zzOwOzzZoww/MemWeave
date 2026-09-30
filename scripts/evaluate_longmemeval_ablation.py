"""Frozen LongMemEval retrieval/adapter ablation, without model calls."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import multiprocessing
from pathlib import Path
import platform
import random
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

from agent_knowledge_bridge.store import KnowledgeStore
from evaluate_retrieval_ablation import clone_database, run_arm, semantic_result

REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
DATA_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
ARMS = {"full": ("full", False), "no_bridge": ("no_bridge", False),
        "no_sibling": ("no_sibling", False), "no_anchor": ("no_anchor", False),
        "retired_lfhv": ("full", True), "retired_no_lfhv": ("no_lfhv", True)}


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def date_key(value):
    return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M")


def memory_records(row, chunk_chars=8000):
    """Use only raw history, dates and position; never gold or source ID hints."""
    sessions, dates = row["haystack_sessions"], row["haystack_dates"]
    if len(sessions) != len(dates):
        raise ValueError("Unaligned history sessions/dates")
    records = []
    for index, (session, date) in enumerate(zip(sessions, dates)):
        text = "\n".join(str(turn["role"]) + ": " + str(turn["content"]) for turn in session)
        # Fixed character boundaries preserve the entire transcript, including
        # oversized turns. Evidence is scored at source-session granularity.
        for offset in range(0, len(text), chunk_chars):
            content = text[offset:offset + chunk_chars].strip()
            if content:
                records.append({"id": f"s{index:04d}:c{offset // chunk_chars:04d}",
                    "session_index": index, "date": date,
                    "title": f"History session {index + 1}, {date}, part {offset // chunk_chars + 1}",
                    "content": content})
    if not records:
        raise ValueError("Empty history")
    return records


def retirement_indices(row):
    # Capacity replay, not the production Governor's LFHV retirement heuristic.
    # This function deliberately cannot inspect question/answer/evidence fields.
    dates = row["haystack_dates"]
    ordered = sorted(range(len(dates)), key=lambda i: (date_key(dates[i]), i))
    return set(ordered[:len(ordered) // 2])


def validate_rows(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected a nonempty JSON array")
    seen = set()
    for row in rows:
        key = row["question_id"]
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("Missing or duplicate question ID")
        seen.add(key)
        if not isinstance(row["question"], str) or not 0 < len(row["question"]) <= 500:
            raise ValueError("Query exceeds the Core contract")
        ids = row["haystack_session_ids"]
        if len(ids) != len(row["haystack_sessions"]) or len(ids) != len(row["haystack_dates"]):
            raise ValueError("Unaligned session IDs")
        if not set(row["answer_session_ids"]) <= set(ids):
            raise ValueError("Gold session absent from history")
        if not key.endswith("_abs") and not row["answer_session_ids"]:
            raise ValueError("Positive question has no evidence")
        for date in row["haystack_dates"]:
            date_key(date)


def seed_history(records, database, project):
    store = KnowledgeStore(database)
    store.clock = lambda: "2026-09-30T00:00:00+00:00"
    aliases = {}
    for record in records:
        result = store.publish(source_agent="claude-code", project_key=project,
            title=record["title"], content=record["content"], knowledge_type="fact", scope="project",
            source_session=record["id"], search_terms="", subject_terms=[],
            evidence_summary="Public LongMemEval transcript; retrieval-only fixture")
        actual = result["knowledge"]["id"]
        if actual in aliases.values():
            raise ValueError("Unexpected deduplication of distinct history chunks")
        aliases[record["id"]] = actual
        store.feedback(agent_id="longmemeval-import", knowledge_id=actual, outcome="verified",
            evidence_kind="test", evidence_ref="longmemeval:" + record["id"],
            evidence_summary="Assumed-active raw history fixture, not automatic admission evidence")
    return aliases


def archive_history(records, aliases, retired, database):
    store = KnowledgeStore(database)
    store.clock = lambda: "2026-09-30T00:00:00+00:00"
    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            if record["session_index"] in retired:
                outcome = store.transit(aliases[record["id"]], to_status="archived",
                    actor="longmemeval-replay", reason="Fixed oldest-half capacity replay; no gold input",
                    expected_status="active", _connection=connection)
                if not outcome.get("changed"):
                    raise AssertionError("Retirement failed")


def session_outputs(row, prediction, *, emitted=True, k=None):
    items = prediction["items"]
    aliases = ([i["evidence_id"] for i in items if i["emitted"]] if emitted else
               [i["evidence_id"] for i in items])
    aliases = aliases if k is None else aliases[:k]
    indices = [int(alias.split(":", 1)[0][1:]) for alias in aliases]
    # Preserve ranking while mapping chunk slots to source-session evidence.
    return list(dict.fromkeys(row["haystack_session_ids"][i] for i in indices))


def score(row, prediction, *, emitted=True, k=None):
    recalled = set(session_outputs(row, prediction, emitted=emitted, k=k))
    expected = set(row["answer_session_ids"])
    if row["question_id"].endswith("_abs"):
        return {"positive": False, "empty": not recalled, "recall": None,
                "hit": None, "complete": None}
    overlap = expected & recalled
    return {"positive": True, "empty": not recalled, "recall": len(overlap) / len(expected),
            "hit": bool(overlap), "complete": expected <= recalled}


def percentile(values, fraction):
    import math
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def metrics(rows, predictions, *, emitted=True, k=None):
    scored = [score(r, p, emitted=emitted, k=k) for r, p in zip(rows, predictions)]
    positives = [s for s in scored if s["positive"]]
    negatives = [s for s in scored if not s["positive"]]
    mean = lambda key: sum(s[key] for s in positives) / len(positives) if positives else None
    return {"positive_questions": len(positives), "abstention_questions": len(negatives),
            "hit": mean("hit"), "mean_evidence_recall": mean("recall"), "complete": mean("complete"),
            "abstention_empty_rate": sum(s["empty"] for s in negatives) / len(negatives) if negatives else None}


def summary(rows, predictions, timings):
    return {"search_at_5": metrics(rows, predictions, emitted=False, k=5),
            "search_at_7": metrics(rows, predictions, emitted=False, k=7),
            "emitted_at_7": metrics(rows, predictions),
            "adapter_p50_ms": percentile(timings, .5), "adapter_p95_ms": percentile(timings, .95),
            "timing_samples": len(timings),
            "context_utf8_bytes": sum(p["context_utf8_bytes"] for p in predictions),
            "restored_records": sum(len(p["restored_ids"]) for p in predictions),
            "retired_search_cases": sum(p["retired_searches"] > 0 for p in predictions),
            "stage_coverage": {stage: {
                "executed_cases": sum(any(s["stage"] == stage and not s["skipped_reason"] for s in p["stages"]) for p in predictions),
                "adding_cases": sum(any(s["stage"] == stage and s["added_count"] > 0 for s in p["stages"]) for p in predictions),
                "emitted_records": sum(i["emitted"] and i["origin"] == origin for p in predictions for i in p["items"])}
                for stage, origin in (("bridge", "bridged"), ("sibling", "sibling"), ("anchor", "anchored"))}}


def paired(rows, full, other):
    harmed, helped, changed = [], [], []
    for row, before, after in zip(rows, full, other):
        a, b = score(row, before), score(row, after)
        key = row["question_id"]
        if before["emitted_ids"] != after["emitted_ids"]:
            changed.append(key)
        if a["positive"]:
            if a["recall"] > b["recall"]:
                harmed.append(key)
            elif a["recall"] < b["recall"]:
                helped.append(key)
    return {"changed_questions": changed, "recall_harmed_questions": harmed, "recall_helped_questions": helped}


def evaluate_question(task):
    index, row, repeats = task
    predictions, first = [], {}
    with tempfile.TemporaryDirectory(prefix="memweave-longmemeval-") as folder:
        temp = Path(folder)
        project = "lme-" + hashlib.sha256(row["question_id"].encode()).hexdigest()[:20]
        case = {"case_id": row["question_id"], "target_agent": "codex", "query": row["question"]}
        records, retired = memory_records(row), retirement_indices(row)
        seed, archive = temp / "seed.db", temp / "archive.db"
        aliases = seed_history(records, seed, project)
        clone_database(seed, archive)
        archive_history(records, aliases, retired, archive)
        for repeat in range(repeats):
            order = list(ARMS)
            random.Random(20260930 + index * 101 + repeat).shuffle(order)
            for arm in order:
                variant, is_retired = ARMS[arm]
                database = temp / f"{arm}-{repeat}.db"
                clone_database(archive if is_retired else seed, database)
                prediction = run_arm(case, database, project, aliases, variant, limit=5, budget=64000)
                prediction["variant"] = arm
                if repeat == 0:
                    first[arm] = prediction
                elif semantic_result(first[arm]) != semantic_result(prediction):
                    raise AssertionError("Semantic repeat mismatch: " + row["question_id"] + ":" + arm)
                predictions.append({"repeat": repeat, "question_type": row["question_type"],
                    "gold_session_ids": row["answer_session_ids"],
                    "emitted_session_ids": session_outputs(row, prediction), **prediction})
    return {"index": index, "imported_chunks": len(records),
        "retired_evidence_positive": not row["question_id"].endswith("_abs") and any(
            row["haystack_session_ids"][i] in row["answer_session_ids"] for i in retired),
        "predictions": predictions}


def evaluate_questions(rows, *, repeats, workers=1, timeout=3600):
    if not 1 <= workers <= 32:
        raise ValueError("workers must be between 1 and 32")
    started = time.monotonic()
    tasks = ((index, row, repeats) for index, row in enumerate(rows))
    if timeout <= 0:
        raise TimeoutError("Experiment exceeded fixed timeout")
    if workers == 1:
        for task in tasks:
            if time.monotonic() - started >= timeout:
                raise TimeoutError("Experiment exceeded fixed timeout")
            yield evaluate_question(task)
        return
    # Process isolation is required: run_arm patches environment and global cache.
    with multiprocessing.get_context("spawn").Pool(processes=workers) as pool:
        results = pool.imap(evaluate_question, tasks, chunksize=1)
        for _ in rows:
            remaining = timeout - (time.monotonic() - started)
            try:
                if remaining <= 0:
                    raise multiprocessing.TimeoutError
                yield results.next(timeout=remaining)
            except multiprocessing.TimeoutError as exc:
                print("Hard timeout reached; stopping experiment workers", flush=True)
                raise TimeoutError("Experiment exceeded fixed timeout") from exc


def write_report(report, path):
    fmt = lambda x: "N/A" if x is None else f"{100*x:.2f}%"
    lines = ["# LongMemEval Ablation", "", "## Material Passport", "",
        "- Origin Skill: academic-research-suite (reproducibility guidance)", "- Origin Mode: run",
        "- Origin Date: 2026-09-30", "- Version Label: longmemeval-ablation-v1",
        "- Verification Status: " + ("VERIFIED" if report["repeats"] > 1 else "UNVERIFIED"), "",
        "Semantic outputs checked across repeats; verification does not imply downstream answer quality.", "",
        "## Results", "", "| Arm | Search Hit@5 | Search Recall@5 | Search Hit@7 | Search Recall@7 | Emitted Recall (max 7) | Complete (emitted) | Abstention Empty (emitted) | Adapter P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for arm, values in report["variants"].items():
        a, b, e = values["search_at_5"], values["search_at_7"], values["emitted_at_7"]
        lines.append(f"| {arm} | {fmt(a['hit'])} | {fmt(a['mean_evidence_recall'])} | {fmt(b['hit'])} | {fmt(b['mean_evidence_recall'])} | {fmt(e['mean_evidence_recall'])} | {fmt(e['complete'])} | {fmt(e['abstention_empty_rate'])} | {values['adapter_p95_ms']:.3f} |")
    lines += ["", "## Fixed Protocol", "", f"Command: `{report['command']}`", "",
        "Same raw histories, gold sessions, chunking, limits and budget for every arm. No model calls.",
        "Query-statistics cache is fresh per arm; DB seed is cloned per arm/repeat.",
        "Search metrics use the first 5/7 chunk slots mapped to source sessions, NOT 5/7 unique sessions.",
        "Retired arms include recovery candidates in their pre-emission candidate metrics.",
        "A session hit does not prove the retrieved chunk contains the answer. No QA score is reported.",
        "Adapter timings include trace/restoration writes, not DB import/clone, Hook transport or TTFT.",
        f"Question workers: {report.get('execution', {}).get('workers', 1)}. Parallel timings include resource contention and are not single-request latency.",
        "Abstention empty rate is a custom context diagnostic, not official QA abstention accuracy.",
        "Retirement is a custom oldest-half capacity replay, not official LongMemEval protocol or LFHV's automatic retirement policy.",
        "The source IDs and has_answer labels never enter content, titles or search terms.",
        "All records are assumed-active retrieval fixtures; automatic extraction, admission and cross-Agent workflow are not tested.",
        "This post-improvement rerun follows earlier dataset-informed diagnosis; it is not a held-out evaluation. No Core tuning occurs during this frozen run.", "",
        "## Paired Emission Changes", ""]
    for label, change in report["paired"].items():
        lines.append(f"- {label}: {len(change['changed_questions'])} changed; {len(change['recall_harmed_questions'])} recall losses; {len(change['recall_helped_questions'])} recall gains (relative to reference).")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workers", type=int, default=1, help="Isolated question processes (1-32); timings under concurrency are load-dependent")
    parser.add_argument("--max-cases", type=int, help="Preflight only; full run uses all 500")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3 or not 1 <= args.workers <= 32 or (args.max_cases is not None and args.max_cases < 1):
        parser.error("Invalid repeats, workers or max-cases")
    if digest(args.dataset) != DATA_SHA256:
        raise ValueError("Dataset differs from pinned public revision")
    rows = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_rows(rows)
    if len(rows) != 500:
        raise ValueError("Pinned dataset must contain 500 questions")
    if args.max_cases:
        rows = rows[:args.max_cases]
    source_paths = sorted((ROOT / "src" / "agent_knowledge_bridge").glob("*.py")) + [
        Path(__file__), ROOT / "scripts/evaluate_retrieval_ablation.py",
        ROOT / "evaluation/memweave-cross-agent-v1/evaluation_profile.py",
        ROOT / "evaluation/memweave-cross-agent-v1/compare_naive_fts.py",
        ROOT / "evaluation/longmemeval-ablation-v1/README.md"]
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in source_paths}
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {"benchmark": "LongMemEval_S cleaned", "revision": REVISION,
        "source": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
        "download_transport": "hf-mirror.com; validated against repository LFS SHA-256",
        "dataset_sha256": DATA_SHA256, "source_sha256": hashes,
        "command": "python scripts/evaluate_longmemeval_ablation.py " + " ".join(sys.argv[1:]),
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "sqlite": sqlite3.sqlite_version},
        "retrieval_only": True, "model_calls": 0, "repeats": args.repeats, "questions": len(rows),
        "execution": {"workers": args.workers, "isolation": "question-local database and process",
                      "result_order": "dataset order", "timing_mode": "serial" if args.workers == 1 else "concurrent_load"},
        "question_types": dict(Counter(r["question_type"] for r in rows)),
        "duplicate_session_id_questions": sum(len(r["haystack_session_ids"]) != len(set(r["haystack_session_ids"])) for r in rows),
        "fixed": {"limit": 5, "slack": 2, "chunk_chars": 8000, "budget_chars": 64000,
                  "retirement": "oldest floor(session_count / 2), ordered by raw timestamps",
                  "seed": 20260930, "cache": "fresh per arm", "timeout_seconds": 3600},
        "imported_chunks": 0, "retired_evidence_positive_questions": 0}
    (args.output / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    first, timings = {a: [] for a in ARMS}, {a: [] for a in ARMS}
    semantic_hash = hashlib.sha256()
    with (args.output / "predictions.jsonl").open("w", encoding="utf-8") as output:
        for result in evaluate_questions(rows, repeats=args.repeats, workers=args.workers,
                                         timeout=3600 - (time.monotonic() - started)):
            index = result["index"]
            report["imported_chunks"] += result["imported_chunks"]
            report["retired_evidence_positive_questions"] += int(result["retired_evidence_positive"])
            for prediction in result["predictions"]:
                arm = prediction["variant"]
                if prediction["repeat"] == 0:
                    first[arm].append(prediction)
                timings[arm].append(prediction["recall_ms"])
                semantic_hash.update(json.dumps(semantic_result(prediction), sort_keys=True).encode())
                output.write(json.dumps(prediction) + "\n")
            output.flush()
            if (index + 1) % 5 == 0 or index == 0 or index + 1 == len(rows):
                print(f"{index+1}/{len(rows)} questions, {report['imported_chunks']} imported chunks, {time.monotonic()-started:.1f}s", flush=True)
    current_hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in source_paths}
    if hashes != current_hashes or digest(args.dataset) != DATA_SHA256:
        report.update(status="freeze_failed", duration_seconds=round(time.monotonic() - started, 2),
            changed_sources=[name for name in hashes if hashes[name] != current_hashes[name]],
            source_after_sha256=current_hashes)
        (args.output / "freeze-failure.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError("Code, protocol or data changed during experiment")
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    report["status"] = "completed"
    report["freeze_passed"] = True
    report["calls"] = len(rows) * len(ARMS) * args.repeats
    report["semantic_sha256"] = semantic_hash.hexdigest()
    report["semantic_repeat_check"] = "exact_match" if args.repeats > 1 else "not_checked"
    report["variants"] = {a: summary(rows, first[a], timings[a]) for a in ARMS}
    report["by_type"] = {kind: {a: metrics(
        [r for r in rows if r["question_type"] == kind],
        [p for r, p in zip(rows, first[a]) if r["question_type"] == kind]) for a in ARMS}
        for kind in sorted({r["question_type"] for r in rows})}
    report["paired"] = {a + "_vs_full": paired(rows, first["full"], first[a]) for a in ARMS if a != "full"}
    report["paired"]["retired_no_lfhv_vs_retired_lfhv"] = paired(rows, first["retired_lfhv"], first["retired_no_lfhv"])
    report["retired_evidence_subset"] = {a: metrics(
        [r for r in rows if not r["question_id"].endswith("_abs") and any(r["haystack_session_ids"][i] in r["answer_session_ids"] for i in retirement_indices(r))],
        [p for r, p in zip(rows, first[a]) if not r["question_id"].endswith("_abs") and any(r["haystack_session_ids"][i] in r["answer_session_ids"] for i in retirement_indices(r))]) for a in ARMS}
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    write_report(report, args.output / "REPORT.md")
    print(json.dumps(report["variants"], indent=2))


if __name__ == "__main__":
    main()

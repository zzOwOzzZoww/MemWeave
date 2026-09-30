"""Paired leave-one-component-out retrieval ablation; isolated DBs, no models."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
BENCHMARK = REPO / "evaluation" / "memweave-cross-agent-v1"
DIAGNOSTICS = REPO / "evaluation" / "retrieval-ablation-v1" / "cases.jsonl"
sys.path[:0] = [str(REPO / "src"), str(BENCHMARK)]

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.retrieval_pipeline import RetrievalPolicy
from agent_knowledge_bridge import retrieval_stats
from compare_naive_fts import percentile
from evaluation_profile import expected_for

STAGES = ("bridge", "sibling", "anchor")
VARIANTS = {
    "full": (STAGES, True),
    "no_bridge": (("sibling", "anchor"), True),
    "no_sibling": (("bridge", "anchor"), True),
    "no_anchor": (("bridge", "sibling"), True),
    "no_lfhv": (STAGES, False),
}


def read_cases(path, *, split=None):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if split and split != "all":
        rows = [row for row in rows if row["split"] == split]
    ids = [row["case_id"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise ValueError("Cases must be nonempty and have unique case IDs")
    for row in rows:
        known = [r["id"] for r in row["memory_records"]]
        if len(known) != len(set(known)):
            raise ValueError("Duplicate memory aliases")
        expected = gold_for(row)
        required, forbidden = set(expected["evidence_ids"]), set(expected["forbidden_ids"])
        allowed = set(expected.get("allowed_ids", expected["evidence_ids"]))
        if (not required <= allowed or allowed & forbidden or
                not (allowed | forbidden) <= set(known) or
                bool(required) != (expected["decision"] == "inject")):
            raise ValueError("Invalid fixed scoring contract: " + row["case_id"])
    return rows


def gold_for(case):
    # Freeze one on-demand contract for ALL arms, including no_lfhv.
    return expected_for(case, "on-demand-v2")


def seed_case(case, database):
    project = "ablation-" + case["case_id"].lower().replace("_", "-")
    adapter = ClaudeLearningAdapter(database_path=database, agent_id=case["target_agent"],
                                    project_key=project, reviewer=lambda _: {"proposals": []})
    store = adapter.store.knowledge
    store.clock = lambda: "2026-09-30T00:00:00+00:00"
    aliases = {}
    for record in case["memory_records"]:
        record_project = project if record["project_key"] == case["project_context"] else project + "-other"
        created = store.publish(source_agent=record["source_agent"], project_key=record_project,
            title=record.get("title") or record["content"].splitlines()[0][:160],
            content=record["content"], scope=record["scope"], knowledge_type="procedure",
            search_terms=record.get("search_terms"), subject_terms=record.get("subject_terms"),
            source_session=record["source_session"], evidence_summary="Synthetic ablation fixture")
        key = created["knowledge"]["id"]
        if key in aliases.values():
            raise ValueError("Fixture publication deduplicated distinct aliases")
        aliases[record["id"]] = key
        if record["status"] in {"active", "stale", "archived"}:
            store.feedback(agent_id="ablation-fixture", knowledge_id=key, outcome="verified",
                evidence_kind="test", evidence_ref="fixture:ablation", project_key=record_project,
                evidence_summary="Synthetic verified fixture, not real task evidence")
        if record["status"] in {"stale", "archived", "quarantined"}:
            store.transit(key, to_status=record["status"], actor="ablation-fixture",
                          reason="Synthetic lifecycle fixture")
    with store._connect() as db:
        for record in case["memory_records"]:
            if record.get("superseded_by"):
                replacement = aliases.get(record["superseded_by"], "tombstone:" + record["superseded_by"])
                db.execute("UPDATE knowledge_records SET status='archived',superseded_by=? WHERE id=?",
                           (replacement, aliases[record["id"]]))
    return project, aliases


def clone_database(source, target):
    with closing(sqlite3.connect(source)) as reader, closing(sqlite3.connect(target)) as writer:
        reader.backup(writer)


def run_arm(case, database, project, aliases, variant, *, limit=3, budget=4000):
    stages, lfhv = VARIANTS[variant]
    adapter_type = CodexLearningAdapter if case["target_agent"] == "codex" else ClaudeLearningAdapter
    adapter = adapter_type(database_path=database, agent_id=case["target_agent"],
                           project_key=project, reviewer=lambda _: {"proposals": []})
    search, emit = adapter.store.knowledge.search, adapter.reuse.start
    calls = []

    def measured_search(**kwargs):
        result = search(**kwargs, retrieval_policy=RetrievalPolicy(stages=stages))
        calls.append({"retired": bool(kwargs.get("include_retired")),
                      "diagnostics": result["retrieval_diagnostics"]})
        return result

    def bounded_emit(**kwargs):
        return emit(**kwargs, budget=budget)

    # Patches are local to this adapter and restored even on exceptions.
    env = {"MW_RECALL_LIMIT": str(limit), "MW_LFHV_PROBE": "1" if lfhv else "0",
           "MW_LFHV_RECOVERY": "1" if lfhv else "0"}
    retrieval_stats.FREQUENCIES = retrieval_stats.FrequencyCache()
    with patch.dict(os.environ, env), patch.object(adapter.store.knowledge, "search", measured_search), \
            patch.object(adapter.reuse, "start", bounded_emit):
        started = time.perf_counter()
        response = adapter.recall({"session_id": "ablation", "turn_id": case["case_id"],
                                   "prompt": case["query"], "cwd": "synthetic://" + project})
        elapsed = (time.perf_counter() - started) * 1000
    context = response.get("hookSpecificOutput", {}).get("additionalContext", "")
    trace = adapter.reuse.list(project, limit=1)[0]
    by_actual = {actual: alias for alias, actual in aliases.items()}
    emitted = [by_actual[i["knowledge_id"]] for i in trace["items"] if i["emitted"]]
    main = next(call["diagnostics"] for call in calls if not call["retired"])
    reports = [r for r in main["stages"] if r["stage"] in STAGES]
    if any(r.get("added_count", 0) for r in reports if r["stage"] not in stages):
        raise AssertionError("Disabled expansion executed")
    restored = [by_actual[i["knowledge_id"]] for i in trace["items"]
                if i["emitted"] and i.get("lfhv_recovery_outcome") == "restored"]
    if not lfhv and (restored or any(call["retired"] for call in calls)):
        raise AssertionError("Disabled LFHV executed")
    return {"case_id": case["case_id"], "variant": variant,
        "emitted_ids": emitted, "restored_ids": restored,
        "context_chars": len(context),
        "context_utf8_bytes": len(context.encode("utf-8")),
        "recall_ms": round(elapsed, 4), "stages": reports,
        "retired_searches": sum(call["retired"] for call in calls),
        "items": [{"evidence_id": by_actual[i["knowledge_id"]], "origin": i["origin"],
                   "emitted": i["emitted"], "omitted_reason": i["omitted_reason"]}
                  for i in trace["items"]]}


def score_case(case, prediction):
    expected = gold_for(case)
    required, forbidden = set(expected["evidence_ids"]), set(expected["forbidden_ids"])
    allowed = set(expected.get("allowed_ids", expected["evidence_ids"]))
    known = {r["id"] for r in case["memory_records"]}
    emitted, restored = set(prediction["emitted_ids"]), set(prediction["restored_ids"])
    if (not (emitted | restored) <= known or not restored <= emitted or
            len(emitted) != len(prediction["emitted_ids"])):
        raise ValueError("Invalid prediction aliases or restoration without emission")
    positive = expected["decision"] == "inject"
    return {"positive": positive, "decision_correct": bool(emitted) == positive,
        "evidence_recall": len(required & emitted) / len(required) if required else None,
        "complete_clean": required <= emitted and not (emitted - allowed),
        "forbidden_injected": bool(emitted & forbidden),
        "unexpected_records": len(emitted - allowed),
        "recovery_complete": set(expected["restored_ids"]) <= restored
                             if expected.get("restored_ids") else None}


def summarize(cases, predictions):
    scored = [score_case(c, p) for c, p in zip(cases, predictions)]
    positives = [s for s in scored if s["positive"]]
    negatives = [s for s in scored if not s["positive"]]
    recoveries = [s for s in scored if s["recovery_complete"] is not None]
    mean = lambda values: round(sum(values) / len(values), 6) if values else None
    return {"cases": len(cases), "positive_cases": len(positives), "negative_cases": len(negatives),
        "decision_accuracy": mean([s["decision_correct"] for s in scored]),
        "positive_evidence_recall": mean([s["evidence_recall"] for s in positives]),
        "complete_clean_rate": mean([s["complete_clean"] for s in scored]),
        "negative_injection_rate": mean([not s["decision_correct"] for s in negatives]),
        "forbidden_injection_rate": mean([s["forbidden_injected"] for s in scored]),
        "unexpected_records": sum(s["unexpected_records"] for s in scored),
        "lfhv_same_turn_recovery_rate": mean([s["recovery_complete"] for s in recoveries]),
        "emitted_records": sum(len(p["emitted_ids"]) for p in predictions),
        "context_utf8_bytes": sum(p["context_utf8_bytes"] for p in predictions),
        "stage_coverage": {stage: {
            "executed_cases": sum(any(r["stage"] == stage and not r["skipped_reason"]
                                      for r in p["stages"]) for p in predictions),
            "added_candidate_cases": sum(any(r["stage"] == stage and r.get("added_count", 0) > 0
                                             for r in p["stages"]) for p in predictions),
            "emitted_origin_records": sum(i["emitted"] and i["origin"] == origin
                                            for p in predictions for i in p["items"]),
        } for stage, origin in zip(STAGES, ("bridged", "sibling", "anchored"))},
        "lfhv_searched_cases": sum(p["retired_searches"] > 0 for p in predictions)}


def semantic_result(prediction):
    return {key: prediction[key] for key in ("case_id", "variant", "emitted_ids", "restored_ids",
                                             "context_chars", "context_utf8_bytes", "items")}


def paired_changes(cases, full, removed):
    changes = []
    for case, before, after in zip(cases, full, removed):
        a, b = score_case(case, before), score_case(case, after)
        if before["emitted_ids"] != after["emitted_ids"]:
            changes.append({"case_id": case["case_id"],
                "lost_ids": sorted(set(before["emitted_ids"]) - set(after["emitted_ids"])),
                "gained_ids": sorted(set(after["emitted_ids"]) - set(before["emitted_ids"])),
                "recall_full": a["evidence_recall"], "recall_removed": b["evidence_recall"]})
    return {"changed_cases": len(changes),
        "recall_harmed_cases": sum((score_case(c, a)["evidence_recall"] or 0) >
                                   (score_case(c, b)["evidence_recall"] or 0)
                                   for c, a, b in zip(cases, full, removed)),
        "recall_helped_cases": sum((score_case(c, a)["evidence_recall"] or 0) <
                                   (score_case(c, b)["evidence_recall"] or 0)
                                   for c, a, b in zip(cases, full, removed)),
        "details": changes}


def write_markdown(report, path):
    lines = ["# Retrieval Ablation", "", "## Material Passport", "",
        "- Origin Skill: academic-research-suite (reproducibility guidance; repository development workflow)",
        "- Origin Mode: run", "- Origin Date: " + report["date"],
        "- Verification Status: " + ("VERIFIED" if report["repeats"] > 1 else "UNVERIFIED"),
        "- Version Label: retrieval-ablation-v1", "", "## Fixed Protocol", "",
        "No model calls. Same on-demand evidence labels, DB snapshot, limit, slack and character budget in all arms.",
        "Recall timing includes local adapter recall + trace/restoration writes, but excludes DB setup/clone and Hook transport.",
        "Fresh query-statistics cache per arm. Timings are machine-sensitive and are not TTFT or the LoCoMo search-only P95.",
        "This is regression/diagnostic evidence, not independent generalization or downstream task success.", "",
        f"Command: `{report['command']}`", ""]
    fmt = lambda value: "N/A" if value is None else f"{value * 100:.2f}%"
    for suite, group in report["suites"].items():
        lines += ["## " + suite, "", "| Arm | Positive Recall | Complete + Clean | Forbidden Injection | Recall P95 ms | Context UTF-8 Bytes |",
                  "|---|---:|---:|---:|---:|---:|"]
        for variant, metrics in group["variants"].items():
            lines.append(f"| {variant} | {fmt(metrics['positive_evidence_recall'])} | {fmt(metrics['complete_clean_rate'])} | "
                         f"{fmt(metrics['forbidden_injection_rate'])} | {metrics['recall_p95_ms']} | {metrics['context_utf8_bytes']} |")
        lines += ["", "### Full-System Coverage", "",
                  "| Stage | Executed Cases | Cases Adding Candidates | Emitted Origin Records |", "|---|---:|---:|---:|"]
        for stage, coverage in group["variants"]["full"]["stage_coverage"].items():
            lines.append(f"| {stage} | {coverage['executed_cases']} | {coverage['added_candidate_cases']} | {coverage['emitted_origin_records']} |")
        lines += ["", "### Paired Changes", ""]
        for variant, changes in group["paired_vs_full"].items():
            lines.append(f"- {variant}: {changes['changed_cases']} changed cases; "
                         f"{changes['recall_harmed_cases']} recall losses; {changes['recall_helped_cases']} recall gains.")
        lines.append("")
    lines += ["## Boundaries", "",
        "- Bundled test failures previously informed rule changes: not a held-out set.",
        "- Diagnostic cases are hand-authored mechanism probes, including regression-derived shapes: not a random workload.",
        "- One-component removal measures conditional contribution; stages interact and gains are not additive.",
        "- Anchor uses the same allowed extra slots in every arm; removing it can reduce actual context volume.",
        "- No policy tuning or data changes are justified merely by a lower ablation score.",
        "- Identical scores can mean redundancy or lack of coverage; inspect stage coverage and per-case predictions.",
        "- Repeat checks compare semantic outputs exactly, excluding timings and internal IDs.",
        "- No significance, population effect, optimal TTL or real task-benefit claim is made.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=("test", "dev", "calibration", "all"), default="test")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--budget", type=int, default=4000)
    parser.add_argument("--suite", choices=("both", "regression", "diagnostic"), default="both")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.limit <= 20 or args.budget <= 0:
        parser.error("Invalid repeats, limit or budget")
    suites = {}
    if args.suite in {"both", "regression"}:
        suites["bundled_regression"] = read_cases(BENCHMARK / "cases.jsonl", split=args.split)
    if args.suite in {"both", "diagnostic"}:
        suites["mechanism_diagnostic"] = read_cases(DIAGNOSTICS)
    source_paths = sorted((REPO / "src" / "agent_knowledge_bridge").glob("*.py")) + [
        Path(__file__), BENCHMARK / "evaluation_profile.py", BENCHMARK / "compare_naive_fts.py"]
    hashes = {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    data_hashes = {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (BENCHMARK / "cases.jsonl", DIAGNOSTICS)}
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"date": "2026-09-30", "protocol": "retrieval-ablation-v1", "repeats": args.repeats,
        "command": "python scripts/evaluate_retrieval_ablation.py " + " ".join(sys.argv[1:]),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "sqlite": sqlite3.sqlite_version},
        "fixed": {"limit": args.limit, "slack": 2, "context_character_budget": args.budget,
                  "gold_profile": "on-demand-v2", "split": args.split, "cache": "fresh per arm"},
        "variants": {key: {"stages": list(value[0]), "lfhv": value[1]} for key, value in VARIANTS.items()},
        "source_sha256": hashes, "data_sha256": data_hashes, "suites": {}}
    with tempfile.TemporaryDirectory(prefix="memweave-ablation-") as folder, \
            (args.output / "predictions.jsonl").open("w", encoding="utf-8") as predictions:
        temp = Path(folder)
        for suite, cases in suites.items():
            first, timings = {v: [] for v in VARIANTS}, {v: [] for v in VARIANTS}
            for index, case in enumerate(cases):
                # Each case gets a fresh seed directory; no user-scope leakage.
                case_dir = temp / f"{suite}-{index}"
                case_dir.mkdir()
                seed = case_dir / "seed.db"
                project, aliases = seed_case(case, seed)
                for repeat in range(args.repeats):
                    order = list(VARIANTS)
                    random.Random(20260930 + index * 101 + repeat).shuffle(order)
                    for variant in order:
                        database = case_dir / f"{variant}-{repeat}.db"
                        clone_database(seed, database)
                        result = run_arm(case, database, project, aliases, variant,
                                         limit=args.limit, budget=args.budget)
                        if repeat == 0:
                            first[variant].append(result)
                        elif semantic_result(first[variant][-1]) != semantic_result(result):
                            raise AssertionError("Non-deterministic semantic result: " + case["case_id"])
                        timings[variant].append(result["recall_ms"])
                        predictions.write(json.dumps({"suite": suite, "repeat": repeat, **result}, ensure_ascii=False) + "\n")
                if (index + 1) % 25 == 0 or index + 1 == len(cases):
                    print(f"{suite}: {index + 1}/{len(cases)} cases, {args.repeats} repeats, 5 arms", flush=True)
            summaries = {}
            for variant in VARIANTS:
                summaries[variant] = summarize(cases, first[variant])
                summaries[variant].update(recall_p50_ms=percentile(timings[variant], 0.5),
                                          recall_p95_ms=percentile(timings[variant], 0.95),
                                          timing_samples=len(timings[variant]))
            report["suites"][suite] = {"variants": summaries,
                "by_category": {category: {v: summarize(
                    [c for c in cases if c["category"] == category],
                    [p for c, p in zip(cases, first[v]) if c["category"] == category]) for v in VARIANTS}
                    for category in sorted({c["category"] for c in cases})},
                "paired_vs_full": {v: paired_changes(cases, first["full"], first[v])
                                   for v in VARIANTS if v != "full"}}
    current = {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    if current != hashes or any(hashlib.sha256((REPO / name).read_bytes()).hexdigest() != digest
                                for name, digest in data_hashes.items()):
        raise RuntimeError("Code or data changed during the frozen experiment")
    semantics = [semantic_result(json.loads(line)) for line in
                 (args.output / "predictions.jsonl").read_text(encoding="utf-8").splitlines()]
    report["semantic_sha256"] = hashlib.sha256(json.dumps(semantics, sort_keys=True).encode()).hexdigest()
    report["semantic_repeat_check"] = "exact_match" if args.repeats > 1 else "not_checked"
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.output / "REPORT.md")
    print(json.dumps({suite: group["variants"] for suite, group in report["suites"].items()}, indent=2))


if __name__ == "__main__":
    main()

"""Default-stage equivalence and cold/warm latency; private snapshots, no LLM."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import random
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")]
from agent_knowledge_bridge import retrieval_stats
from agent_knowledge_bridge.store import KnowledgeStore
from agent_knowledge_bridge.reuse import ReuseStore
from agent_knowledge_bridge.live_benchmark import build_live_benchmark
from fixtures.retrieval_before_stages import LegacyKnowledgeStore
from fixtures.reuse_before_stages import LegacyReuseStore
from benchmark_recall_frequency import clone, timing
from evaluate_memweave_live_100 import seed_knowledge


def quality_metrics(rows, k=3):
    positive = [r for r in rows if r['expected_ids']]
    negative = [r for r in rows if not r['expected_ids']]
    recall, precision, reciprocal = [], [], []
    for r in positive:
        expected = set(r['expected_ids'])
        retrieved = list(dict.fromkeys(r['retrieved_ids']))[:k]
        hits = len(expected.intersection(retrieved))
        recall.append(hits / len(expected))
        precision.append(hits / k)
        reciprocal.append(next((1 / rank for rank, key in enumerate(retrieved, 1) if key in expected), 0))
    mean = lambda values: sum(values) / len(values) if values else None
    return {'positive_cases': len(positive), 'negative_cases': len(negative),
            'recall_at_3': mean(recall), 'precision_at_3': mean(precision),
            'mrr_at_3': mean(reciprocal),
            'negative_injection_rate': mean([bool(r['emitted_ids']) for r in negative])}


def measure(cases, databases, repeats, gold=None):
    stores = {"before": LegacyKnowledgeStore(databases["before"]),
              "after": KnowledgeStore(databases["after"])}
    emitters = {"before": LegacyReuseStore(databases["before"]),
                "after": ReuseStore(databases["after"])}
    variants = ("before", "after_cold", "after_warm")
    durations = {v: [] for v in variants}
    stage_times = {v: {} for v in variants if v != "before"}
    stats_counts = {v: {"hits": 0, "misses": 0, "batches": 0} for v in stage_times}
    fingerprints = {v: {} for v in variants}
    expected_contexts = {}
    quality = {v: [] for v in variants}
    for repeat in range(repeats):
        for case in cases:
            args = dict(requester_agent=case["requester_agent"], project_key=case["project_key"],
                        query=case["query"], limit=3)
            order = list(variants)
            if repeat % 2:
                order.reverse()
            for variant in order:
                store = stores["before" if variant == "before" else "after"]
                if variant != "before":
                    # Cold means no query-statistics entries, not a cold OS page cache.
                    retrieval_stats.FREQUENCIES = retrieval_stats.FrequencyCache()
                    if variant == "after_warm":
                        store.search(**args)
                start = time.perf_counter()
                result = store.search(**args)
                elapsed = (time.perf_counter() - start) * 1000
                durations[variant].append(elapsed)
                if variant != "before":
                    diag = result["retrieval_diagnostics"]
                    for report in diag["stages"]:
                        stage_times[variant].setdefault(report["stage"], []).append(report["elapsed_ms"])
                    for key in stats_counts[variant]:
                        stats_counts[variant][key] += diag["statistics"][key]
                signature = [(r["id"], r["origin"], r.get("related_to")) for r in result["results"]]
                emitter = emitters["before" if variant == "before" else "after"]
                _, context_text, emitted = emitter.start(agent_id=case["requester_agent"],
                    project_key=case["project_key"], session_id="stage-benchmark",
                    turn_id=f"{variant}-{repeat}-{case['id']}", prompt=case["query"],
                    records=result["results"], retrieval_ms=elapsed, budget=4000)
                normalized = re.sub(r'rt_[a-f0-9]{20}', 'TRACE', context_text)
                if variant == 'before':
                    expected = normalized
                    returned = {r['id'] for r in result['results']}
                    with store._connect() as db:
                        for record in result['results']:
                            parent = record.get('related_to')
                            if record.get('origin') != 'sibling' or not parent or parent in returned:
                                continue
                            title_row = db.execute('SELECT title FROM knowledge_records WHERE id=?', (parent,)).fetchone()
                            if title_row:
                                expected = expected.replace(f'topic= (inferred from [{parent}]',
                                                            f'topic={title_row[0]} (inferred from [{parent}]')
                    expected_contexts[repeat, case['id']] = expected
                fingerprints[variant][repeat, case["id"]] = (signature, emitted, normalized)
                if repeat == 0 and gold is not None:
                    expected = gold.get(case.get("expected_key"))
                    quality[variant].append({"id": case["id"], "expected_ids": [expected] if expected else [],
                        "retrieved_ids": [r["id"] for r in result["results"]], "emitted_ids": emitted,
                        "retrieval_ms": elapsed})
        print(f"{len(cases)} cases: repeat {repeat + 1}/{repeats}", flush=True)
    equivalence = {}
    for variant in variants[1:]:
        mismatches = [key for key, old in fingerprints["before"].items()
                      if old != fingerprints[variant][key]]
        equivalence[variant] = {
            "ordered_result_mismatches": sum(fingerprints["before"][k][0] != fingerprints[variant][k][0]
                                            for k in mismatches),
            "emission_mismatches": sum(fingerprints["before"][k][1] != fingerprints[variant][k][1]
                                      for k in mismatches),
            "context_mismatches": sum(fingerprints["before"][k][2] != fingerprints[variant][k][2]
                                     for k in mismatches),
            "expected_parent_title_fixes": sum(fingerprints['before'][k][2] != fingerprints[variant][k][2]
                                              and expected_contexts[k] == fingerprints[variant][k][2]
                                              for k in mismatches),
            "unexpected_context_mismatches": sum(expected_contexts[k] != fingerprints[variant][k][2]
                                                for k in fingerprints['before']),
            "all_equal": not mismatches,
            "compatible_except_documented_title_fix": all(
                old[:2] == fingerprints[variant][k][:2] and expected_contexts[k] == fingerprints[variant][k][2]
                for k, old in fingerprints['before'].items())}
    return {"cases": len(cases), "repeats": repeats, "equivalence": equivalence,
            "search_latency": {v: timing(vals) for v, vals in durations.items()},
            "stage_latency": {v: {s: timing(vals) for s, vals in stages.items()}
                              for v, stages in stage_times.items()},
            "statistics": stats_counts,
            "quality": {v: quality_metrics(rows) for v, rows in quality.items()} if gold else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-db", type=Path)
    parser.add_argument("--project", default="claude-codex-mvp")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be 1..10")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"measurement": "Local SQLite retrieval and context equivalence; no model calls, no task-success or TTFT claim. Cold/warm refers only to DF cache.",
              "sqlite_version": sqlite3.sqlite_version,
              "source_sha256": {name: hashlib.sha256((ROOT / "src" / "agent_knowledge_bridge" / name).read_bytes()).hexdigest()
                                for name in ("store.py", "retrieval_pipeline.py", "retrieval_stats.py", "reuse.py")}}
    with tempfile.TemporaryDirectory(prefix="memweave-stages-") as folder:
        root = Path(folder)
        dataset = build_live_benchmark()
        seed = root / "seed.db"
        gold, _ = seed_knowledge(seed, dataset)
        databases = {v: root / f"{v}.db" for v in ("before", "after")}
        for target in databases.values():
            clone(seed, target)
        report["synthetic_100"] = measure(dataset["cases"], databases, args.repeats, gold)
        if args.live_db:
            snapshot = root / "snapshot.db"
            clone(args.live_db, snapshot)
            with contextlib.closing(sqlite3.connect(snapshot)) as db:
                titles = db.execute("SELECT title FROM knowledge_records WHERE status IN ('active','stale') "
                    "AND (scope='user' OR project_key=?) ORDER BY id", (args.project,)).fetchall()
            random.Random(20260924).shuffle(titles)
            queries = [r[0][:500] for r in titles[:50]] + [
                "论文第三方渠道应该如何披露，具体是哪个项目？", "关于第三方平台的论文信息",
                "为什么 Codex 和 Claude Code 的共享知识没有显示？", "知识库归档之后如何召回",
                "查询起始时间和结束时间，按更新时间降序排序", "今天下午的天气怎么样？",
                "请计算二十一除以三", "明天午饭吃什么", "猫咪的照片", "本地数据库如何备份和恢复"]
            cases = [dict(id=f"local-{i}", query=q, project_key=args.project,
                          requester_agent="codex" if i % 2 else "claude-code")
                     for i, q in enumerate(queries)]
            databases = {v: root / f"live-{v}.db" for v in ("before", "after")}
            for target in databases.values():
                clone(snapshot, target)
            report["local_snapshot"] = measure(cases, databases, args.repeats)
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: {n: v[n] for n in ("cases", "equivalence", "search_latency")}
                      for k, v in report.items() if k in ("synthetic_100", "local_snapshot")},
                     ensure_ascii=False, indent=2))
    return int(any(not x["compatible_except_documented_title_fix"] for k in ("synthetic_100", "local_snapshot")
                   for x in report.get(k, {}).get("equivalence", {}).values()))


if __name__ == "__main__":
    raise SystemExit(main())

"""Paired local benchmark; never calls a model or writes to the live database.

The legacy mode reproduces full per-term MATCH counts. Both modes keep every
ranking, expansion, budget and lifecycle rule. Report ordered result equality,
not just aggregate recall. Live snapshots and per-turn writes stay in a temporary
directory; reports contain no real knowledge text or IDs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

import agent_knowledge_bridge.store as store_module
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.evaluation import retrieval_metrics
from agent_knowledge_bridge.live_benchmark import build_live_benchmark
from agent_knowledge_bridge.reuse import percentile
from evaluate_memweave_live_100 import seed_knowledge

FAST = store_module.bounded_document_frequencies


def legacy_counts(db, terms, *, ceiling):
    counts = {}
    for term in dict.fromkeys(terms):
        try:
            counts[term] = db.execute(
                'SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?',
                ('"' + term.replace('"', '""') + '"',),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return counts


@contextlib.contextmanager
def mode(variant):
    store_module.bounded_document_frequencies = legacy_counts if variant == 'before' else FAST
    try:
        yield
    finally:
        store_module.bounded_document_frequencies = FAST


def clone(source, destination):
    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def timing(values):
    return {'p50_ms': percentile(values, .5), 'p95_ms': percentile(values, .95),
            'max_ms': round(max(values), 3), 'samples': len(values)}


def measure(cases, databases, repeats, gold=None):
    stores = {v: store_module.KnowledgeStore(p) for v, p in databases.items()}
    agent_projects = {(c['requester_agent'], c['project_key']) for c in cases}
    adapters = {v: {(agent, project): ClaudeLearningAdapter(
        database_path=p, agent_id=agent, project_key=project,
        reviewer=lambda _: {'proposals': []}) for agent, project in agent_projects} for v, p in databases.items()}
    measurements = {v: {'search': [], 'adapter': []} for v in databases}
    fingerprints = {v: {} for v in databases}
    eval_rows = {v: [] for v in databases}
    for repeat in range(repeats):
        order = list(databases) if repeat % 2 == 0 else list(reversed(databases))
        for variant in order:
            with mode(variant):
                for case in cases:
                    started = time.perf_counter()
                    result = stores[variant].search(requester_agent=case['requester_agent'],
                        project_key=case['project_key'], query=case['query'], limit=3)
                    elapsed = (time.perf_counter() - started) * 1000
                    measurements[variant]['search'].append(elapsed)
                    signature = [(r['id'], r['origin'], r.get('related_to')) for r in result['results']]
                    adapter = adapters[variant][case['requester_agent'], case['project_key']]
                    turn = f"{repeat}-{case['id']}"
                    started = time.perf_counter()
                    adapter.recall({'session_id': 'frequency-benchmark', 'turn_id': turn, 'prompt': case['query']})
                    adapter_ms = (time.perf_counter() - started) * 1000
                    measurements[variant]['adapter'].append(adapter_ms)
                    trace = adapter.reuse.existing(case['requester_agent'], case['project_key'], 'frequency-benchmark', turn)
                    emitted = [i['knowledge_id'] for i in json.loads(trace['items_json']) if i['emitted']]
                    fingerprints[variant][turn] = (signature, emitted)
                    if repeat == 0 and gold is not None:
                        expected = gold.get(case.get('expected_key'))
                        eval_rows[variant].append({'id': case['id'], 'expected_ids': [expected] if expected else [],
                            'retrieved_ids': [r['id'] for r in result['results']], 'emitted_ids': emitted,
                            'retrieval_ms': elapsed, 'adapter_ms': adapter_ms})
            print(f'{len(cases)} cases / repeat {repeat + 1} / {variant}', flush=True)
    mismatches = [key for key in fingerprints['before'] if fingerprints['before'][key] != fingerprints['after'][key]]
    return {'cases': len(cases), 'repeats': repeats, 'ordered_results_and_emissions_equal': not mismatches,
            'mismatch_count': len(mismatches), 'timing': {v: {k: timing(vals) for k, vals in m.items()} for v, m in measurements.items()},
            'quality': {v: retrieval_metrics(rows, k=3) for v, rows in eval_rows.items()} if gold else None}


def stress_counts():
    with contextlib.closing(sqlite3.connect(':memory:')) as db:
        db.execute("CREATE VIRTUAL TABLE knowledge_fts USING fts5(content, tokenize='unicode61')")
        terms = store_module.retrieval_tokens('处理任务标记并应用持久策略返回结果请确认共享知识库来源并核对协议字段')
        text = ' '.join(terms)
        db.executemany('INSERT INTO knowledge_fts VALUES (?)', ((text,) for _ in range(10000)))
        samples = {'before': [], 'after': []}
        equal = True
        for rep in range(8):
            variants = [('before', legacy_counts), ('after', FAST)]
            if rep % 2: variants.reverse()
            results = {}
            for variant, fn in variants:
                started = time.perf_counter()
                results[variant] = fn(db, terms, ceiling=6)
                samples[variant].append((time.perf_counter() - started) * 1000)
            equal &= all(min(results['before'][t], 7) == results['after'][t] for t in terms)
        return {'rows': 10000, 'terms': len(terms), 'same_threshold_decisions': equal,
                'timing': {v: timing(vals) for v, vals in samples.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live-db', type=Path)
    parser.add_argument('--project', default='claude-codex-mvp')
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10: parser.error('repeats must be 1..10')
    args.output.mkdir(parents=True, exist_ok=True)
    report = {'measurement': 'Paired same-host, same-corpus local search and adapter; no model calls, no TTFT claim.',
              'sqlite_version': sqlite3.sqlite_version, 'source_sha256': hashlib.sha256(Path(store_module.__file__).read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='memweave-frequency-') as temp:
        root = Path(temp)
        dataset = build_live_benchmark()
        seed = root / 'seed.db'
        ids, _ = seed_knowledge(seed, dataset)
        databases = {v: root / f'{v}.db' for v in ('before', 'after')}
        for p in databases.values(): clone(seed, p)
        report['synthetic_100'] = measure(dataset['cases'], databases, args.repeats, ids)
        if args.live_db:
            snapshot = root / 'live-snapshot.db'
            clone(args.live_db, snapshot)
            with contextlib.closing(sqlite3.connect(snapshot)) as db:
                titles = db.execute("SELECT title FROM knowledge_records WHERE status IN ('active','stale') "
                                    "AND (scope='user' OR project_key=?) ORDER BY id", (args.project,)).fetchall()
            random.Random(20260924).shuffle(titles)
            queries = [r[0][:500] for r in titles[:50]] + [
                '论文第三方渠道应该如何披露，具体是哪个项目？', '关于第三方平台的论文信息',
                '为什么 Codex 和 Claude Code 的共享知识没有显示？', '知识库归档之后如何召回',
                '查询起始时间和结束时间，按更新时间降序排序', '今天下午的天气怎么样？',
                '请计算二十一除以三', '明天午饭吃什么', '猫咪的照片', '本地数据库如何备份和恢复',
            ]
            cases = [{'id': f'local-{i}', 'query': q, 'project_key': args.project,
                      'requester_agent': 'codex' if i % 2 == 0 else 'claude-code'} for i, q in enumerate(queries)]
            live_databases = {v: root / f'live-{v}.db' for v in ('before', 'after')}
            for p in live_databases.values(): clone(snapshot, p)
            report['local_snapshot'] = measure(cases, live_databases, args.repeats)
        report['frequency_stress'] = stress_counts()
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(not report['synthetic_100']['ordered_results_and_emissions_equal'] or
               not report.get('local_snapshot', {}).get('ordered_results_and_emissions_equal', True) or
               not report['frequency_stress']['same_threshold_decisions'])


if __name__ == '__main__':
    raise SystemExit(main())

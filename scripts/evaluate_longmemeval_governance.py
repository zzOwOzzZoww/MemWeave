"""Public-history admission replay, not an official governance/QA benchmark.

No model calls, production databases, manual activation, gold-guided extraction,
or policy changes. Each question runs in an independent temporary database.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
from pathlib import Path
import platform
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'src')]

from agent_knowledge_bridge.claude_transcript import TranscriptTurn, redact_text
from agent_knowledge_bridge.learning_engine import LearningEngine
from evaluate_longmemeval_ablation import DATA_SHA256, REVISION, date_key, digest, percentile, validate_rows

PROTOCOL = 'longmemeval-governance-audit-v2-content-coverage'
# A frozen, source-only adapter, not a trained classifier or a gold label.
PREFERENCE_CUE = re.compile(r'\b(?:prefer|preference|always|from now on|by default|remember)\b', re.I)


def source_hashes():
    paths = list((ROOT / 'src/agent_knowledge_bridge').rglob('*.py')) + [
        Path(__file__), ROOT / 'scripts/evaluate_longmemeval_ablation.py',
        ROOT / 'scripts/audit_governance_retention.py',
        ROOT / 'evaluation/longmemeval-governance-v1/README.md',
        ROOT / 'evaluation/longmemeval-governance-v1/ASSERTION_REPLAY.md',
        ROOT / 'evaluation/longmemeval-governance-v1/QUALITY_AUDIT.md',
        ROOT / 'evaluation/longmemeval-governance-v1/ATOMIC_EVIDENCE.md']
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(paths)}


def history_inputs(row):
    """Only raw history, role and dates cross the input boundary; no gold IDs."""
    result = []
    sessions, dates = row['haystack_sessions'], row['haystack_dates']
    if len(sessions) != len(dates):
        raise ValueError('Unaligned history dates')
    for si in sorted(range(len(sessions)), key=lambda i: (date_key(dates[i]), i)):
        session = sessions[si]
        for ti, turn in enumerate(session):
            if turn['role'] != 'user':
                continue
            user = turn['content']
            if not isinstance(user, str):
                raise ValueError('Expected textual public user turn')
            replies = []
            for following in session[ti + 1:]:
                if following['role'] == 'user':
                    break
                if following['role'] == 'assistant':
                    replies.append(following['content'])
            result.append({'session_index': si, 'turn_index': ti,
                           'user': user, 'assistant': '\n'.join(replies)})
    return result


def fixed_proposal(user):
    """Whole-turn stress input. Real reviewers normally select shorter assertions.

    Do not truncate a large turn and then claim that all its evidence survived.
    Oversized inputs are reported separately, and remain in score denominators.
    """
    text = user.strip()
    if len(text) > 8000:
        return None
    return {'title': re.sub(r'\s+', ' ', text)[:120] or 'Empty public statement',
            'content': text, 'knowledge_type': 'preference' if PREFERENCE_CUE.search(text) else 'fact',
            'scope': 'project', 'source_role': 'user', 'source_quotes': [text],
            'retention': 'durable', 'search_terms': '', 'subject_terms': []}


def replay_history(inputs, database):
    """Actually exercise LearningEngine.learn, persistence, dedup and promotion.

    The deliberately permissive fixed reviewer proposes every supported turn,
    so these are Core admission results, NOT real model extraction quality.
    """
    current = {'turn': TranscriptTurn('', '', ()), 'proposal': None}
    engine = LearningEngine(database_path=database, agent_id='governance-audit',
        project_key='public-history', transcript_parser=lambda *a, **k: current['turn'],
        reviewer=lambda _: {'proposals': [current['proposal']], '_require_source_quotes': True})
    outputs, timings, source_texts = [], [], {}
    for item in inputs:
        si, ti = item['session_index'], item['turn_index']
        source_texts[(si, ti)] = item['user']
        proposal = fixed_proposal(item['user'])
        output = {'session_index': si, 'turn_index': ti,
                  'source_sha256': hashlib.sha256(item['user'].encode()).hexdigest(),
                  'outcome': 'adapter_oversize', 'reasons': {}, 'promoted': 0,
                  'retention_coverage': 'none', 'retained_characters': 0, 'retained_spans': []}
        if proposal is not None:
            current['turn'] = TranscriptTurn(item['user'], item['assistant'], (),
                                            source_turn_key=f's{si}:t{ti}')
            current['proposal'] = proposal
            started = time.perf_counter()
            result = engine.learn({'session_id': f'history-s{si}', 'turn_id': f't{ti}'})
            timings.append((time.perf_counter() - started) * 1000)
            if result['status'] != 'completed':
                raise ValueError('Unexpected replay result: ' + result['status'])
            output.update(outcome='filtered' if not result['proposals'] else 'pending_state',
                          reasons=result['rejection_reasons'], promoted=result['promoted'],
                          _run=result['compiler']['run_id'])
        outputs.append(output)
    # Query final states after the entire history, not optimistic proposal counts.
    with engine.store.knowledge._connect() as db:
        run_records = {r['run_id']: dict(r) for r in db.execute('''
            SELECT l.run_id, r.status, r.content FROM knowledge_compilation_links l
            JOIN knowledge_records r ON r.id=l.knowledge_id''')}
        unique_states = dict(db.execute('SELECT status,COUNT(*) FROM knowledge_records GROUP BY status'))
    for output in outputs:
        run = output.pop('_run', None)
        if output['outcome'] == 'pending_state':
            record = run_records[run]
            output['outcome'] = record['status']
            source = source_texts[(output['session_index'], output['turn_index'])]
            content = record['content']
            output['retention_coverage'] = 'full' if source.strip() in content else 'partial'
            spans = retained_source_spans(source, content)
            output['retained_spans'] = spans
            output['retained_characters'] = sum(b - a for a, b in spans)
    return outputs, timings, unique_states


def retained_source_spans(source, content):
    """Offsets into the raw source, ignoring only outer whitespace for full.

    Literal coverage is not answer/semantic evidence recovery. Fail closed if
    persistence cannot be traced back to ordered, verbatim source fragments.
    """
    stripped = source.strip()
    if stripped and stripped in content:
        start = len(source) - len(source.lstrip())
        return [[start, start + len(stripped)]]
    spans = []
    cursor = 0
    for fragment in content.splitlines():
        fragment = fragment.strip()
        if not fragment:
            continue
        start = source.find(fragment, cursor)
        if start < 0:
            raise ValueError('Retained text is not an ordered verbatim source fragment')
        spans.append([start, start + len(fragment)])
        cursor = start + len(fragment)
    if not spans:
        raise ValueError('Retained text has no verbatim source fragment')
    return spans


def score_evidence(row, outputs):
    """Only the scorer reads has_answer; absence is NOT a negative governance label."""
    if row['question_id'].endswith('_abs'):
        return {'applicable': False, 'reason': 'abstention', 'user_evidence': 0,
                'assistant_evidence': 0, 'retained': 0, 'active': 0, 'filtered': 0,
                'oversize': 0, 'full_retained': 0, 'partial_retained': 0}
    gold, assistant = set(), 0
    for si, session in enumerate(row['haystack_sessions']):
        for ti, turn in enumerate(session):
            if turn.get('has_answer') is True:
                if turn['role'] == 'user':
                    gold.add((si, ti))
                elif turn['role'] == 'assistant':
                    assistant += 1
    selected = [item for item in outputs if (item['session_index'], item['turn_index']) in gold]
    if len(selected) != len(gold):
        raise ValueError('A gold user turn disappeared from the replay')
    return {'applicable': bool(gold), 'reason': None if gold else 'no_user_evidence',
            'user_evidence': len(gold), 'assistant_evidence': assistant,
            'retained': sum(o['outcome'] in {'candidate', 'active', 'stale'} for o in selected),
            'active': sum(o['outcome'] in {'active', 'stale'} for o in selected),
            'filtered': sum(o['outcome'] == 'filtered' for o in selected),
            'oversize': sum(o['outcome'] == 'adapter_oversize' for o in selected),
            'full_retained': sum(o['outcome'] in {'candidate', 'active', 'stale'}
                                 and o.get('retention_coverage', 'full') == 'full' for o in selected),
            'partial_retained': sum(o['outcome'] in {'candidate', 'active', 'stale'}
                                    and o.get('retention_coverage') == 'partial' for o in selected)}


def evaluate_case(task):
    index, row = task
    inputs = history_inputs(row)
    with tempfile.TemporaryDirectory(prefix='memweave-governance-audit-') as directory:
        outputs, timings, unique_states = replay_history(inputs, Path(directory) / 'audit.db')
    return {'index': index, 'question_id': row['question_id'], 'question_type': row['question_type'],
            'turns': outputs, 'latencies_ms': timings, 'unique_record_states': unique_states,
            'evidence': score_evidence(row, outputs)}


def evidence_summary(cases):
    eligible = [case['evidence'] for case in cases if case['evidence']['applicable']]
    total = sum(s['user_evidence'] for s in eligible)
    retained = sum(s['retained'] for s in eligible)
    active = sum(s['active'] for s in eligible)
    full = sum(s['full_retained'] for s in eligible)
    partial = sum(s['partial_retained'] for s in eligible)
    return {'questions_with_user_evidence': len(eligible), 'labeled_user_evidence_turns': total,
            'retained_including_candidates': retained,
            'retention_rate_including_candidates': retained / total if total else None,
            'full_source_retained_including_candidates': full,
            'partial_source_retained_including_candidates': partial,
            'full_source_retention_rate': full / total if total else None,
            'partial_source_presence_rate': partial / total if total else None,
            'active_evidence_turns': active, 'active_evidence_rate': active / total if total else None,
            'filtered_labeled_evidence_turns': sum(s['filtered'] for s in eligible),
            'oversize_labeled_evidence_turns': sum(s['oversize'] for s in eligible),
            'questions_any_evidence_retained': sum(s['retained'] > 0 for s in eligible),
            'questions_all_evidence_retained': sum(s['retained'] == s['user_evidence'] for s in eligible),
            'assistant_evidence_turns_not_evaluated': sum(c['evidence']['assistant_evidence'] for c in cases)}


def summarize(cases):
    turns = [turn for case in cases for turn in case['turns']]
    outcomes = Counter(t['outcome'] for t in turns)
    reasons = Counter()
    unique_states = Counter()
    for turn in turns:
        reasons.update(turn['reasons'])
    for case in cases:
        unique_states.update(case['unique_record_states'])
    timings = [ms for case in cases for ms in case['latencies_ms']]
    by_type = defaultdict(list)
    for case in cases:
        if not case['question_id'].endswith('_abs'):
            by_type[case['question_type']].append(case)
    return {'questions': len(cases), 'user_turn_occurrences': len(turns),
            'unique_user_texts': len({t['source_sha256'] for t in turns}),
            'outcomes': dict(outcomes), 'filter_reasons': dict(reasons),
            'retention_coverage': dict(Counter(t.get('retention_coverage', 'full')
                if t['outcome'] in {'candidate', 'active', 'stale'} else 'none' for t in turns)),
            'automatic_promotion_events': sum(t['promoted'] for t in turns),
            'automatic_promotion_fraction_of_offered_turns': sum(t['promoted'] for t in turns) / len(turns) if turns else None,
            'unique_record_states': dict(unique_states),
            'evidence': evidence_summary(cases),
            'by_question_type': {key: evidence_summary(value) for key, value in sorted(by_type.items())},
            'learn_p50_ms_under_parallel_load': percentile(timings, .5),
            'learn_p95_ms_under_parallel_load': percentile(timings, .95),
            'learn_calls': len(timings),
            'auto_accept_precision': None, 'correct_filter_rate': None,
            'not_scored': ['independent governance action labels unavailable',
                           'real model extraction and MCP planner not run',
                           'merge/supersede/LFHV and final QA not evaluated']}


def write_report(output, report):
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    m, e = report['metrics'], report['metrics']['evidence']
    text = '\n'.join([
        '## Run provenance', '', '- Generator: scripts/evaluate_longmemeval_governance.py',
        '- Verification Status: COMPLETED; not an independent validation',
        '- Version Label: ' + PROTOCOL, '', '# LongMemEval 自动准入诊断', '',
        '固定原文候选、真实 Core 入库；不是官方治理成绩，不是付费模型端到端成绩。', '',
        f"- 问题：{m['questions']}；用户消息出现次数：{m['user_turn_occurrences']}；不同原文：{m['unique_user_texts']}。",
        '- 最终处理分布：`' + json.dumps(m['outcomes'], ensure_ascii=False) + '`。',
        f"- 自动晋升事件：{m['automatic_promotion_events']}。没有人工批准。",
        f"- 官方用户标注消息：{e['labeled_user_evidence_turns']}；有内容保留（含待审）：{e['retained_including_candidates']}；active：{e['active_evidence_turns']}。",
        f"- 整条原文保留：{e['full_source_retained_including_candidates']}；仅部分原文保留：{e['partial_source_retained_including_candidates']}。部分保留不能解释为答题证据已经恢复。",
        f"- 标注证据被过滤：{e['filtered_labeled_evidence_turns']}；整轮过长未接入：{e['oversize_labeled_evidence_turns']}。",
        '- 拦截理由：`' + json.dumps(m['filter_reasons'], ensure_ascii=False) + '`。', '',
        '## 不能据此声称', '',
        '- 过滤率不是正确过滤率；未标 has_answer 不代表无长期价值。',
        '- 候选保留不代表可召回，active 数量不代表正确批准数量。',
        '- 生活历史的记忆政策与 Coding Agent 稳定规则政策并不相同。',
        '- 重复语料和同题多消息不是独立样本；不做显著性或生产收益推断。',
        '- 本数据已用于之前的检索和准入诊断，修复受其启发；不是全新盲测，不做独立泛化结论。',
        '- v2 保持输入和消息分母不变，新增实际存储原文覆盖；部分保留不能与旧整条证据保留率直接等同。',
        '- 本次只测固定原文候选下的自动准入，未测真实提取模型、MCP 规划、归并、版本替代或最终问答。',
        '- 并发 learn 延迟含 SQLite/审计写入和资源争用，不是线上单请求或检索 TTFT。', ''])
    (output / 'REPORT.md').write_text(text, encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--max-cases', type=int, default=None, help='Smoke only; not a full-set result')
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 32:
        parser.error('workers must be 1..32')
    output = args.output.resolve()
    # Raw source-derived audit artifacts must remain ignored, not in public docs.
    if not output.is_relative_to((ROOT / 'outputs').resolve()):
        parser.error('output must be a new directory under outputs/')
    if args.dataset.resolve().is_relative_to(output) or output.exists():
        parser.error('output must not exist or contain the input')
    if digest(args.dataset) != DATA_SHA256:
        parser.error('public dataset hash mismatch')
    rows = json.loads(args.dataset.read_text(encoding='utf-8'))
    validate_rows(rows)
    if len(rows) != 500:
        parser.error('expected the frozen 500-question dataset')
    if args.max_cases is not None:
        if not 1 <= args.max_cases <= 500:
            parser.error('max-cases must be 1..500')
        rows = rows[:args.max_cases]
    frozen = source_hashes()
    output.mkdir(parents=True)
    manifest = {'protocol': PROTOCOL, 'dataset_revision': REVISION, 'dataset_sha256': DATA_SHA256,
                'frozen_sources': frozen, 'questions': len(rows), 'workers': args.workers,
                'python': platform.python_version(), 'platform': platform.platform(),
                'started_at': datetime.now(timezone.utc).isoformat(),
                'history_used_for_prior_retrieval_diagnostics': True,
                'model_calls': 0, 'production_database_access': False}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    started = time.perf_counter()
    cases = []
    with (output / 'cases.jsonl').open('w', encoding='utf-8') as handle:
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context('spawn')) as pool:
            futures = [pool.submit(evaluate_case, item) for item in enumerate(rows)]
            for future in as_completed(futures):
                case = future.result()  # No auto-retry or silent case removal.
                cases.append(case)
                handle.write(json.dumps(case, ensure_ascii=False) + '\n')
                handle.flush()
                print(f"Completed {len(cases)}/{len(rows)}; elapsed {time.perf_counter()-started:.1f}s", flush=True)
    if source_hashes() != frozen or digest(args.dataset) != DATA_SHA256:
        raise RuntimeError('Source/data changed during experiment; result invalid')
    cases.sort(key=lambda c: c['index'])
    report = {'manifest': manifest, 'elapsed_seconds': time.perf_counter() - started,
              'metrics': summarize(cases)}
    write_report(output, report)
    print(json.dumps(report['metrics'], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

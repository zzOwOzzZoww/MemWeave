"""Offline content audit of a completed replay; never calls Core or a model.

Answer-surface coverage is a diagnostic proxy, not semantic support, QA accuracy,
admission precision or an authorization signal. Gold stays in this scorer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from evaluate_longmemeval_ablation import DATA_SHA256, digest


def tokens(text):
    return re.findall(r'\w+(?:[.-]\w+)*', unicodedata.normalize('NFKC', str(text)).casefold())


def contains_answer(text, answer):
    source, needle = tokens(text), tokens(answer)
    return bool(needle) and any(source[i:i + len(needle)] == needle
        for i in range(len(source) - len(needle) + 1))


def source_fragments(source, turn):
    """Validate actual stored-body spans from the replay, not guessed quotes."""
    coverage, spans = turn['retention_coverage'], turn['retained_spans']
    if turn['outcome'] not in {'candidate', 'active', 'stale'}:
        if coverage != 'none' or spans or turn['retained_characters']:
            raise ValueError('Unretained turn has stored-body coverage')
        return []
    if coverage not in {'full', 'partial'} or not spans:
        raise ValueError('Retained turn lacks stored-body coverage')
    fragments, previous = [], 0
    for span in spans:
        if (not isinstance(span, list) or len(span) != 2
                or any(type(value) is not int for value in span)):
            raise ValueError('Invalid source span')
        a, b = span
        if not 0 <= previous <= a < b <= len(source):
            raise ValueError('Unordered or out-of-range source span')
        previous = b
        fragments.append(source[a:b])
    if sum(len(fragment) for fragment in fragments) != turn['retained_characters']:
        raise ValueError('Source character count mismatch')
    if coverage == 'full' and fragments != [source.strip()]:
        raise ValueError('Partial text mislabeled as full coverage')
    return fragments


def surface_category(source, fragments, answer):
    # Do not join disjoint spans to manufacture a contiguous answer phrase.
    if not contains_answer(source, answer):
        return 'not_literal_in_source'
    return ('literal_in_retained' if any(contains_answer(fragment, answer) for fragment in fragments)
            else 'literal_not_retained')


def audit(rows, cases):
    by_index = {case['index']: case for case in cases}
    if len(by_index) != len(cases) or set(by_index) != set(range(len(rows))):
        raise ValueError('Incomplete or duplicate replay cases')
    details, coverage, by_type, duplicate_bodies = [], Counter(), {}, 0
    for index, row in enumerate(rows):
        case = by_index[index]
        if case['question_id'] != row['question_id']:
            raise ValueError('Question alignment mismatch')
        expected = {(si, ti): turn for si, session in enumerate(row['haystack_sessions'])
                    for ti, turn in enumerate(session) if turn['role'] == 'user'}
        seen, bodies = set(), set()
        for turn in case['turns']:
            key = turn['session_index'], turn['turn_index']
            if key not in expected or key in seen:
                raise ValueError('Duplicate or unexpected replay input')
            seen.add(key)
            source = expected[key]['content']
            if hashlib.sha256(source.encode()).hexdigest() != turn['source_sha256']:
                raise ValueError('Replay input hash mismatch')
            fragments = source_fragments(source, turn)
            if fragments:
                body = '\n'.join(fragments)
                duplicate_bodies += int(body in bodies)
                bodies.add(body)
            if row['question_id'].endswith('_abs') or expected[key].get('has_answer') is not True:
                continue
            category = surface_category(source, fragments, row['answer'])
            coverage[turn['retention_coverage']] += 1
            by_type.setdefault(row['question_type'], Counter())[category] += 1
            details.append({'index': index, 'session_index': key[0], 'turn_index': key[1],
                'question_type': row['question_type'], 'source_sha256': turn['source_sha256'],
                'retention_coverage': turn['retention_coverage'], 'answer_surface': category})
        if seen != set(expected):
            raise ValueError('Replay dropped input messages')
    all_surface = Counter(item['answer_surface'] for item in details)
    partial_surface = Counter(item['answer_surface'] for item in details if item['retention_coverage'] == 'partial')
    return {'labeled_user_messages': len(details), 'source_coverage': dict(coverage),
        'answer_surface': dict(all_surface), 'partial_answer_surface': dict(partial_surface),
        'by_question_type': {key: dict(value) for key, value in sorted(by_type.items())},
        'repeated_retained_body_occurrences_within_case': duplicate_bodies,
        'not_scored': ['semantic answer support', 'admission correctness', 'QA accuracy',
                       'independent governance precision', 'real client acceptance']}, details


def load_run(path):
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    cases = [json.loads(line) for line in (path / 'cases.jsonl').read_text(encoding='utf-8').splitlines()]
    if report['manifest']['dataset_sha256'] != DATA_SHA256 or report['metrics']['questions'] != 500:
        raise ValueError('Expected a completed frozen 500-case replay')
    if len(cases) != 500 or sum(len(case['turns']) for case in cases) != report['metrics']['user_turn_occurrences']:
        raise ValueError('Replay counts disagree with its report')
    return report, cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to((ROOT / 'outputs').resolve()):
        parser.error('output must be a new directory under outputs/')
    if digest(args.dataset) != DATA_SHA256:
        parser.error('dataset SHA-256 mismatch')
    rows = json.loads(args.dataset.read_text(encoding='utf-8'))
    report, cases = load_run(args.run)
    summary, details = audit(rows, cases)
    result = {'protocol': 'governance-retention-surface-audit-v1',
        'audit_script_sha256': digest(Path(__file__)), 'dataset_sha256': DATA_SHA256,
        'run_report_sha256': digest(args.run / 'report.json'),
        'run_cases_sha256': digest(args.run / 'cases.jsonl'), 'metrics': summary}
    if args.baseline:
        baseline_report, before = load_run(args.baseline)
        baseline, previous_details = audit(rows, before)
        previous = {(d['index'], d['session_index'], d['turn_index']): d for d in previous_details}
        transitions = Counter()
        for item in details:
            prior = previous[(item['index'], item['session_index'], item['turn_index'])]
            if prior['source_sha256'] != item['source_sha256']:
                raise ValueError('Baseline source alignment mismatch')
            transitions[prior['answer_surface'] + '->' + item['answer_surface']] += 1
        result.update(baseline_metrics=baseline, answer_surface_transitions=dict(transitions),
            baseline_report_sha256=digest(args.baseline / 'report.json'),
            unique_record_states_before=baseline_report['metrics']['unique_record_states'],
            unique_record_states_after=report['metrics']['unique_record_states'])
    output.mkdir(parents=True)
    (output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / 'labeled_messages.jsonl').write_text(''.join(
        json.dumps(item, ensure_ascii=False) + '\n' for item in details), encoding='utf-8')
    (output / 'REPORT.md').write_text('\n'.join([
        '# 已完成回放的内容质量诊断', '',
        '仅离线读取原文、标准答案和已完成回放的保留偏移；不调用 Core、模型或生产数据库。', '',
        '完整答案字面仍在片段里，不等于语义支持：否定、反事实和主体差异都可能让它无效。',
        '答案字面不在原文时，可能需要别名、跨消息组合或推理，记为无法用此方法判定。',
        '本报告不能解释为回答准确率、自动批准精度或独立人工标注。', '',
        '```json', json.dumps(result, ensure_ascii=False, indent=2), '```', '']), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

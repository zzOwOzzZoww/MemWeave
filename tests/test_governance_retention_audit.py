"""Answer spelling is a diagnostic signal, never semantic grading."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/audit_governance_retention.py'
SPEC = importlib.util.spec_from_file_location('audit_governance_retention', SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


@pytest.mark.parametrize('source,fragments,answer,expected', [
    ('My cache is Redis. Can you help?', ['My cache is Redis.'], 'Redis', 'literal_in_retained'),
    ('My cache is Redis. Can you help?', ['Can you help?'], 'Redis', 'literal_not_retained'),
    ('My cache is Redis.', ['My cache is Redis.'], 'Remote Dictionary Server', 'not_literal_in_source'),
    ('The limit is 200.', ['The limit is 200.'], '20', 'not_literal_in_source'),
    ('Alpha Beta was configured.', ['Alpha', 'Beta'], 'Alpha Beta', 'literal_not_retained'),
    # A negated answer is still a spelling match. This must NOT be labeled QA
    # correctness or support, even when an entire gold string is present.
    ('My cache is not Redis.', ['My cache is not Redis.'], 'Redis', 'literal_in_retained'),
])
def test_surface_categories_do_not_invent_semantic_support(source, fragments, answer, expected):
    assert AUDIT.surface_category(source, fragments, answer) == expected


def fixture():
    source = 'My cache is Redis. Can you help?'
    row = {'question_id': 'fixture', 'question_type': 'single-session-user', 'answer': 'Redis',
        'haystack_sessions': [[{'role': 'user', 'content': source, 'has_answer': True}]]}
    turn = {'session_index': 0, 'turn_index': 0, 'outcome': 'candidate',
        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'retention_coverage': 'partial', 'retained_spans': [[0, 18]], 'retained_characters': 18}
    case = {'index': 0, 'question_id': 'fixture', 'turns': [turn]}
    return row, case, turn


def test_audit_preserves_the_full_labeled_denominator():
    row, case, turn = fixture()
    metrics, details = AUDIT.audit([row], [case])
    assert metrics['labeled_user_messages'] == 1
    assert metrics['partial_answer_surface'] == {'literal_in_retained': 1}
    assert 'question' not in details[0] and 'answer' not in details[0]
    turn.update(outcome='filtered', retention_coverage='none', retained_spans=[], retained_characters=0)
    assert AUDIT.audit([row], [case])[0]['answer_surface'] == {'literal_not_retained': 1}


@pytest.mark.parametrize('change', ['drop', 'duplicate', 'hash', 'range', 'count', 'full', 'fake_filtered'])
def test_missing_or_forged_replay_data_cannot_improve_the_metric(change):
    row, case, turn = fixture()
    if change == 'drop':
        case['turns'] = []
    elif change == 'duplicate':
        case['turns'].append(dict(turn))
    elif change == 'hash':
        turn['source_sha256'] = 'invalid'
    elif change == 'range':
        turn['retained_spans'] = [[0, 1000]]
    elif change == 'count':
        turn['retained_characters'] = 1
    elif change == 'full':
        turn['retention_coverage'] = 'full'
    else:
        turn['outcome'] = 'filtered'
    with pytest.raises(ValueError):
        AUDIT.audit([row], [case])


def test_abstention_and_unlabeled_messages_are_not_negative_governance_labels():
    row, case, _ = fixture()
    row['question_id'] = case['question_id'] = 'fixture_abs'
    assert AUDIT.audit([row], [case])[0]['labeled_user_messages'] == 0
    row['question_id'] = case['question_id'] = 'fixture'
    row['haystack_sessions'][0][0]['has_answer'] = False
    assert AUDIT.audit([row], [case])[0]['labeled_user_messages'] == 0

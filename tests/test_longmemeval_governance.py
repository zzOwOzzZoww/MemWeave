"""Keep source-only replay separate from gold-aware evidence scoring."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/evaluate_longmemeval_governance.py'
SPEC = importlib.util.spec_from_file_location('evaluate_longmemeval_governance', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row():
    return {'question_id': 'fixture', 'question_type': 'single-session-user',
            'question': 'Unused question', 'answer': 'Unused gold',
            'answer_session_ids': ['answer-private'], 'haystack_session_ids': ['answer-private'],
            'haystack_dates': ['2023/05/20 (Sat) 03:00'],
            'haystack_sessions': [[{'role': 'user', 'content': 'My project uses SQLite.', 'has_answer': True},
                                  {'role': 'assistant', 'content': 'Understood.'}]]}


def test_gold_cannot_change_proposals_or_history_inputs():
    source = row()
    before = MODULE.history_inputs(source)
    source.update(question_id='other', question='Different', answer='SECRET_GOLD',
                  answer_session_ids=['different'], haystack_session_ids=['different'])
    source['haystack_sessions'][0][0]['has_answer'] = False
    assert MODULE.history_inputs(source) == before
    assert 'SECRET_GOLD' not in str(before) and 'answer-private' not in str(before)


def test_full_turn_is_not_truncated_or_gold_selected():
    text = 'A' * 8001
    assert MODULE.fixed_proposal(text) is None
    assert MODULE.fixed_proposal('A' * 8000)['source_quotes'] == ['A' * 8000]


def test_real_engine_filters_and_promotes_without_external_model(tmp_path):
    texts = ['Currently I am testing a new editor.',
             'Always use concise explanations in all my projects.',
             'My project uses SQLite.']
    inputs = [{'session_index': 0, 'turn_index': i, 'user': text, 'assistant': 'Understood.'}
              for i, text in enumerate(texts)]
    outputs, timings, states = MODULE.replay_history(inputs, tmp_path / 'audit.db')
    assert [o['outcome'] for o in outputs] == ['filtered', 'active', 'candidate']
    assert outputs[0]['reasons'] == {'temporary_knowledge': 1}
    assert states == {'active': 1, 'candidate': 1} and len(timings) == 3
    assert all('_run' not in o for o in outputs)


@pytest.mark.parametrize('outcome,retained,active,filtered', [
    ('candidate', 1, 0, 0), ('active', 1, 1, 0), ('filtered', 0, 0, 1),
    ('adapter_oversize', 0, 0, 0),
])
def test_candidate_retention_is_not_active_or_correctness(outcome, retained, active, filtered):
    scored = MODULE.score_evidence(row(), [{'session_index': 0, 'turn_index': 0, 'outcome': outcome}])
    assert scored['retained'] == retained and scored['active'] == active
    assert scored['filtered'] == filtered and scored['user_evidence'] == 1


def test_unmarked_messages_are_not_negative_labels():
    source = row()
    source['haystack_sessions'][0][0]['has_answer'] = False
    source['haystack_sessions'][0][1]['has_answer'] = True
    result = MODULE.score_evidence(source, [])
    assert not result['applicable'] and result['assistant_evidence'] == 1
    source['question_id'] += '_abs'
    assert MODULE.score_evidence(source, [])['reason'] == 'abstention'


def test_missing_evidence_turn_fails_instead_of_shrinking_denominator():
    with pytest.raises(ValueError, match='disappeared'):
        MODULE.score_evidence(row(), [])


def test_short_or_repeated_inputs_are_not_silently_dropped(tmp_path):
    texts = ['Hi', 'My project uses SQLite.', 'My project uses SQLite.']
    inputs = [{'session_index': 0, 'turn_index': i, 'user': text, 'assistant': ''}
              for i, text in enumerate(texts)]
    outputs, timings, states = MODULE.replay_history(inputs, tmp_path / 'audit.db')
    assert len(outputs) == len(timings) == 3
    assert outputs[0]['outcome'] == 'filtered'
    assert outputs[1]['outcome'] == outputs[2]['outcome'] == 'candidate'
    assert states == {'candidate': 1}


@pytest.mark.parametrize('text,coverage,retained', [
    ('  My project uses SQLite.\n', 'full', 'My project uses SQLite.'),
    ('  My project uses SQLite. Can you explain WAL?\n', 'partial', 'My project uses SQLite.'),
    ('Can you explain WAL? My editor is Vim.', 'partial', 'My editor is Vim.'),
])
def test_coverage_matches_persisted_content_and_raw_offsets(tmp_path, text, coverage, retained):
    database = tmp_path / 'coverage.db'
    outputs, _, states = MODULE.replay_history([
        {'session_index': 0, 'turn_index': 0, 'user': text, 'assistant': ''}], database)
    output = outputs[0]
    assert output['outcome'] == 'candidate' and states == {'candidate': 1}
    assert output['retention_coverage'] == coverage
    assert output['retained_characters'] == len(retained)
    assert [text[a:b] for a, b in output['retained_spans']] == [retained]
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT content FROM knowledge_records').fetchone()[0] == retained


@pytest.mark.parametrize('coverage,full,partial', [('full', 1, 0), ('partial', 0, 1)])
def test_labeled_message_presence_is_not_full_evidence_recovery(coverage, full, partial):
    # Even if the fragment contains none of the actual answer, it is only
    # partial source presence. The scorer cannot invent answer-span labels.
    source = row()
    source['haystack_sessions'][0][0]['content'] = 'My editor is Vim. Can you explain WAL?'
    scored = MODULE.score_evidence(source, [{'session_index': 0, 'turn_index': 0,
        'outcome': 'candidate', 'retention_coverage': coverage}])
    assert scored['retained'] == 1 and scored['active'] == 0
    assert scored['full_retained'] == full and scored['partial_retained'] == partial
    summary = MODULE.evidence_summary([{'evidence': scored}])
    assert summary['labeled_user_evidence_turns'] == 1
    assert summary['full_source_retained_including_candidates'] == full
    assert summary['partial_source_retained_including_candidates'] == partial


def test_multiple_retained_spans_are_verbatim_in_source_order():
    source = '  My project uses SQLite. Can you explain WAL? My editor is Vim. '
    fragments = ['My project uses SQLite.', 'My editor is Vim.']
    spans = MODULE.retained_source_spans(source, '\n'.join(fragments))
    assert [source[a:b] for a, b in spans] == fragments
    assert spans[0][1] < spans[1][0]


@pytest.mark.parametrize('content', ['', 'Unrelated text.', 'My editor is Vim.\nMy project uses SQLite.'])
def test_invalid_persisted_coverage_fails_instead_of_inventing_spans(content):
    with pytest.raises(ValueError, match='verbatim source fragment'):
        MODULE.retained_source_spans('My project uses SQLite. My editor is Vim.', content)


def test_filtered_messages_have_no_retained_spans(tmp_path):
    outputs, _, _ = MODULE.replay_history([{'session_index': 0, 'turn_index': 0,
        'user': 'Maybe my project uses SQLite. Can you help?', 'assistant': ''}], tmp_path / 'none.db')
    assert outputs[0]['outcome'] == 'filtered'
    assert outputs[0]['retention_coverage'] == 'none'
    assert outputs[0]['retained_characters'] == 0 and outputs[0]['retained_spans'] == []

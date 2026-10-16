"""Quality regressions use technical fixtures, never gold-guided extraction."""
import pytest

from test_tiered_admission import learn, proposal


@pytest.mark.parametrize('fact', [
    'My service uses SQLite, which is embedded in the application.',
    'My service uses SQLite, which has a WAL journal.',
    'My service uses SQLite, which I configured in settings.json.',
    'My service uses SQLite, where the project stores its index.',
])
def test_relative_clause_stays_with_its_fact(tmp_path, fact):
    _, result, rows = learn(tmp_path, fact + ' Can you explain indexing?', [proposal(fact + ' Can you explain indexing?')])
    assert result['promoted'] == 0 and rows[0]['content'] == fact


@pytest.mark.parametrize('question', ['have you used it?', 'do you recommend it?', 'can you explain it?',
                                     'how do you configure it?', 'what is its journal mode?'])
def test_explicit_comma_question_can_be_removed_without_losing_fact(tmp_path, question):
    fact = 'My service uses SQLite'
    text = fact + ', ' + question
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 0 and rows[0]['content'] == fact


@pytest.mark.parametrize('prefix', ['', 'Also, by the way, ', 'Oh, and by the way, ', 'Actually, '])
@pytest.mark.parametrize('plan', [
    'I was thinking of migrating to Redis.',
    "I'm also thinking of migrating to Redis.",
    "I've been thinking of migrating to Redis.",
    "I'm looking for a new cache library.",
    'I am still considering a new cache library.',
    'We were trying to find a cache library.',
    'I think Redis is the right choice.',
])
def test_plans_and_help_requests_do_not_fill_the_candidate_budget(tmp_path, prefix, plan):
    fact = 'My service uses SQLite.'
    text = prefix + plan + ' ' + fact + ' Can you explain WAL?'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 0 and rows[0]['content'] == fact


@pytest.mark.parametrize('fragment', ["It's been performing well.", 'This is helpful.',
                                       'That has improved performance.', 'Those are useful.'])
def test_orphaned_references_are_not_standalone_knowledge(tmp_path, fragment):
    text = fragment + ' Can you explain WAL?'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert not rows and result['proposals'] == 0


def test_discourse_markers_do_not_hide_an_explicit_completed_observation(tmp_path):
    fact = 'Oh, and by the way, I built the SQLite index in the service.'
    text = 'Can you explain WAL? ' + fact
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert rows[0]['content'] == fact and result['promoted'] == 0


@pytest.mark.parametrize('qualifier', ['Maybe, ', 'Today, '])
def test_relative_clause_fix_does_not_strip_leading_qualifications(tmp_path, qualifier):
    text = qualifier + 'my service uses SQLite, which has a WAL journal. Can you explain it?'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert not rows and result['proposals'] == 0


def test_vague_remember_narrative_is_not_an_independent_fact(tmp_path):
    text = 'I remember yesterday; from now on I might try a different editor.'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}

"""Atomic evidence must not borrow a sibling's intent or discard a qualifier."""
import io
import json

import pytest

from agent_knowledge_bridge.admission import EvidenceContext, quote_context
from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.learning_engine import DeepSeekReviewer, LearningEngine
from test_tiered_admission import learn, proposal


@pytest.mark.parametrize('prefix,fact,separator', [
    ("I'm considering a cache migration", 'our service uses SQLite.', ', '),
    ('I am planning to replace the editor', 'my service uses SQLite.', ', '),
    ('I was thinking of changing the formatter', 'our deployment uses containers.', ', and '),
    ('We are planning to replace the editor', 'the service does not use Redis.', ' but '),
    ('我打算更换编辑器', '我们的服务采用 SQLite。', '，'),
    ('我们正在考虑更换构建工具', '我们的构建系统使用 Python。', '，但是'),
])
@pytest.mark.parametrize('whole_proposal', [False, True])
def test_independent_asserted_clause_survives_neighboring_intent(tmp_path, prefix, fact, separator, whole_proposal):
    source = prefix + separator + fact + ' Can you explain the tradeoffs?'
    quote = source if whole_proposal else fact
    _, result, rows = learn(tmp_path, source, [proposal(quote, kind='fact')])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['content'] == fact and rows[0]['status'] == 'candidate'
    assert rows[0]['scope'] == 'project'


@pytest.mark.parametrize('fact,intent,separator', [
    ('My service uses SQLite', 'I am planning to replace the editor.', ', '),
    ('Our service does not use Redis', 'we are considering another cache.', ' and '),
    ('我们的服务采用 SQLite', '我打算更换编辑器。', '，'),
])
def test_whole_mixed_declaration_keeps_fact_not_plan_without_needing_a_question(tmp_path, fact, intent, separator):
    source = fact + separator + intent
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert rows[0]['content'] == fact and result['promoted'] == 0


@pytest.mark.parametrize('fact', [
    'My service uses SQLite and PostgreSQL for its reporting index.',
    'I built the index and deployed it with a bounded rollback procedure.',
    'My service uses SQLite, with a local WAL journal, and we keep daily backups.',
    "I've configured my service, Atlas, for nine months, and I've been considering a migration.",
    'I attended a deployment review and was impressed by the validated rollback evidence.',
])
def test_enumerations_and_shared_subject_predicates_keep_complete_evidence(tmp_path, fact):
    source = fact + ' Can you explain the tradeoffs?'
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert result['promoted'] == 0
    assert 'PostgreSQL' not in fact or 'PostgreSQL' in rows[0]['content']
    if fact.startswith('My service uses SQLite, with'):
        # Two real subjects may split; the prepositional qualifier must remain.
        assert 'with a local WAL journal' in rows[0]['content']
    elif 'considering' in fact:
        assert "I've configured my service, Atlas, for nine months" == rows[0]['content']
    else:
        assert rows[0]['content'] == fact


@pytest.mark.parametrize('fact', [
    'I finally completed the verified SQLite migration last month.',
    "As for deployment, I've recently completed the validated rollback.",
    'Our service uses SQLite, which stores the local index.',
])
def test_new_same_sentence_rule_does_not_normalize_previously_durable_multi_sentence_turns(tmp_path, fact):
    source = 'I am planning to replace the editor. ' + fact
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert rows[0]['content'] == source and result['promoted'] == 0


@pytest.mark.parametrize('epistemic', ['think', 'suppose', 'guess', 'wonder'])
def test_epistemic_sibling_cannot_trigger_new_durable_normalization(tmp_path, epistemic):
    source = ("I'm still deciding on an editor, but I " + epistemic +
              " I'll adopt another formatter. By the way, I finally completed the verified SQLite migration.")
    index = EvidenceContext(source)
    assert not index.mixed_intent([source])
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert rows[0]['content'] == source and result['promoted'] == 0


@pytest.mark.parametrize('epistemic', ['think', 'suppose', 'guess', 'wonder'])
def test_epistemic_context_cannot_be_clipped_on_either_side(tmp_path, epistemic):
    fact = 'our service uses SQLite.'
    source = 'I ' + epistemic + ' the migration succeeded, ' + fact
    _, result, rows = learn(tmp_path, source, [proposal(fact, kind='fact')])
    assert not rows and result['promoted'] == 0


@pytest.mark.parametrize('fragment', [
    'a Redis cluster that was not verified.',
    'metrics and traces that were collected from tests.',
    'which stores the index in SQLite.',
])
def test_bare_list_items_never_become_facts_from_a_plan(tmp_path, fragment):
    source = "I'm considering an editor and " + fragment + ' Can you explain it?'
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert not rows and result['promoted'] == 0


@pytest.mark.parametrize('source,quote,reason', [
    ('Maybe, my service uses SQLite.', 'my service uses SQLite.', 'uncertain_knowledge'),
    ('Maybe my editor uses Vim, my service uses SQLite.', 'my service uses SQLite.', 'uncertain_knowledge'),
    ('Today, my service uses SQLite.', 'my service uses SQLite.', 'temporary_knowledge'),
    ('Only for today, our service uses SQLite.', 'our service uses SQLite.', 'temporary_knowledge'),
    ('If the deployment succeeds, my service uses SQLite. Can you help?',
     'my service uses SQLite.', 'uncertain_knowledge'),
    ('I am not sure, my service uses SQLite.', 'my service uses SQLite.', 'uncertain_knowledge'),
    ('I think the configuration is correct, my service uses SQLite.',
     'my service uses SQLite.', 'uncertain_knowledge'),
    ('I am planning a migration (my service uses SQLite). Can you help?',
     'my service uses SQLite', 'uncertain_knowledge'),
    ('我的项目可能采用 Redis，我们的服务使用 SQLite。', '我们的服务使用 SQLite。', 'uncertain_knowledge'),
    ('今天我打算更换编辑器，我们的服务使用 SQLite。', '我们的服务使用 SQLite。', 'temporary_knowledge'),
])
def test_clipped_quote_cannot_erase_parent_qualifier(tmp_path, source, quote, reason):
    _, result, rows = learn(tmp_path, source, [proposal(quote, kind='fact')])
    assert not rows and result['rejection_reasons'] == {reason: 1}


@pytest.mark.parametrize('suffix', [
    ', but not in production.', ', only for today.', ', maybe only in tests.',
    ', unless the service is deployed.', ', which is not verified yet.',
])
def test_dependent_negation_temporality_and_condition_stay_in_context(suffix):
    fact = 'My service uses SQLite'
    source = fact + suffix
    assert quote_context(source, [fact]) == source


@pytest.mark.parametrize('source,quote', [
    ('My service uses SQLite, but not in production.', 'My service uses SQLite'),
    ('My service uses SQLite only in tests.', 'My service uses SQLite'),
    ('My service does not use Redis.', 'use Redis.'),
    ('我们的服务使用 SQLite，但不用于生产环境。', '我们的服务使用 SQLite'),
    ('我们的服务仅在测试环境使用 SQLite。', '使用 SQLite。'),
])
def test_clipped_negation_or_limit_never_becomes_unqualified_knowledge(tmp_path, source, quote):
    _, result, rows = learn(tmp_path, source, [proposal(quote, kind='fact')])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


@pytest.mark.parametrize('source', [
    'My service uses SQLite, but not in production.',
    'My service uses SQLite only in tests.',
    'My service does not use Redis.',
    '我们的服务使用 SQLite，但不用于生产环境。',
    '我们的服务仅在测试环境使用 SQLite。',
])
def test_complete_negation_and_limit_are_persisted_verbatim(tmp_path, source):
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert rows[0]['content'] == source and rows[0]['status'] == 'candidate'
    assert result['promoted'] == 0


@pytest.mark.parametrize('source', [
    "I'm thinking of using my new workstation. Can you help?",
    'I am planning to buy a workstation. Can you help?',
    '我打算买一台新的工作站。你能推荐一下吗？',
])
def test_intent_does_not_imply_an_owned_or_existing_resource(tmp_path, source):
    _, result, rows = learn(tmp_path, source, [proposal(source, kind='fact')])
    assert not rows and result['promoted'] == 0


def test_relative_clause_is_not_reinterpreted_as_independent_from_a_plan(tmp_path):
    source = "I'm considering a migration, which uses SQLite. Can you help?"
    _, result, rows = learn(tmp_path, source, [proposal('which uses SQLite.', kind='fact')])
    assert not rows and result['promoted'] == 0


@pytest.mark.parametrize('wrapper', ['"{}"', '`{}`', '“{}”', '```text\n{}\n```'])
def test_quoted_example_cannot_grant_activation_authority(tmp_path, wrapper):
    fact = 'my service uses SQLite.'
    source = wrapper.format('I am considering a migration, ' + fact) + ' Can you help?'
    _, result, rows = learn(tmp_path, source, [proposal(fact, kind='fact')])
    assert result['promoted'] == 0 and all(row['status'] == 'candidate' for row in rows)


@pytest.mark.parametrize('category', ['temporary', 'uncertain'])
def test_model_cannot_override_its_own_uncertainty_using_atomic_quotes(tmp_path, category):
    source = 'I am planning a migration, my service uses SQLite.'
    _, result, rows = learn(tmp_path, source,
        [proposal('my service uses SQLite.', kind='fact', retention=category)])
    assert not rows and result['rejection_reasons'] == {category + '_knowledge': 1}


def test_source_index_handles_repeated_quotes_conservatively():
    fact = 'My service uses SQLite.'
    source = 'Maybe. ' + fact + ' ' + fact
    index = EvidenceContext(source)
    assert 'Maybe.' in index.context([fact])
    assert index.context([fact]) == quote_context(source, [fact])


@pytest.mark.parametrize('source,fact', [
    ("My service uses SQLite, but I'm not sure if that's correct.", 'My service uses SQLite'),
    ('My service uses SQLite and I am not sure, our editor uses Vim.', 'My service uses SQLite'),
    ('我们的服务采用 SQLite，而且这只是临时的。', '我们的服务采用 SQLite'),
])
def test_coordination_never_detaches_following_qualifier(tmp_path, source, fact):
    assert quote_context(source, [fact]) == source
    _, result, rows = learn(tmp_path, source, [proposal(fact, kind='fact')])
    assert not rows and result['promoted'] == 0


def test_separate_proposals_use_one_actual_reviewer_call_and_keep_scopes(tmp_path, monkeypatch):
    fact = 'our service uses SQLite.'
    rule = 'Always write concise explanations in all my projects.'
    source = "I'm considering a cache migration, " + fact + ' ' + rule
    calls = []
    def response(request, **kwargs):
        calls.append(json.loads(request.data))
        proposals = [proposal(fact, kind='fact'), proposal(rule)]
        payload = {'choices': [{'message': {'content': json.dumps({'proposals': proposals})}}]}
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr('agent_knowledge_bridge.provider.open_request', response)
    reviewer = DeepSeekReviewer.__new__(DeepSeekReviewer)
    reviewer.api_key, reviewer.base_url, reviewer.model, reviewer.timeout = 'fixture', 'http://invalid', 'fixture', 1
    engine = LearningEngine(database_path=tmp_path / 'review.db', agent_id='codex', project_key='p',
        reviewer=reviewer, transcript_parser=lambda *a, **k: TranscriptTurn(source, '', ()))
    result = engine.learn({'session_id': 'fixture'})
    assert len(calls) == 1 and result['proposals'] == 2 and result['promoted'] == 0
    assert 'separate proposals with exact source_quotes' in calls[0]['messages'][0]['content']
    with engine.store.knowledge._connect() as db:
        records = {r['content']: dict(r) for r in db.execute('SELECT * FROM knowledge_records')}
    assert records[fact]['scope'] == 'project' and records[rule]['scope'] == 'user'
    assert all(r['status'] == 'candidate' for r in records.values())

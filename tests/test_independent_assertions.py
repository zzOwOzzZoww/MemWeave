"""Mixed evidence can retain an assertion, never launder its qualifications."""
import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from test_tiered_admission import learn, proposal


@pytest.mark.parametrize('fact,other,join', [
    ('My project uses SQLite.', 'Can you explain WAL?', ' '),
    ('我的项目采用 SQLite。', '你能解释 WAL 吗？', ''),
    ('My project uses SQLite.', 'I am not sure how to benchmark the service.', ' '),
    ('My project uses SQLite.', 'Maybe the editor is broken.', ' '),
    ('My project uses SQLite.', 'Currently I am testing an editor.', ' '),
    ('My commute is thirty minutes.', 'I am not sure which podcast to choose.', ' '),
    ('My project does not use Redis.', 'Can you explain SQLite?', ' '),
    ("I don't use Redis.", 'Can you explain SQLite?', ' '),
    ('我的项目不使用 Redis。', '你觉得缓存怎么选？', ''),
])
@pytest.mark.parametrize('order', ['fact_first', 'fact_last'])
def test_mixed_quote_retains_only_independent_assertion(tmp_path, fact, other, join, order):
    text = fact + join + other if order == 'fact_first' else other + join + fact
    engine, result, rows = learn(tmp_path, text, [proposal(text, kind='fact')])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['content'] == fact and rows[0]['scope'] == 'project'
    assert rows[0]['status'] == 'candidate'
    assert engine.store.knowledge.search(requester_agent='codex', project_key='admission',
        query='SQLite Redis commute', limit=3)['count'] == 0


@pytest.mark.parametrize('fact,tail', [
    ('My project uses SQLite', 'can you explain WAL?'),
    ('My project uses SQLite', "but I'm not sure how to benchmark the service."),
    ('My commute is thirty minutes', 'but I am not sure which podcast to choose.'),
    ('我的项目使用 SQLite', '你能解释 WAL 吗？'),
])
@pytest.mark.parametrize('clipped', [False, True])
def test_explicit_independent_question_clause_does_not_poison_fact(tmp_path, fact, tail, clipped):
    text = fact + ', ' + tail
    _, result, rows = learn(tmp_path, text, [proposal(fact if clipped else text, kind='fact')])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['content'] == fact


@pytest.mark.parametrize('text,reason', [
    ('Maybe my project uses SQLite. Can you explain WAL?', 'uncertain_knowledge'),
    ('Today, my project uses SQLite. Can you explain WAL?', 'uncertain_knowledge'),
    ('My project uses SQLite, maybe only for this test. Can you explain WAL?', 'uncertain_knowledge'),
    ('My project uses SQLite. This is temporary. Can you explain WAL?', 'uncertain_knowledge'),
    ('My project uses SQLite. This is only for today. Can you explain WAL?', 'uncertain_knowledge'),
    ('My project uses SQLite. This is not verified. Can you explain WAL?', 'uncertain_knowledge'),
    ("My project uses SQLite, but I'm not sure if that's correct.", 'uncertain_knowledge'),
    ('My project uses SQLite. I am not sure. Can you explain WAL?', 'uncertain_knowledge'),
    ('Maybe. My project uses SQLite. Can you explain WAL?', 'uncertain_knowledge'),
    ('我的项目使用 SQLite。这只是猜测。你能解释 WAL 吗？', 'uncertain_knowledge'),
    ('以后默认使用简体中文可以吗', 'uncertain_knowledge'),
    ('Can you explain whether SQLite is appropriate?', 'uncertain_knowledge'),
    ('Please tell me how SQLite is implemented. Can you help?', 'uncertain_knowledge'),
    ('I want to use SQLite. Can you help?', 'uncertain_knowledge'),
    ('I am planning to use SQLite. Can you help?', 'uncertain_knowledge'),
    ('Thanks for your assistance. Can you explain WAL?', 'uncertain_knowledge'),
    ('"My project uses SQLite." Can you explain WAL?', 'uncertain_knowledge'),
    ('```text\nMy project uses SQLite.\n```\nCan you explain WAL?', 'uncertain_knowledge'),
    ('不要保存。My project uses SQLite. Can you explain WAL?', 'memory_storage_opt_out'),
    ('My password is fixture-only-password. My project uses SQLite. Can you help?', 'sensitive_content'),
])
def test_selection_does_not_drop_restrictions_or_create_facts_from_requests(tmp_path, text, reason):
    _, result, rows = learn(tmp_path, text, [proposal(text, kind='fact')])
    assert not rows and result['proposals'] == 0
    assert result['rejection_reasons'] == {reason: 1}


@pytest.mark.parametrize('category', ['uncertain', 'temporary'])
def test_model_retention_cannot_be_overridden_by_selection(tmp_path, category):
    text = 'My project uses SQLite. Can you explain WAL?'
    _, result, rows = learn(tmp_path, text, [proposal(text, retention=category)])
    # The explicit question takes precedence over temporary in the existing
    # reason classifier; neither model label may be overridden by selection.
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_selection_cannot_borrow_an_assertion_outside_the_models_quote(tmp_path):
    fact = 'My project uses SQLite.'
    question = 'Can you explain WAL?'
    _, result, rows = learn(tmp_path, fact + ' ' + question, [proposal(question)])
    assert not rows and result['proposals'] == 0


def test_selection_has_a_bounded_source_order_and_cleans_discarded_metadata(tmp_path):
    facts = ['My project uses SQLite.', 'My editor is Vim.', 'My shell is Bash.', 'My OS is Linux.']
    text = ' '.join(facts) + ' Can you explain the editor crash?'
    engine, result, rows = learn(tmp_path, text, [proposal(text, kind='fact',
        title='Unproven editor crash cause', search_terms='unproven cause', subject_terms=['SQLite', 'crash'])])
    row = rows[0]
    assert result['promoted'] == 0 and row['content'] == '\n'.join(facts[:3])
    assert row['title'] == ' '.join(facts[:3]) and 'crash' not in row['content']
    assert not row['search_terms']
    with engine.store.knowledge._connect() as db:
        evidence = [r[0] for r in db.execute('SELECT summary FROM knowledge_evidence')]
    assert any('independent_source_assertions' in entry for entry in evidence)


def test_split_record_never_borrows_global_scope_or_directive_approval(tmp_path):
    text = 'Always use SQLite in all my projects. My project uses Python. Can you explain WAL?'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['content'] == 'My project uses Python.'
    assert rows[0]['scope'] == 'project' and rows[0]['status'] == 'candidate'


@pytest.mark.parametrize('agent', ['codex', 'claude-code', 'gemini-cli', 'custom-agent'])
def test_runtime_api_uses_the_same_assertion_fallback(tmp_path, agent):
    text = 'My project uses SQLite. Can you explain WAL?'
    app = create_app(database_path=tmp_path / 'api.db', api_token='fixture',
        reviewer=lambda _: {'proposals': [proposal(text, kind='fact')]})
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer fixture'}
        response = client.post('/v1/learning/turn', headers=headers, json={
            'agent_id': agent, 'project_key': 'p', 'session_id': 's',
            'turn': {'user_text': text, 'assistant_text': 'Acknowledged.', 'tools': []}})
        assert response.status_code == 200 and response.json()['promoted'] == 0
        listing = client.post('/v1/knowledge/list', headers=headers, json={
            'agent_id': agent, 'project_key': 'p', 'status': 'all'})
        row = listing.json()['results'][0]
        assert row['content'] == 'My project uses SQLite.' and row['status'] == 'candidate'


def test_passing_tool_cannot_activate_a_normalized_fact(tmp_path):
    text = 'My project uses SQLite. Can you explain WAL?'
    _, result, rows = learn(tmp_path, text, [proposal(text, kind='fact')],
        tools=(ToolEvent('test', 'shell', 'pytest fixture', 'PASS', True, 'test'),))
    assert result['promoted'] == 0 and rows[0]['status'] == 'candidate'


def test_legacy_unquoted_proposal_has_no_assertion_fallback(tmp_path):
    text = 'My project uses SQLite. Can you explain WAL?'
    item = proposal(text, content=text)
    item.pop('source_quotes')
    _, result, rows = learn(tmp_path, text, [item])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_selection_restores_source_order_of_multiple_quotes(tmp_path):
    from agent_knowledge_bridge.admission import independent_assertions
    first, second = 'My project uses SQLite.', 'My editor is Vim.'
    source = first + ' Can you explain WAL? ' + second + ' Can you explain Vim?'
    quotes = [second + ' Can you explain Vim?', first + ' Can you explain WAL?']
    assert independent_assertions(proposal(source), source, quotes) == [first, second]

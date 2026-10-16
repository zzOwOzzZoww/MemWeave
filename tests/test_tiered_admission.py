"""Admission is independent of interests, source Agent and model confidence."""
import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.admission import AdmissionRejected
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from agent_knowledge_bridge.decisions import grounded_user_proposal
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.learning_engine import LearningEngine


def proposal(text, *, kind='preference', **extra):
    return {'title': 'Fixture preference', 'content': 'Do not trust this summary.',
            'knowledge_type': kind, 'scope': 'user', 'source_role': 'user',
            'source_quotes': [text], 'retention': 'durable', **extra}


def learn(tmp_path, user, proposals, *, agent='codex', assistant='Acknowledged.', tools=()):
    engine = LearningEngine(database_path=tmp_path / 'admission.db', agent_id=agent,
        project_key='admission', transcript_parser=lambda *a, **kw: TranscriptTurn(user, assistant, tools),
        reviewer=lambda _: {'proposals': proposals})
    result = engine.learn({'session_id': 's'})
    with engine.store.knowledge._connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM knowledge_records')]
    return engine, result, rows


@pytest.mark.parametrize('agent', ['codex', 'claude-code', 'gemini-cli', 'custom-agent'])
@pytest.mark.parametrize('user', [
    '以后默认使用简体中文解释技术问题。',
    '今后所有项目默认使用浅色主题。',
    '以后默认采用低风险的投资方案。',
    'Always use concise explanations in all my projects.',
])
def test_stable_user_directives_can_activate_without_remember(tmp_path, user, agent):
    engine, result, rows = learn(tmp_path, user, [proposal(user)], agent=agent)
    assert result['proposals'] == result['promoted'] == 1
    assert rows[0]['status'] == 'active' and rows[0]['content'] == user
    record = engine.store.knowledge.get(requester_agent=agent, knowledge_id=rows[0]['id'])
    assert any(e['evidence_kind'] == 'user_approval' for e in record['evidence'])


@pytest.mark.parametrize('user,reason', [
    ('今天我正在准备实习面试。', 'temporary_knowledge'),
    ('最近我在处理家庭事务。', 'temporary_knowledge'),
    ('暂时使用深色主题完成这个页面。', 'temporary_knowledge'),
    ('请记住本轮默认使用深色主题。', 'temporary_knowledge'),
    ('本次只执行代码检查，不运行测试。', 'temporary_knowledge'),
    ('Currently I am testing a new editor.', 'temporary_knowledge'),
    ('我猜这个问题可能是缓存导致的。', 'uncertain_knowledge'),
    ('The error might be caused by the cache.', 'uncertain_knowledge'),
    ('你觉得是否应该默认使用浅色主题？请记住。', 'uncertain_knowledge'),
    ('以后默认使用简体中文解释技术问题可以吗', 'uncertain_knowledge'),
    ('我比较喜欢浅色主题，这个还需要讨论。', 'uncertain_knowledge'),
])
def test_ephemeral_or_uncertain_evidence_does_not_even_create_candidate(tmp_path, user, reason):
    engine, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['proposals'] == result['promoted'] == 0 and not rows
    assert result['rejection_reasons'] == {reason: 1}
    latest = engine.store.knowledge.latest_learning('codex')
    assert latest['rejection_reasons'] == {reason: 1}


@pytest.mark.parametrize('user,category,warning', [
    ('请长期记录：我猜这个问题可能是缓存导致的。', 'uncertain', '尚未确认，不得当作事实'),
    ('请持续跟踪：今天我正在准备实习面试。', 'temporary', '不代表当前状态'),
    ('请长期保留这个方案，尚未决定是否采用。', 'uncertain', '尚未确认，不得当作事实'),
    ('Please track long-term: the error might be caused by the cache.', 'uncertain', '尚未确认'),
    ('Please track long-term: this is just a guess.', 'uncertain', '尚未确认'),
    ('请帮我持续跟踪：今天我正在准备实习面试。', 'temporary', '不代表当前状态'),
    ('请持续关注：今天我正在准备实习面试。', 'temporary', '不代表当前状态'),
])
def test_explicit_retention_is_candidate_only_and_preserves_qualifiers(tmp_path, user, category, warning):
    engine, result, rows = learn(tmp_path, user, [proposal(user, retention=category)])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['status'] == 'candidate'
    assert warning in rows[0]['content'] and user in rows[0]['content']
    assert engine.store.knowledge.search(requester_agent='codex', project_key='admission',
        query='缓存 实习 方案 error', limit=3)['count'] == 0


@pytest.mark.parametrize('category', ['temporary', 'uncertain'])
def test_model_retention_flag_can_block_but_never_authorize(tmp_path, category):
    user = '我的项目主要使用 Python 编写。'
    _, result, rows = learn(tmp_path, user, [proposal(user, retention=category, confidence=1.0)])
    assert not rows and result['rejection_reasons'] == {f'{category}_knowledge': 1}


def test_durable_model_label_and_passing_tool_are_not_proof_of_user_fact(tmp_path):
    user = '我的项目主要使用 Python 编写。'
    _, result, rows = learn(tmp_path, user, [proposal(user, kind='fact', confidence=1.0)],
        tools=(ToolEvent('test', 'shell', 'pytest unrelated', 'PASS', True, 'test'),))
    assert result['promoted'] == 0 and rows[0]['status'] == 'candidate'


def test_filtering_is_per_proposal_not_per_turn(tmp_path):
    temporary = '今天我正在排查一个网络故障。'
    stable = '以后默认使用简体中文解释技术问题。'
    _, result, rows = learn(tmp_path, temporary + '\n' + stable,
        [proposal(temporary), proposal(stable)])
    assert result['proposals'] == 1 and result['rejection_reasons'] == {'temporary_knowledge': 1}
    # A clipped statement may be retained, but cannot authorize automatic use.
    assert len(rows) == 1 and rows[0]['content'] == stable and rows[0]['status'] == 'candidate'


def test_unresolved_topic_does_not_block_an_independent_rule(tmp_path):
    uncertain = '数据库方案尚未决定，还需要讨论。'
    stable = '以后默认使用简体中文解释技术问题。'
    _, result, rows = learn(tmp_path, uncertain + '\n' + stable,
        [proposal(uncertain), proposal(stable)])
    assert result['proposals'] == 1 and result['rejection_reasons'] == {'uncertain_knowledge': 1}
    assert len(rows) == 1 and rows[0]['content'] == stable


def test_reviewer_cannot_clip_temporality_or_uncertainty_out_of_quote(tmp_path):
    user = '今天我的编辑器使用深色主题。\n我猜数据库选择 SQLite 更合适。'
    _, result, rows = learn(tmp_path, user, [proposal('我的编辑器使用深色主题。'),
        proposal('数据库选择 SQLite 更合适。')])
    assert not rows and result['rejection_reasons'] == {'temporary_knowledge': 1, 'uncertain_knowledge': 1}


def test_unrelated_remember_request_does_not_authorize_uncertain_proposal(tmp_path):
    uncertain = '我猜这个故障可能是缓存导致的。'
    user = '请记住今后默认使用简体中文。\n' + uncertain
    _, result, rows = learn(tmp_path, user, [proposal(uncertain)])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_clipped_quote_cannot_borrow_authorization_from_other_clause(tmp_path):
    uncertain = '我猜这个故障可能是缓存导致的。'
    user = '请记住今后默认使用简体中文，' + uncertain
    _, result, rows = learn(tmp_path, user, [proposal(uncertain)])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_uncertain_legacy_experience_cannot_use_test_pass_as_truth(tmp_path):
    from test_experiences import reviewer, tool

    def speculative(text):
        response = reviewer(text)
        response['proposals'][0]['experience']['reason'] = 'Maybe unordered fields caused the widget failure.'
        return response

    engine = LearningEngine(database_path=tmp_path / 'experience.db', agent_id='codex', project_key='p',
        transcript_parser=lambda *a, **k: TranscriptTurn('Build widget for Windows, not Linux',
            'Corrected field order.', (tool(False), tool(True, 1))), reviewer=speculative)
    result = engine.learn({'session_id': 'source'})
    assert result['proposals'] == 0 and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_explicit_storage_never_overrides_secrets_or_optout(tmp_path):
    for index, user in enumerate((
        '请长期记住密码 password=fixture-private-secret。',
        '请长期跟踪：今天我正在排查问题，但不要保存。',
        '我猜是缓存导致的，不要长期记录这个猜测。',
    )):
        case = tmp_path / str(index)
        case.mkdir()
        _, result, rows = learn(case, user, [proposal(user)])
        assert not rows and result['proposals'] == 0


def test_typed_experience_respects_user_storage_optout(tmp_path):
    from test_experiences import reviewer, tool

    engine = LearningEngine(database_path=tmp_path / 'optout.db', agent_id='codex', project_key='p',
        transcript_parser=lambda *a, **k: TranscriptTurn('Build widget for Windows, not Linux; do not save.',
            'Corrected field order.', (tool(False), tool(True, 1))), reviewer=reviewer)
    result = engine.learn({'session_id': 'source'})
    assert result['proposals'] == 0 and result['rejection_reasons'] == {'memory_storage_opt_out': 1}


def test_quoted_request_cannot_authorize_long_term_uncertain_storage():
    user = '示例：“请长期记住我猜缓存有问题”。'
    with pytest.raises(AdmissionRejected, match='uncertain_knowledge'):
        grounded_user_proposal(proposal(user, retention='uncertain'), TranscriptTurn(user, '', ()))


@pytest.mark.parametrize('user', [
    '以后默认先验证结果，不要把未经验证的推测当作结论。',
    '以后默认使用临时文件隔离测试数据。',
    '今后默认每周一执行数据备份。',
])
def test_rules_about_uncertainty_or_temporary_files_are_not_temporary_status(tmp_path, user):
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['promoted'] == 1 and rows[0]['status'] == 'active'


def test_whether_in_a_verification_rule_is_not_a_speculative_question(tmp_path):
    user = '以后默认检查是否通过测试再提交。'
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['proposals'] == 1 and rows[0]['content'] == user


def test_reported_advice_does_not_automatically_activate(tmp_path):
    user = '别人建议以后默认使用深色主题。'
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['promoted'] == 0 and rows[0]['status'] == 'candidate'


def test_related_accepted_rule_forces_manual_review(tmp_path):
    user = '今后所有项目默认使用浅色主题。'
    engine, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['promoted'] == 1
    revised = '今后所有项目默认使用深色主题。'
    engine.transcript_parser = lambda *a, **kw: TranscriptTurn(revised, '', ())
    engine.reviewer = lambda _: {'proposals': [proposal(revised)]}
    result = engine.learn({'session_id': 'next'})
    assert result['promoted'] == 0 and result['proposals'] == 1
    assert engine.store.knowledge.get(requester_agent='codex', knowledge_id=rows[0]['id'])['knowledge']['status'] == 'active'
    assert engine.store.knowledge.review_queue(requester_agent='codex', project_key='admission')['count'] == 1


@pytest.mark.parametrize('category', ['permanent', None, [], {}])
def test_invalid_model_classification_fails_closed(tmp_path, category):
    user = '今后默认使用简体中文解释技术问题。'
    _, result, rows = learn(tmp_path, user, [proposal(user, retention=category)])
    assert not rows and result['rejection_reasons'] == {'invalid_retention': 1}


def test_legacy_reviewer_cannot_bypass_ephemeral_gate_without_quotes(tmp_path):
    user = '今天我正在准备实习面试。'
    p = proposal(user, content=user)
    p.pop('source_quotes')
    _, result, rows = learn(tmp_path, user, [p])
    assert not rows and result['rejection_reasons'] == {'temporary_knowledge': 1}


def test_runtime_api_uses_same_core_gate(tmp_path):
    user = '今天我正在准备实习面试。'
    app = create_app(database_path=tmp_path / 'api.db', api_token='test-only',
        reviewer=lambda _: {'proposals': [proposal(user)]})
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer test-only'}
        payload = {'agent_id': 'custom-agent', 'project_key': 'admission', 'session_id': 's',
                   'turn': {'user_text': user, 'assistant_text': 'Acknowledged.', 'tools': []}}
        response = client.post('/v1/learning/turn', json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()['rejection_reasons'] == {'temporary_knowledge': 1}
        listing = client.post('/v1/knowledge/list', headers=headers,
            json={'agent_id': 'custom-agent', 'project_key': 'admission', 'status': 'all'})
        assert listing.json()['results'] == []


@pytest.mark.parametrize('user,quote', [
    ('My project uses SQLite. Can you explain WAL?', 'My project uses SQLite.'),
    ('Can you explain WAL? My project uses SQLite.', 'My project uses SQLite.'),
    ('My project uses SQLite. Currently I am testing a new editor.', 'My project uses SQLite.'),
    ('My project uses SQLite. Maybe the cache is broken.', 'My project uses SQLite.'),
    ('我的项目使用 SQLite。你能解释一下 WAL 吗？', '我的项目使用 SQLite。'),
    ('My project uses SQLite. Can you explain WAL?', 'My project uses SQLite'),
])
def test_independent_assertion_does_not_inherit_next_sentences_risk(tmp_path, user, quote):
    _, result, rows = learn(tmp_path, user, [proposal(quote, kind='fact')])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['status'] == 'candidate' and rows[0]['content'] == quote


@pytest.mark.parametrize('user,quote,reason', [
    ('Maybe my project uses SQLite.', 'my project uses SQLite.', 'uncertain_knowledge'),
    ('My project uses SQLite, maybe only for this test.', 'My project uses SQLite', 'uncertain_knowledge'),
    ('Currently my project uses SQLite.', 'my project uses SQLite.', 'temporary_knowledge'),
    ('Maybe e.g. SQLite is appropriate.', 'SQLite is appropriate.', 'uncertain_knowledge'),
    ('Maybe Dr. Smith uses SQLite.', 'Smith uses SQLite.', 'uncertain_knowledge'),
    ('Currently version 3.12 uses SQLite.', 'version 3.12 uses SQLite.', 'temporary_knowledge'),
    ('Maybe src/store.py uses SQLite.', 'src/store.py uses SQLite.', 'uncertain_knowledge'),
    ('My project uses SQLite. This is just a guess.', 'My project uses SQLite.', 'uncertain_knowledge'),
    ('My project uses SQLite. Only for today.', 'My project uses SQLite.', 'temporary_knowledge'),
    ('Maybe. My project uses SQLite.', 'My project uses SQLite.', 'uncertain_knowledge'),
    ('我的项目使用 SQLite。这只是猜测。', '我的项目使用 SQLite。', 'uncertain_knowledge'),
])
def test_sentence_boundary_never_clips_same_assertion_qualifiers(tmp_path, user, quote, reason):
    _, result, rows = learn(tmp_path, user, [proposal(quote, kind='fact')])
    assert not rows and result['rejection_reasons'] == {reason: 1}


@pytest.mark.parametrize('user', [
    "Yeah, I agree. It's always better to be prepared. I'll definitely use these tips for my next performance review.",
    "I remember listening to a radio show. It was always a highlight of my day.",
    "I usually just remember it in my head. I think I'll try using a notes app from now on.",
    "I've always admired the use of bold colors.",
    'I always love it when they use silence in horror movies.',
    'I can always use a soft cloth to protect my floors.',
    'I remember that we always meet in the evening.',
    '以后我打算默认使用浅色主题。',
    'Always use concise explanations. I remember a holiday from years ago.',
])
def test_narration_and_future_plans_cannot_authorize_automatic_activation(tmp_path, user):
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['status'] == 'candidate'


@pytest.mark.parametrize('user', [
    'From now on, answer everything in British English.',
    'Please always write concise explanations.',
    'Please remember: always use concise explanations.',
    '请记住这个项目决策：重试期限正式定为24小时。',
])
def test_explicit_agent_directives_still_activate(tmp_path, user):
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert result['promoted'] == 1 and rows[0]['status'] == 'active'


@pytest.mark.parametrize('user', [
    'I remember that this might always happen.',
    'I remember yesterday; from now on I might try a different editor.',
    '我记住了以前的经验，以后可能会换一个编辑器。',
])
def test_narrative_remember_cannot_authorize_uncertain_storage(tmp_path, user):
    _, result, rows = learn(tmp_path, user, [proposal(user)])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


def test_whole_mixed_turn_retains_only_a_grounded_candidate_assertion(tmp_path):
    user = 'My project uses SQLite. Can you explain WAL?'
    _, result, rows = learn(tmp_path, user, [proposal(user, kind='fact')])
    assert result['promoted'] == 0 and result['proposals'] == 1
    assert rows[0]['status'] == 'candidate' and rows[0]['content'] == 'My project uses SQLite.'

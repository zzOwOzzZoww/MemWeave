"""Evidence-local governance regressions, never a model-quality benchmark."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.learning_engine import DeepSeekReviewer, LearningEngine
from test_tiered_admission import learn, proposal


@pytest.mark.parametrize('text', [
    'Always use concise explanations, my project uses SQLite.',
    'Always use concise explanations and my project uses SQLite.',
    '以后默认使用简体中文，我的项目采用 SQLite 数据库。',
    'Please remember: always use concise explanations, my project uses SQLite.',
    'Always use concise explanations. "My project uses SQLite."',
    "Always use concise explanations. 'My project uses SQLite.'",
    '以后默认使用简体中文。“我的项目采用 SQLite 数据库。”',
    'Always use concise explanations. `My project uses SQLite.`',
    'Always use concise explanations.\n```text\nMy project uses SQLite.\n```',
    'Always use concise explanations. Use Python for this task.',
    'Always use concise explanations; verify the tests.',
    'Always use concise explanations.\nVerify the tests.',
    '以后默认使用简体中文。执行代码检查。',
    'Always use concise explanations. "My project uses SQLite.',
    "Always use concise explanations. 'My project uses SQLite.",
])
def test_mixed_authority_is_candidate_not_active(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert rows[0]['status'] == 'candidate' and rows[0]['content'] == text


@pytest.mark.parametrize('text', [
    'From now on, answer everything in British English.',
    'Always use concise explanations, always verify the tests.',
    'Always use concise explanations and verify the tests.',
    '以后默认先验证结果，不要把未经验证的推测当作结论。',
    'Always use concise explanations. Always verify the tests.',
    '以后默认使用简体中文。今后默认先验证结果。',
    'By default, verify the tests and check the logs.',
])
def test_unambiguous_directives_still_activate(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 1 and rows[0]['status'] == 'active'


@pytest.mark.parametrize('text,quotes,scope', [
    ('这个项目默认使用 SQLite 作为数据库。\n所有项目的文档请写清楚。',
     ['这个项目默认使用 SQLite 作为数据库。'], 'project'),
    ('This project uses SQLite. Always use concise answers in all my projects.',
     ['This project uses SQLite.'], 'project'),
    ('我的项目使用 SQLite；文档示例写着“所有项目”。',
     ['我的项目使用 SQLite；文档示例写着“所有项目”。'], 'project'),
    ("Documentation contains 'all my projects' as a sample phrase.",
     ["Documentation contains 'all my projects' as a sample phrase."], 'project'),
    ('仅限这个项目，默认遵循所有项目通用的代码风格。',
     ['默认遵循所有项目通用的代码风格。'], 'project'),
    ('这个项目使用 SQLite，不要跨项目共享这条规则。',
     ['这个项目使用 SQLite，不要跨项目共享这条规则。'], 'project'),
    ('Always use SQLite in this project, not across all projects.',
     ['Always use SQLite in this project, not across all projects.'], 'project'),
    ('我的项目使用 SQLite。今后所有项目默认使用浅色主题。',
     ['我的项目使用 SQLite。', '今后所有项目默认使用浅色主题。'], 'project'),
    ('我的项目使用 SQLite。今后所有项目默认使用浅色主题。',
     ['我的项目使用 SQLite。今后所有项目默认使用浅色主题。'], 'project'),
    ('今后所有项目默认使用浅色主题。', ['今后所有项目默认使用浅色主题。'], 'user'),
    ('Always use concise explanations in all my projects.',
     ['Always use concise explanations in all my projects.'], 'user'),
    ('Always use concise explanations in all my projects. "My project uses SQLite."',
     ['Always use concise explanations in all my projects. "My project uses SQLite."'], 'project'),
    ('今后所有项目默认使用浅色主题。“我的项目使用 SQLite。”',
     ['今后所有项目默认使用浅色主题。“我的项目使用 SQLite。”'], 'project'),
    ('Always use concise explanations in all my projects. `My project uses SQLite.`',
     ['Always use concise explanations in all my projects. `My project uses SQLite.`'], 'project'),
    ('"Always use concise explanations in all my projects."',
     ['Always use concise explanations in all my projects.'], 'project'),
    ('Always use concise explanations in all my projects. "My project uses SQLite.',
     ['Always use concise explanations in all my projects. "My project uses SQLite.'], 'project'),
    ('Always use concise explanations in all my projects. My project uses SQLite.',
     ['Always use concise explanations in all my projects.'], 'user'),
])
def test_scope_belongs_to_every_quoted_assertion(tmp_path, text, quotes, scope):
    engine, _, rows = learn(tmp_path, text, [proposal(text, source_quotes=quotes)])
    assert len(rows) == 1 and rows[0]['scope'] == scope
    # Even after approval, a project record must not appear in another project.
    engine.store.knowledge.feedback(agent_id='human', knowledge_id=rows[0]['id'],
        outcome='verified', evidence_kind='user_approval', evidence_ref='fixture:approval',
        evidence_summary='Synthetic operator approval')
    with engine.store.knowledge._connect() as db:
        visible = db.execute("SELECT COUNT(*) FROM knowledge_records WHERE status='active' "
            "AND (project_key='unrelated-project' OR scope='user')").fetchone()[0]
    assert visible == int(scope == 'user')


@pytest.mark.parametrize('text,kind,status', [
    ('以后默认采用 token 预算控制上下文长度。', 'preference', 'active'),
    ('My project uses the tokenizer from this library.', 'fact', 'candidate'),
    ('请长期记住 token 预算上限为4096。', 'preference', 'active'),
    ('Please remember the token budget is always 4096.', 'preference', 'active'),
    ('Always use environment variables for API keys.', 'preference', 'active'),
    ('以后默认使用密码哈希保护认证数据。', 'preference', 'active'),
    ('The password hashing algorithm uses Argon2.', 'fact', 'candidate'),
    ('My project uses a secret manager for credentials.', 'fact', 'candidate'),
    ('Please remember the API key rotation policy for future reference.', 'fact', 'candidate'),
])
def test_credential_concepts_are_not_secret_values(tmp_path, text, kind, status):
    _, result, rows = learn(tmp_path, text, [proposal(text, kind=kind)])
    assert result['rejection_reasons'] == {} and result['proposals'] == 1
    assert rows[0]['status'] == status


@pytest.mark.parametrize('text,reason', [
    ('请长期记住密码 password=fixture-private-secret。', 'sensitive_storage_request'),
    ('remember API key test-only-credential', 'sensitive_storage_request'),
    ('请记住今后默认把 API Key 写在配置文件里。', 'sensitive_storage_request'),
    ('My password is fixture-only-password.', 'sensitive_content'),
    ('token=fixture-only-value should be reused.', 'sensitive_content'),
    ('我的密码是测试专用口令不要实际使用。', 'sensitive_content'),
    ('The stored value was [REDACTED] from the source.', 'sensitive_content'),
])
def test_credential_rejections_have_specific_persistent_reasons(tmp_path, text, reason):
    # A tool prevents the pre-review fast path: exercise the per-proposal gate.
    engine, result, rows = learn(tmp_path, text, [proposal(text)],
        tools=(ToolEvent('fixture', 'shell', 'pytest fixture', 'PASS', True, 'test'),))
    assert not rows and result['rejection_reasons'] == {reason: 1}
    latest = engine.store.knowledge.latest_learning('codex')
    assert latest['rejection_reasons'] == {reason: 1}


def test_sensitive_fast_path_reports_suppression_without_calling_reviewer(tmp_path):
    def never_review(_):
        pytest.fail('credential-only turn must not call the model')
    engine = LearningEngine(database_path=tmp_path / 'fast.db', agent_id='codex', project_key='p',
        transcript_parser=lambda *a, **k: TranscriptTurn('remember API key test-only-credential', '', ()),
        reviewer=never_review)
    result = engine.learn({'session_id': 'fixture'})
    assert result['proposal_outcome'] == 'suppressed_sensitive_request'
    assert result['reviewer_proposals'] == result['rejected_proposals'] == result['proposals'] == 0
    assert result['rejection_reasons'] == {'sensitive_storage_request': 1}
    assert engine.store.knowledge.latest_learning('codex')['rejection_reasons'] == result['rejection_reasons']


def test_sensitive_sentence_does_not_suppress_independent_evidence(tmp_path):
    fact = 'My project uses SQLite.'
    secret = 'Please remember my password=fixture-only-password.'
    _, result, rows = learn(tmp_path, secret + '\n' + fact,
        [proposal(secret), proposal(fact, kind='fact')])
    assert result['proposals'] == 1 and result['promoted'] == 0
    assert result['rejection_reasons'] == {'sensitive_storage_request': 1}
    assert rows[0]['content'] == fact and rows[0]['status'] == 'candidate'


def test_old_adapter_imports_remain_compatible():
    from agent_knowledge_bridge.claude_learning_adapter import SECRET_STORAGE_REQUEST
    assert SECRET_STORAGE_REQUEST.search('remember API key fixture')


def test_missing_and_forged_quotes_are_not_misreported_as_credentials(tmp_path):
    text = 'My project uses SQLite.'
    _, result, rows = learn(tmp_path, text, [proposal('My project uses PostgreSQL.')])
    assert not rows and result['rejection_reasons'] == {'invalid_source_evidence': 1}


def test_dashboard_has_explicit_governance_reason_labels():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / 'src/agent_knowledge_bridge/knowledge_dashboard.html').read_text(encoding='utf-8')
    assert "sensitive_storage_request: '涉及凭据保存请求'" in page
    assert "temporary_knowledge: '临时信息未要求长期保留'" in page
    assert "uncertain_knowledge: '不确定内容未要求长期保留'" in page


@pytest.mark.parametrize('qualifier,reason', [
    ('This is temporary.', 'temporary_knowledge'),
    ('It is only temporary.', 'temporary_knowledge'),
    ('Temporary.', 'temporary_knowledge'),
    ('I am not sure.', 'uncertain_knowledge'),
    ("I'm not sure.", 'uncertain_knowledge'),
    ('This has not been verified yet.', 'uncertain_knowledge'),
    ('这只是临时的。', 'temporary_knowledge'),
    ('我还不确定。', 'uncertain_knowledge'),
])
@pytest.mark.parametrize('position', ['before', 'after'])
def test_adjacent_qualifiers_are_kept_and_classified(tmp_path, qualifier, reason, position):
    fact = 'My project uses SQLite.'
    text = qualifier + ' ' + fact if position == 'before' else fact + ' ' + qualifier
    _, result, rows = learn(tmp_path, text, [proposal(fact, kind='fact')])
    assert not rows and result['rejection_reasons'] == {reason: 1}


@pytest.mark.parametrize('text', [
    'My project uses SQLite. This is temporary. I am not sure.',
    'I am not sure. This is temporary. My project uses SQLite.',
])
def test_multiple_adjacent_qualifiers_are_not_truncated(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal('My project uses SQLite.', kind='fact')])
    assert not rows and result['rejection_reasons'] == {'uncertain_knowledge': 1}


@pytest.mark.parametrize('text', [
    'Always use SQLite, this is temporary.',
    '以后默认使用 SQLite，这只是临时的。',
])
def test_temporary_qualifier_blocks_automatic_activation(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert not rows and result['rejection_reasons'] == {'temporary_knowledge': 1}


def test_explicit_retention_keeps_warning_for_adjacent_qualifier(tmp_path):
    request = 'Please remember long-term: my project uses SQLite.'
    _, result, rows = learn(tmp_path, request + ' This is temporary.', [proposal(request)])
    assert result['promoted'] == 0 and rows[0]['status'] == 'candidate'
    assert '仅描述来源会话时的临时状态' in rows[0]['content']


@pytest.mark.parametrize('text', [
    'Always use temporary files for isolated tests.',
    '以后默认使用临时文件隔离测试数据。',
])
def test_temporary_resources_do_not_make_a_durable_rule_temporary(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 1 and rows[0]['status'] == 'active'


def test_reviewer_response_runtime_and_database_share_the_gate(tmp_path, monkeypatch):
    text = 'Always use concise explanations, my project uses SQLite.'
    model = DeepSeekReviewer.__new__(DeepSeekReviewer)
    model.api_key, model.base_url, model.model, model.timeout = 'fixture', 'http://invalid', 'fixture', 1
    response = {'choices': [{'message': {'content': json.dumps({'proposals': [proposal(text)]})}}]}
    monkeypatch.setattr('agent_knowledge_bridge.provider.open_request',
        lambda *a, **k: io.BytesIO(json.dumps(response).encode()))
    app = create_app(database_path=tmp_path / 'api.db', api_token='fixture', reviewer=model)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer fixture'}
        result = client.post('/v1/learning/turn', headers=headers, json={
            'agent_id': 'custom-agent', 'project_key': 'p', 'session_id': 's',
            'turn': {'user_text': text, 'assistant_text': 'Acknowledged.', 'tools': []}})
        assert result.status_code == 200 and result.json()['promoted'] == 0
        listing = client.post('/v1/knowledge/list', headers=headers, json={
            'agent_id': 'custom-agent', 'project_key': 'p', 'status': 'all'})
        assert len(listing.json()['results']) == 1
        assert listing.json()['results'][0]['status'] == 'candidate'

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from agent_knowledge_bridge.runtime_learning_adapter import normalized_tool


def proposal(text):
    quote = 'Use the widget verifier before deployment.'
    return {'proposals': [{'title': 'Widget deployment rule', 'content': quote,
        'knowledge_type': 'procedure', 'scope': 'project', 'source_role': 'user',
        'source_quotes': [quote], 'evidence_event_ids': re.findall(r'EVENT_ID: (ae_[a-f0-9]+)', text)}]}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMWEAVE_HOME', str(tmp_path / 'home'))
    app = create_app(database_path=tmp_path / 'test.db', api_token='test-only', reviewer=proposal)
    # No worker is needed for these synchronous API tests.
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer test-only'
    yield client, app
    app.state.timing_collector.close()
    client.close()


def request(**extra):
    return {'agent_id': 'custom-agent', 'project_key': 'shared', 'session_id': 's', 'turn_id': 't',
        'turn': {'user_text': 'Use the widget verifier before deployment.', 'assistant_text': 'Acknowledged.',
            'tools': [{'id': 'tool', 'name': 'Shell', 'input': {'command': 'python verify_widget.py'},
                       'output': 'passed', 'exit_code': 0}]}, **extra}


def test_generic_learning_is_candidate_deduplicated_and_recallable_after_review(runtime):
    client, app = runtime
    learned = client.post('/v1/learning/turn', json=request())
    assert learned.status_code == 200
    assert learned.json()['proposals'] == 1
    # An unrelated command pass does not prove the proposed procedure.
    assert learned.json()['promoted'] == 0
    assert client.post('/v1/learning/turn', json=request()).json()['status'] == 'duplicate_turn'
    context = {'agent_id': 'custom-agent', 'project_key': 'shared'}
    records = client.post('/v1/knowledge/list', json={**context, 'status': 'all'}).json()['results']
    assert len(records) == 1 and records[0]['status'] == 'candidate'
    assert records[0]['source_agent'] == 'custom-agent'
    approved = client.post('/v1/knowledge/feedback', json={**context,
        'knowledge_id': records[0]['id'], 'outcome': 'verified', 'evidence_kind': 'user_approval',
        'evidence_ref': 'manual-fixture', 'evidence_summary': 'Reviewed this exact instruction'})
    assert approved.status_code == 200
    recalled = client.post('/v1/learning/recall', json={**context, 'session_id': 'next', 'prompt': 'widget verifier deployment'})
    assert 'widget verifier' in recalled.json()['hookSpecificOutput']['additionalContext']
    assert client.post('/v1/learning/recall', json={**context, 'session_id': 'unrelated', 'prompt': 'weather today'}).json() == {}


@pytest.mark.parametrize('tool,expected', [
    ({'output': 'PASS'}, None),
    ({'output': 'PASS', 'is_error': False}, None),
    ({'exit_code': 0}, True),
    ({'exit_code': 1, 'is_error': False}, False),
    ({'exit_code': 0, 'is_error': True}, False),
    ({'exit_code': 0, 'status': 'running'}, None),
    ({'exit_code': 0, 'interrupted': True}, False),
])
def test_generic_tools_preserve_unknown_running_and_failure(tool, expected):
    event = normalized_tool({'id': 'x', 'name': 'Shell', 'input': {'command': 'pytest'}, **tool})
    assert event.success is expected
    assert event.objective_kind == 'test'


@pytest.mark.parametrize('change', [
    {'transcript_path': 'something.jsonl'}, {'turn': None},
    {'transcript_format': 'claude'},
    {'turn': {'user_text': 'x', 'tools': [{'id': 'same', 'name': 'Shell'}, {'id': 'same', 'name': 'Shell'}]}},
    {'turn': {'user_text': 'x', 'tools': [{'id': 'x', 'name': 'Shell', 'exit_code': True}]}},
])
def test_generic_input_rejects_ambiguous_or_invalid_evidence(runtime, change):
    client, _ = runtime
    assert client.post('/v1/learning/turn', json=request(**change)).status_code == 422


def test_queue_does_not_persist_inline_turn(runtime):
    client, app = runtime
    queue = app.state.learning_queue
    queue.store.knowledge.register_agent(agent_id='custom-agent', display_name='Custom', adapter_type='custom')
    assert client.post('/v1/learning/queue', json=request()).status_code == 422
    assert queue.summary() == {}
    with pytest.raises(ValueError, match='cannot be persisted'):
        queue.submit(request())


def test_generic_transcript_requires_explicit_format(runtime, tmp_path):
    client, _ = runtime
    path = tmp_path / 'claude.jsonl'
    path.write_text(json.dumps({'type': 'user', 'message': {'role': 'user',
        'content': 'Use the widget verifier before deployment.'}}) + '\n', encoding='utf-8')
    args = request(turn=None, transcript_path=str(path))
    assert client.post('/v1/learning/turn', json=args).status_code == 422
    result = client.post('/v1/learning/turn', json={**args, 'transcript_format': 'claude'})
    assert result.status_code == 200 and result.json()['proposals'] == 1


def test_disabled_agent_does_not_parse_or_call_reviewer(runtime):
    client, app = runtime
    app.state.learning_queue.store.knowledge.register_agent(agent_id='custom-agent', display_name='Custom', adapter_type='custom')
    app.state.learning_queue.store.knowledge.disable_agent('custom-agent')
    assert client.post('/v1/learning/turn', json=request()).json() == {'status': 'disabled'}


def test_client_sends_normalized_turn_without_making_a_transcript(monkeypatch):
    client = MemWeaveRuntimeClient(base_url='http://localhost', token='test-only', agent_id='custom-agent', project_key='p')
    monkeypatch.setattr(client, '_post', lambda path, payload: (path, payload))
    path, payload = client.learn(session_id='s', turn=request()['turn'])
    assert path == '/v1/learning/turn' and payload['turn']['user_text']
    assert payload['transcript_path'] == ''


def test_inline_turn_redacts_secrets_before_review_and_persistence(runtime):
    client, app = runtime
    seen = []
    app.state.reviewer = lambda text: seen.append(text) or {'proposals': []}
    secret = 'TEST_ONLY_NOT_A_REAL_SECRET_123456'
    args = request()
    args['turn']['user_text'] = 'Use the verifier. api_key=' + secret
    args['turn']['assistant_text'] = 'PRIVATE_CHAT_NOT_A_LESSON password=' + secret
    args['turn']['tools'][0]['input']['api_key'] = secret
    args['turn']['tools'][0]['output'] = 'PASS\naccess_token=' + secret
    assert client.post('/v1/learning/turn', json=args).status_code == 200
    assert len(seen) == 1 and secret not in seen[0]
    with app.state.learning_queue.store.knowledge._connect() as db:
        dumped = '\n'.join(db.iterdump())
    assert secret not in dumped and 'PRIVATE_CHAT_NOT_A_LESSON' not in dumped

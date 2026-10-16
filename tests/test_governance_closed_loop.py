"""Isolated Runtime integration acceptance, not a real client/model verdict."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.learning_engine import DeepSeekReviewer
from test_tiered_admission import proposal


@pytest.mark.parametrize('source_agent,reader', [('codex', 'gemini-cli'),
                                             ('gemini-cli', 'claude-code'), ('claude-code', 'codex')])
def test_grounded_candidate_review_recall_and_undo_closed_loop(tmp_path, monkeypatch, source_agent, reader):
    monkeypatch.setenv('MEMWEAVE_HOME', str(tmp_path / 'home'))
    fact = 'My service uses SQLite, which stores the deployment index.'
    text = fact + ' Can you explain deployment indexing?'
    model = DeepSeekReviewer.__new__(DeepSeekReviewer)
    model.api_key, model.base_url, model.model, model.timeout = 'fixture', 'http://invalid', 'fixture', 1
    replies = iter([proposal(text, kind='fact', title='Storage fact'),
                    proposal(fact, kind='fact', title='Another title for the exact fact')])
    def respond(*args, **kwargs):
        payload = {'choices': [{'message': {'content': json.dumps({'proposals': [next(replies)]})}}]}
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr('agent_knowledge_bridge.provider.open_request', respond)
    app = create_app(database_path=tmp_path / 'runtime.db', api_token='fixture', reviewer=model)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer fixture'
        context = {'agent_id': source_agent, 'project_key': 'p'}
        request = {**context, 'session_id': 'first', 'turn_id': 't1',
            'turn': {'user_text': text, 'assistant_text': 'Acknowledged.', 'tools': []}}
        result = client.post('/v1/learning/turn', json=request)
        assert result.status_code == 200 and result.json()['promoted'] == 0
        records = client.post('/v1/knowledge/list', json={**context, 'status': 'all'}).json()['results']
        assert len(records) == 1 and records[0]['content'] == fact and records[0]['status'] == 'candidate'
        key = records[0]['id']
        other = {'agent_id': reader, 'project_key': 'p'}
        query = {**other, 'session_id': 'before-review', 'prompt': 'SQLite deployment index'}
        assert client.post('/v1/learning/recall', json=query).json() == {}

        # Another actual source turn, parsed through the reviewer interface,
        # reuses the candidate while retaining its independent compilation link.
        again = {**request, 'agent_id': reader, 'session_id': 'second',
                 'turn': {'user_text': fact, 'assistant_text': '', 'tools': []}}
        assert client.post('/v1/learning/turn', json=again).json()['promoted'] == 0
        assert len(client.post('/v1/knowledge/list', json={**context, 'status': 'all'}).json()['results']) == 1

        preview = client.post('/v1/governance/preview', json={**context, 'actions': [
            {'action': 'approve', 'knowledge_id': key, 'reason': 'Fixture operator reviewed exact source.'}]}).json()
        plan = {**context, 'plan_id': preview['id']}
        assert client.post('/v1/governance/apply', json=plan).status_code == 422
        approve = {'project_key': 'p', 'plan_id': preview['id'], 'confirm': True}
        assert client.post('/v1/governance/approve', json=approve).status_code == 200
        assert client.post('/v1/governance/apply', json=plan).status_code == 200
        # Undo before retrieval mutates hit counts; a later recall correctly
        # invalidates stale undo guards rather than overwriting that activity.
        assert client.post('/v1/governance/undo', json=approve).status_code == 200
        preview = client.post('/v1/governance/preview', json={**context, 'actions': [
            {'action': 'approve', 'knowledge_id': key, 'reason': 'Fixture operator reapproved exact source.'}]}).json()
        plan['plan_id'] = approve['plan_id'] = preview['id']
        assert client.post('/v1/governance/approve', json=approve).status_code == 200
        assert client.post('/v1/governance/apply', json=plan).status_code == 200
        recalled = client.post('/v1/learning/recall', json={**query, 'session_id': 'after-review'}).json()
        assert fact in recalled['hookSpecificOutput']['additionalContext']
        assert client.post('/v1/learning/recall', json={**query, 'project_key': 'other', 'session_id': 'other-project'}).json() == {}
        assert client.post('/v1/learning/recall', json={**query, 'session_id': 'noise', 'prompt': 'weather today'}).json() == {}
        assert client.post('/v1/governance/undo', json=approve).status_code == 422

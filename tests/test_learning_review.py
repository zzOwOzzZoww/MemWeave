"""Batch review is scoped, concurrent-safe, and visible across hook processes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.dashboard_events import DashboardChanges
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.store import KnowledgeStore


@pytest.fixture
def learning(tmp_path):
    return LearningStore(tmp_path / 'review.db')


def run(learning, name='batch', agent='codex', project='review-test'):
    return learning.begin_run(agent_id=agent, project_key=project, session_id=name,
                              turn_hash=name, input_chars=100)


def publish(learning, name='entry', agent='codex', project='review-test', scope='project'):
    return learning.knowledge.publish(source_agent=agent, project_key=project,
        title=name, content=f'Synthetic knowledge for {name}.', knowledge_type='fact',
        scope=scope, evidence_summary='Synthetic observed evidence', source_session='fixture')['knowledge']['id']


def records(learning, batch, **kwargs):
    return learning.run_records(requester_agent='human-review', project_key='review-test',
                                source_agent='codex', run_id=batch, **kwargs)


def feedback(learning, key, batch, outcome='verified', **kwargs):
    return learning.knowledge.feedback(agent_id='human-review', project_key='review-test',
        knowledge_id=key, learning_run_id=batch, expected_status='candidate', outcome=outcome,
        evidence_kind='user_approval', evidence_ref='test://review', evidence_summary='Reviewed fixture', **kwargs)


def test_batch_links_not_agent_or_session_guessing(learning):
    batch = run(learning)
    next_batch = run(learning, 'next-batch')
    other_agent = run(learning, 'other-agent', agent='claude-code')
    other_project = run(learning, 'other-project', project='elsewhere')
    selected = publish(learning)
    unrelated = [publish(learning, name) for name in ('later', 'different-agent', 'different-project')]
    learning.link_compilation(batch, selected, 'produced')
    for batch_id, key in zip((next_batch, other_agent, other_project), unrelated):
        learning.link_compilation(batch_id, key, 'produced')
    result = records(learning, batch)
    assert [item['knowledge']['id'] for item in result['results']] == [selected]
    assert result['results'][0]['knowledge']['content']
    assert result['results'][0]['evidence'][0]['summary'] == 'Synthetic observed evidence'
    assert result['pending_count'] == result['linked_count'] == 1


def test_deduplicated_links_are_unique_and_keep_original_source(learning):
    batch = run(learning)
    key = publish(learning, agent='claude-code', project='elsewhere', scope='user')
    learning.link_compilation(batch, key, 'produced')
    learning.link_compilation(batch, key, 'deduplicated')
    result = records(learning, batch)
    assert result['total'] == result['pending_count'] == 1
    assert result['results'][0]['relations'] == ['deduplicated', 'produced']
    assert result['results'][0]['knowledge']['source_agent'] == 'claude-code'


def test_historical_count_and_processed_entries_survive_review(learning):
    batch = run(learning)
    keys = [publish(learning, name) for name in ('approved', 'isolated')]
    for key in keys:
        learning.link_compilation(batch, key, 'produced')
    learning.finish_run(batch, status='completed', proposal_count=2)
    assert learning.knowledge.latest_learning('codex')['pending_count'] == 2
    feedback(learning, keys[0], batch)
    feedback(learning, keys[1], batch, outcome='rejected')
    latest = learning.knowledge.latest_learning('codex')
    assert latest['run_id'] == batch
    assert latest['project_key'] == 'review-test'
    assert latest['proposal_count'] == 2 and latest['pending_count'] == 0
    assert records(learning, batch, status='candidate')['results'] == []
    result = records(learning, batch)
    assert {item['knowledge']['status'] for item in result['results']} == {'active', 'quarantined'}
    assert all(len(item['evidence']) == 2 for item in result['results'])


def test_empty_deleted_and_legacy_runs_do_not_fabricate_records(learning):
    batch = run(learning)
    learning.finish_run(batch, status='completed', proposal_count=2)
    assert records(learning, batch)['total'] == 0
    key = publish(learning)
    learning.link_compilation(batch, key, 'produced')
    learning.knowledge.remove_many(agent_id='human-review', project_key='review-test', knowledge_ids=[key])
    assert records(learning, batch)['total'] == 0
    assert learning.knowledge.latest_learning('codex')['proposal_count'] == 2
    with learning.knowledge._connect() as db:
        db.execute('DROP TABLE knowledge_compilation_links')
    assert learning.knowledge.latest_learning('codex')['pending_count'] == 0


def test_latest_uses_exact_run_even_when_timestamps_tie(learning):
    first = run(learning, 'first')
    second = run(learning, 'second')
    key = publish(learning)
    learning.link_compilation(first, key, 'produced')
    with learning.knowledge._connect() as db:
        db.execute("UPDATE learning_runs SET created_at='2026-09-30T00:00:00+00:00'")
    latest = learning.knowledge.latest_learning('codex')
    assert latest['run_id'] == second and latest['pending_count'] == 0


def test_pagination_and_cross_project_link_defense(learning):
    batch = run(learning)
    keys = [publish(learning, f'entry-{index}') for index in range(23)]
    for key in keys:
        learning.link_compilation(batch, key, 'produced')
    foreign = publish(learning, 'foreign', project='elsewhere')
    learning.link_compilation(batch, foreign, 'produced')
    first = records(learning, batch, limit=20)
    second = records(learning, batch, offset=20, limit=20)
    assert first['total'] == 23 and first['count'] == 20 and second['count'] == 3
    assert {item['knowledge']['id'] for item in first['results'] + second['results']} == set(keys)
    assert learning.knowledge.latest_learning('codex')['pending_count'] == 23
    with pytest.raises(ValueError, match='not found'):
        feedback(learning, foreign, batch)


def test_feedback_cannot_review_a_different_run_member(learning):
    batch = run(learning)
    another = run(learning, 'another')
    key = publish(learning)
    learning.link_compilation(another, key, 'produced')
    with pytest.raises(ValueError, match='not part'):
        feedback(learning, key, batch)
    assert learning.knowledge.get(requester_agent='human-review', knowledge_id=key)['knowledge']['status'] == 'candidate'


def test_concurrent_decisions_change_status_only_once(learning):
    batch = run(learning)
    key = publish(learning)
    learning.link_compilation(batch, key, 'produced')
    def decide(outcome):
        try:
            return feedback(learning, key, batch, outcome)['status']
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ('verified', 'rejected')))
    assert sorted(results) == ['knowledge status changed', 'recorded']
    assert len(learning.knowledge.get(requester_agent='human-review', knowledge_id=key)['evidence']) == 2


def test_batch_approval_preserves_version_conflict_gate(learning):
    batch = run(learning)
    keys = []
    for hours in (24, 48):
        key = learning.knowledge.publish(source_agent='codex', project_key='review-test', scope='project',
            title=f'Review deadline {hours}', content=f'审核期限正式定为{hours}小时。',
            knowledge_type='decision', evidence_summary='Synthetic statement')['knowledge']['id']
        learning.link_compilation(batch, key, 'produced')
        keys.append(key)
    feedback(learning, keys[0], batch)
    with pytest.raises(ValueError, match='替代'):
        feedback(learning, keys[1], batch)
    result = records(learning, batch, status='candidate')
    assert result['pending_count'] == 1
    assert result['results'][0]['versions']['conflicts'][0]['id'] == keys[0]


def test_authenticated_api_enforces_run_agent_and_project(learning):
    batch = run(learning)
    key = publish(learning)
    learning.link_compilation(batch, key, 'produced')
    app = create_app(database_path=learning.database_path, api_token='test-token')
    payload = {'agent_id':'human-review', 'source_agent':'codex', 'project_key':'review-test', 'run_id':batch}
    headers = {'Authorization':'Bearer test-token'}
    with TestClient(app) as client:
        assert client.post('/v1/learning/run-records', json=payload).status_code == 401
        assert client.get('/v1/dashboard/events').status_code == 401
        assert client.post('/v1/learning/run-records', json=payload, headers=headers).json()['total'] == 1
        for override in ({'project_key':'elsewhere'}, {'source_agent':'claude-code'}, {'run_id':'lr_missing'}):
            response = client.post('/v1/learning/run-records', json={**payload, **override}, headers=headers)
            assert response.status_code == 422 and response.json()['detail'] == 'learning run not found'
        for override in ({'status':'active'}, {'offset':-1}, {'limit':101}, {'run_id':'../bad'}):
            assert client.post('/v1/learning/run-records', json={**payload, **override}, headers=headers).status_code == 422
        review = {k:payload[k] for k in ('agent_id', 'project_key')}
        review.update(knowledge_id=key, learning_run_id=batch, expected_status='candidate',
            outcome='verified', evidence_summary='Reviewed fixture', evidence_kind='user_approval', evidence_ref='test://approval')
        assert client.post('/v1/knowledge/feedback', json=review, headers=headers).status_code == 200
        stale = client.post('/v1/knowledge/feedback', json={**review, 'outcome':'rejected'}, headers=headers)
        assert stale.status_code == 422 and stale.json()['detail'] == 'knowledge status changed'
        result = client.post('/v1/learning/run-records', json=payload, headers=headers).json()
        assert result['pending_count'] == 0 and result['results'][0]['knowledge']['status'] == 'active'


def test_api_expires_candidates_and_keeps_batch_history(learning):
    batch = run(learning)
    key = publish(learning)
    learning.link_compilation(batch, key, 'produced')
    learning.finish_run(batch, status='completed', proposal_count=1)
    app = create_app(database_path=learning.database_path, api_token='test-token')
    with learning.knowledge._connect() as db:
        db.execute("UPDATE knowledge_records SET candidate_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (key,))
    with TestClient(app) as client:
        result = client.post('/v1/learning/run-records', headers={'Authorization':'Bearer test-token'},
            json={'agent_id':'human-review','project_key':'review-test','source_agent':'codex','run_id':batch}).json()
        assert result['pending_count'] == 0 and result['run']['proposal_count'] == 1
        assert result['results'][0]['knowledge']['status'] == 'quarantined'


def test_changes_observe_independent_hook_connections_without_telemetry_noise(learning):
    changes = DashboardChanges(learning)
    def revision():
        with learning.knowledge._connect() as db:
            return changes.revision(db)
    before = revision()
    external = LearningStore(learning.database_path)
    batch = run(external)
    assert revision() != before
    before = revision()
    external.finish_run(batch, status='completed', proposal_count=0)
    assert revision() != before
    before = revision()
    key = publish(external)
    assert revision() != before
    external.link_compilation(batch, key, 'produced')
    before = revision()
    feedback(external, key, batch)
    assert revision() != before
    before = revision()
    external.record_recall(agent_id='codex', project_key='review-test', session_id='fixture',
        query='synthetic', knowledge_ids=[], injected_chars=0, latency_ms=1)
    with learning.knowledge._connect() as db:
        db.execute('UPDATE knowledge_records SET hit_count=hit_count+1 WHERE id=?', (key,))
        assert db.execute('SELECT COUNT(*) FROM dashboard_revision').fetchone()[0] == 1
    assert revision() == before


def test_changes_never_publish_rolled_back_writes(learning):
    changes = DashboardChanges(learning)
    batch = run(learning)
    with learning.knowledge._connect() as db:
        before = changes.revision(db)
    with pytest.raises(RuntimeError):
        with learning.knowledge._connect() as db:
            db.execute("UPDATE learning_runs SET status='failed' WHERE id=?", (batch,))
            raise RuntimeError('abort transaction')
    with learning.knowledge._connect() as db:
        assert changes.revision(db) == before


def test_stream_initial_snapshot_change_and_disconnect(learning):
    changes = DashboardChanges(learning)
    class Request:
        disconnected = False
        async def is_disconnected(self):
            return self.disconnected
    async def check():
        request = Request()
        stream = changes.stream(request, interval=0)
        assert await anext(stream) == 'event: changed\ndata: {}\n\n'
        # A separate store represents a native hook, not the HTTP writer.
        external = KnowledgeStore(learning.database_path)
        external.publish(source_agent='codex', project_key='review-test', title='Hook output',
            content='Synthetic hook output', knowledge_type='fact', scope='project', evidence_summary='Synthetic evidence')
        assert await anext(stream) == 'event: changed\ndata: {}\n\n'
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(check())

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.store import KnowledgeStore


def publish(store, title='Review deadline', scope='project'):
    return store.publish(source_agent='claude-code', project_key='review-test',
        title=title, content='A reviewable project convention.', knowledge_type='fact',
        scope=scope, evidence_summary='Isolated test observation')['knowledge']


def test_exact_24_hour_boundary_is_reversible_and_preserves_approved(tmp_path):
    now = datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)
    store = KnowledgeStore(tmp_path/'knowledge.db', clock=lambda: now.isoformat(timespec='seconds'))
    candidate = publish(store)
    approved = publish(store, 'Already approved')
    store.feedback(agent_id='human', knowledge_id=approved['id'], outcome='verified',
        evidence_kind='user_approval', evidence_summary='Approved fixture', evidence_ref='test://approval')
    governor = Governor(store)
    assert datetime.fromisoformat(candidate['candidate_expires_at']) - now == timedelta(hours=24)
    now += timedelta(hours=24, seconds=-1)
    assert governor.expire_candidates()['expired_candidates'] == 0
    now += timedelta(seconds=1)
    assert governor.expire_candidates(dry_run=True)['expired_candidates'] == 1
    assert store.get(requester_agent='human', knowledge_id=candidate['id'])['knowledge']['status'] == 'candidate'
    assert governor.expire_candidates()['expired_candidates'] == 1
    assert governor.expire_candidates()['expired_candidates'] == 0
    assert store.get(requester_agent='human', knowledge_id=approved['id'])['knowledge']['status'] == 'active'
    assert len(store.lifecycle_audit(candidate['id'])) == 1
    restored = store.feedback(agent_id='human', knowledge_id=candidate['id'], outcome='verified',
        evidence_kind='user_approval', evidence_summary='Reviewed after expiry', evidence_ref='test://late-review')
    assert restored['knowledge']['status'] == 'active'
    assert restored['knowledge']['candidate_expires_at'] is None


def test_duplicate_proposal_does_not_extend_deadline(tmp_path):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    store = KnowledgeStore(tmp_path/'knowledge.db', clock=lambda: now.isoformat())
    original = publish(store)
    now += timedelta(hours=23)
    duplicate = publish(store)
    assert duplicate['id'] == original['id']
    assert duplicate['candidate_expires_at'] == original['candidate_expires_at']
    now += timedelta(hours=1)
    assert Governor(store).expire_candidates()['expired_candidates'] == 1


def test_upgrade_only_shortens_old_default_pending_and_is_idempotent(tmp_path):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    store = KnowledgeStore(tmp_path/'knowledge.db', clock=lambda: now.isoformat(timespec='seconds'))
    rows = [publish(store, title) for title in ('old', 'custom', 'legacy', 'active', 'archived', 'quarantined')]
    old_deadline = (now+timedelta(days=3)).isoformat(timespec='seconds')
    custom = (now+timedelta(hours=12)).isoformat(timespec='seconds')
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET candidate_expires_at=?', (old_deadline,))
        db.execute('UPDATE knowledge_records SET candidate_expires_at=? WHERE id=?', (custom, rows[1]['id']))
        db.execute('UPDATE knowledge_records SET candidate_expires_at=NULL WHERE id=?', (rows[2]['id'],))
        for record,status in zip(rows[3:], ('active','archived','quarantined'), strict=True):
            db.execute('UPDATE knowledge_records SET status=? WHERE id=?', (status,record['id']))
        before = [tuple(r) for r in db.execute('SELECT * FROM knowledge_records ORDER BY id')]
        epoch = db.execute('SELECT epoch FROM retrieval_revision').fetchone()[0]
    assert store.migrate_candidate_review_deadlines() == 1
    assert store.migrate_candidate_review_deadlines() == 0
    with store._connect() as db:
        old = db.execute('SELECT * FROM knowledge_records WHERE id=?',(rows[0]['id'],)).fetchone()
        assert datetime.fromisoformat(old['candidate_expires_at']) == now+timedelta(days=1)
        assert old['created_at'] == rows[0]['created_at']
        assert old['updated_at'] == rows[0]['updated_at']
        assert db.execute('SELECT epoch FROM retrieval_revision').fetchone()[0] == epoch
        after = [tuple(r) for r in db.execute('SELECT * FROM knowledge_records ORDER BY id')]
    assert sum(a != b for a,b in zip(before,after,strict=True)) == 1
    assert len(after) == len(before)


def test_runtime_startup_migrates_then_expires_old_candidate(tmp_path):
    then = datetime.now(timezone.utc)-timedelta(hours=25)
    store = KnowledgeStore(tmp_path/'knowledge.db', clock=lambda: then.isoformat(timespec='seconds'))
    candidate = publish(store)
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET candidate_expires_at=? WHERE id=?',
            ((then+timedelta(days=3)).isoformat(timespec='seconds'),candidate['id']))
    app=create_app(database_path=store.database_path, api_token='local-test-only', reviewer=lambda _: {'proposals':[]})
    headers={'Authorization':'Bearer local-test-only'}
    with TestClient(app) as client:
        response=client.post('/v1/knowledge/get',headers=headers,json={
            'agent_id':'human','project_key':'review-test','knowledge_id':candidate['id']})
        assert response.status_code == 200
        assert response.json()['knowledge']['status'] == 'quarantined'
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0] == 1


def test_management_read_expires_at_one_day_without_touching_other_project(tmp_path):
    now=datetime(2026,9,24,9,30,tzinfo=timezone.utc)
    service=KnowledgeBridgeService(agent_id='human',project_key='review-test',
        database_path=tmp_path/'knowledge.db',clock=lambda:now.isoformat())
    record=publish(service.store)
    other=service.store.publish(source_agent='claude-code',project_key='other-project',
        title='Other project',content='Unrelated candidate',knowledge_type='fact',scope='project',
        evidence_summary='fixture')['knowledge']
    now+=timedelta(hours=24)
    assert service.review_queue(status='candidate')['count'] == 0
    assert service.review_queue(status='quarantined')['results'][0]['id'] == record['id']
    assert service.store.get(requester_agent='human',knowledge_id=other['id'])['knowledge']['status'] == 'candidate'

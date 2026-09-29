import json

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.reuse import ReuseStore
from agent_knowledge_bridge.store import KnowledgeStore


def publish(store, name='alpha', project='p', scope='project'):
    return store.publish(source_agent='claude-code', project_key=project, title=name,
        content=name + ' reusable fact', knowledge_type='fact', evidence_summary='observed',
        scope=scope)['knowledge']['id']


def approve(store, key):
    return store.feedback(agent_id='human-review', project_key='p', knowledge_id=key,
        outcome='verified', evidence_summary='checked', evidence_kind='user_approval', evidence_ref='test://approval')


def remove(store, ids):
    return store.remove_many(agent_id='human-review', project_key='p', knowledge_ids=ids)


def test_delete_cleans_indexes_relationships_and_replay_cache(tmp_path):
    database = tmp_path / 'knowledge.db'
    learning, reuse = LearningStore(database), ReuseStore(database)
    store = learning.knowledge
    key, retained = publish(store), publish(store, 'beta')
    approve(store, key)
    record = store.get(requester_agent='codex', knowledge_id=key, project_key='p')['knowledge']
    trace, _, _ = reuse.start(agent_id='codex', project_key='p', session_id='s',
        turn_id='t', prompt='alpha', records=[record], retrieval_ms=1, validate_live_records=True)
    with store._connect() as db:
        db.execute("INSERT INTO agent_events(id,agent_id,project_key,session_id,event_type,payload_json,payload_hash,created_at) VALUES ('ae_test','codex','p','s','test','{}','hash','now')")
        db.execute("INSERT INTO learning_runs(id,agent_id,project_key,session_id,turn_hash,status,created_at) VALUES ('lr_test','codex','p','s','unique','done','now')")
        db.execute("INSERT INTO knowledge_event_links VALUES (?,'ae_test','evidence','now')", (key,))
        db.execute("INSERT INTO knowledge_compilation_links VALUES ('lr_test',?,'compiled','now')", (key,))
        db.execute("INSERT INTO experience_outcomes(knowledge_id,admission,updated_at) VALUES (?,?,?)", (key,'test','now'))
        db.execute("INSERT INTO shadow_hits (knowledge_id,project_key,hit_count,best_rank,first_seen_at,last_seen_at) VALUES (?,?,1,1,?,?)", (key,'p','now','now'))
        before = db.execute('SELECT revision FROM retrieval_revision').fetchone()[0]
    assert remove(store, [key, key])['removed_count'] == 1
    with store._connect() as db:
        for table in ('knowledge_records', 'knowledge_evidence', 'knowledge_fts', 'experience_outcomes', 'shadow_hits',
                      'knowledge_event_links','knowledge_compilation_links'):
            column = 'id' if table == 'knowledge_records' else 'knowledge_id'
            assert db.execute(f'SELECT count(*) FROM {table} WHERE {column}=?', (key,)).fetchone()[0] == 0
        assert db.execute('SELECT revision FROM retrieval_revision').fetchone()[0] > before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('SELECT count(*) FROM agent_events').fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM term_cooccurrence WHERE term_a='alpha' OR term_b='alpha'").fetchall()
    assert reuse.existing('codex', 'p', 's', 't')['context_text'] == ''
    assert reuse.list('p')[0]['items'][0]['removed']
    assert store.get(requester_agent='codex', knowledge_id=retained)['knowledge']
    assert store.search(requester_agent='codex', project_key='p', query='alpha', limit=3)['results'] == []
    assert remove(store, [key]) == {'status':'removed', 'removed_count':0,'already_missing_count':1}


def test_scope_check_is_atomic_and_user_scope_is_explicitly_shared(tmp_path):
    store = KnowledgeStore(tmp_path / 'k.db')
    own, foreign = publish(store), publish(store, 'other', 'other')
    with pytest.raises(ValueError, match='outside'):
        remove(store, [own, foreign])
    assert store.get(requester_agent='codex', knowledge_id=own)['knowledge']
    shared = publish(store, 'shared', 'other', 'user')
    assert remove(store, [shared])['removed_count'] == 1


def test_approval_is_idempotent_but_real_test_evidence_still_counts(tmp_path):
    store = KnowledgeStore(tmp_path / 'k.db')
    key = publish(store)
    assert approve(store, key)['status'] == 'recorded'
    before = store.get(requester_agent='codex', knowledge_id=key)
    assert approve(store, key)['status'] == 'already_active'
    assert store.get(requester_agent='codex', knowledge_id=key) == before
    result = store.feedback(agent_id='codex', project_key='p', knowledge_id=key,
        outcome='verified', evidence_summary='test passed', evidence_kind='test', evidence_ref='test://real')
    assert result['knowledge']['verified_count'] == 2


def test_delete_rollback_on_dependency_failure(tmp_path):
    store = KnowledgeStore(tmp_path / 'k.db')
    keys = [publish(store), publish(store, 'beta')]
    with store._connect() as db:
        db.execute("CREATE TRIGGER prevent_delete BEFORE DELETE ON knowledge_records BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(Exception, match='test failure'):
        remove(store, keys)
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM knowledge_evidence').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM knowledge_fts').fetchone()[0] == 2


def test_api_requires_confirmation_auth_and_keeps_project_isolation(tmp_path):
    database = tmp_path / 'k.db'
    store = KnowledgeStore(database)
    key = publish(store)
    payload = {'agent_id':'human-review','project_key':'p','knowledge_ids':[key]}
    with TestClient(create_app(database_path=database, api_token='test')) as client:
        assert client.post('/v1/knowledge/remove', json=payload).status_code == 401
        headers = {'Authorization':'Bearer test'}
        assert client.post('/v1/knowledge/remove', headers=headers, json=payload).status_code == 422
        result = client.post('/v1/knowledge/remove', headers=headers, json={**payload,'confirm_permanent':True})
        assert result.status_code == 200 and result.json()['removed_count'] == 1


def test_stale_search_result_cannot_recreate_removed_context(tmp_path):
    reuse = ReuseStore(tmp_path / 'k.db')
    key = publish(reuse.knowledge)
    record = reuse.knowledge.get(requester_agent='codex', knowledge_id=key)['knowledge']
    remove(reuse.knowledge, [key])
    _, context, ids = reuse.start(agent_id='codex', project_key='p', session_id='s',
        turn_id='t', prompt='alpha', records=[record], retrieval_ms=1, validate_live_records=True)
    assert context == '' and ids == []

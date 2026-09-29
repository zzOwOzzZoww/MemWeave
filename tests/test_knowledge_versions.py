"""Version lifecycle boundaries, using disposable stores and real adapters."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.store import KnowledgeStore
from agent_knowledge_bridge.knowledge_versions import extract, initialize
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.reuse import ReuseStore
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.daemon import create_app


def publish(store, value='24小时', *, project='alpha', scope='project', agent='claude-code', title='审核期限'):
    return store.publish(source_agent=agent, project_key=project, scope=scope, title=title,
        content=f'审核期限正式定为{value}。', knowledge_type='decision',
        evidence_summary='测试：用户明确确认的原句')['knowledge']['id']


def approve(store, key, **extra):
    return store.feedback(agent_id='human-review', knowledge_id=key, outcome='verified',
        evidence_kind='user_approval', evidence_ref='test://approval', evidence_summary='已核对', **extra)


def record(store, key):
    return store.get(requester_agent='codex', knowledge_id=key)['knowledge']


def search(store, query='审核期限', project='alpha'):
    return store.search(requester_agent='codex', project_key=project, query=query, limit=10)['results']


@pytest.fixture
def store(tmp_path):
    return KnowledgeStore(tmp_path/'versions.db')


@pytest.mark.parametrize('text', [
    '如果上线，审核期限为24小时。', '下周审核期限为24小时。',
    '2026-10-01审核期限为24小时。', '示例：审核期限为24小时。',
    '他说“审核期限为24小时”。', '审核期限为24小时；主题为浅色。',
    '审核期限可能为24小时。', '审核期限为24小时？',
    '审核期限为24小时。仅当负责人批准。',
])
def test_conditional_and_compound_text_is_not_flattened(text):
    assert extract(text, 'decision') is None


def test_normalized_units_and_fact_boundary():
    assert extract('审核期限为1天。', 'decision') == extract('审核期限正式定为24小时。', 'decision')
    assert extract('审核期限为24小时。', 'fact') is None


def test_candidate_conflict_preserves_current_until_explicit_replacement(store):
    old = publish(store); approve(store, old)
    new = publish(store, '48小时', agent='codex', title='最新审核设置')
    assert [r['id'] for r in search(store)] == [old]
    details = store.get(requester_agent='codex', knowledge_id=new)['versions']
    assert [r['id'] for r in details['conflicts']] == [old]
    with pytest.raises(ValueError, match='替代'):
        approve(store, new)
    assert record(store, old)['status']=='active'
    assert record(store, new)['status']=='candidate'
    approve(store, new, supersedes=[old])
    assert [r['id'] for r in search(store)] == [new]
    assert record(store, old)['status']=='archived'
    assert record(store, old)['superseded_by']==new
    assert record(store, old)['valid_until']==record(store, new)['valid_from']
    assert {r['id'] for r in search(store, '查看历史审核期限')} == {old, new}


def test_same_value_not_conflict_and_other_projects_do_not_override(store):
    old=publish(store); approve(store,old)
    equivalent=publish(store,'1天',title='等值表述'); approve(store,equivalent)
    other=publish(store,'48小时',project='beta'); approve(store,other)
    global_pref=publish(store,'72小时',scope='user'); approve(store,global_pref)
    assert all(record(store,k)['status']=='active' for k in (old,equivalent,other,global_pref))
    candidate=publish(store,'48小时')
    with pytest.raises(ValueError): approve(store,candidate,supersedes=[old])
    with pytest.raises(ValueError): approve(store,candidate,supersedes=[old,equivalent,other])
    approve(store,candidate,supersedes=[old,equivalent])
    assert record(store,other)['status']=='active'
    assert record(store,global_pref)['status']=='active'


def test_user_scope_conflicts_across_source_projects(store):
    old=publish(store,scope='user'); approve(store,old)
    new=publish(store,'48小时',scope='user',project='beta')
    with pytest.raises(ValueError): approve(store,new)
    approve(store,new,supersedes=[old],project_key='beta')
    assert record(store,old)['superseded_by']==new


@pytest.mark.parametrize('action',['delete','reject','archive'])
def test_removed_successor_never_resurrects_predecessor(store,action):
    old=publish(store); approve(store,old)
    new=publish(store,'48小时'); approve(store,new,supersedes=[old])
    if action=='delete':
        store.remove_many(agent_id='human-review',project_key='alpha',knowledge_ids=[new])
    elif action=='reject':
        store.feedback(agent_id='human-review',knowledge_id=new,outcome='rejected',
            evidence_kind='user_approval',evidence_ref='test://reject',evidence_summary='错误')
    else:
        store.transit(new,to_status='archived',reason='test',actor='human-review')
    assert not search(store)
    with pytest.raises(ValueError): approve(store,old)
    assert not store.transit(old,to_status='active',reason='attempt',actor='test')['changed']
    gov=Governor(store,policy={'lfhv_resurrect_threshold':2})
    for query in ('审核期限','历史审核期限','请解释审核期限'):
        gov.shadow_probe(project_key='alpha',query=query)
    assert old not in {r['knowledge_id'] for r in gov.lfhv_report(project_key='alpha')['false_kills']}


def test_automatic_and_objective_evidence_cannot_silently_replace(store):
    old=publish(store); approve(store,old)
    new=publish(store,'48小时',title='另一个标题')
    assert approve(store,new,require_no_related=True)['status']=='needs_review'
    with pytest.raises(ValueError): approve(store,new,require_no_related=True,supersedes=[old])
    with pytest.raises(ValueError):
        store.feedback(agent_id='codex',knowledge_id=new,outcome='verified',
            evidence_kind='test',evidence_ref='test://pass',evidence_summary='pass',supersedes=[old])


def test_concurrent_replacements_reject_stale_selection(store):
    old=publish(store); approve(store,old)
    keys=[publish(store,f'{h}小时') for h in (48,72)]
    def replace(key):
        try: approve(store,key,supersedes=[old]); return True
        except ValueError: return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(replace,keys))==1
    assert sum(record(store,k)['status']=='active' for k in keys)==1


def test_retry_and_search_write_race_revalidate_versions(store):
    old=publish(store); approve(store,old)
    reuse=ReuseStore(store.database_path)
    snapshot=search(store)
    args=dict(agent_id='codex',project_key='alpha',session_id='version-test',
        prompt='审核期限',records=snapshot,retrieval_ms=0,validate_live_records=True)
    _,context,_=reuse.start(**args,turn_id='before')
    assert context
    new=publish(store,'48小时'); approve(store,new,supersedes=[old])
    assert reuse.start(**args,turn_id='before')[1]==''
    assert reuse.start(**args,turn_id='race')[1]==''


@pytest.mark.parametrize('source,target',[('claude-code','codex'),('codex','claude-code')])
def test_real_adapter_current_and_historical_recall(store,tmp_path,source,target):
    old=publish(store,agent=source); approve(store,old)
    new=publish(store,'48小时',agent=source); approve(store,new,supersedes=[old])
    adapter_cls = CodexLearningAdapter if target=='codex' else ClaudeLearningAdapter
    adapter=adapter_cls(database_path=store.database_path,agent_id=target,
        project_key='alpha',reviewer=lambda _: {'proposals':[]})
    for turn,query,expected in [('current','审核期限',{new}),('history','查看历史审核期限',{old,new})]:
        payload={'session_id':'v-test','turn_id':turn,'prompt':query,'cwd':str(tmp_path)}
        adapter.recall(payload)
        trace=adapter.reuse.existing(target,'alpha','v-test',turn)
        got={r['knowledge_id'] for r in json.loads(trace['items_json']) if r['emitted']}
        assert got==expected
        assert adapter.reuse.live_context(trace)
        if turn=='history': assert '历史版本：已被替代' in trace['context_text']


def test_migration_conflicts_are_quarantined_from_recall_and_can_be_resolved(store):
    old=publish(store); other=publish(store,'48小时')
    with store._connect() as db:
        db.execute("UPDATE knowledge_records SET status='active', claim_topic='',claim_value='',claim_fingerprint='',claim_conflicted=0")
        db.execute('DELETE FROM knowledge_migrations')
        initialize(db)
    assert not search(store)
    approve(store,other,supersedes=[old])
    assert [r['id'] for r in search(store)]==[other]
    KnowledgeStore(store.database_path)
    assert record(store,old)['superseded_by']==other


def test_ordinary_archived_knowledge_is_not_history_loophole(store):
    key=publish(store); approve(store,key)
    store.transit(key,to_status='archived',reason='unused',actor='test')
    assert not search(store,'查看历史审核期限')


def test_dormant_accepted_value_also_requires_explicit_replacement(store):
    old=publish(store); approve(store,old)
    store.transit(old,to_status='archived',reason='unused',actor='test')
    new=publish(store,'48小时')
    with pytest.raises(ValueError): approve(store,new)
    approve(store,new,supersedes=[old])
    store.transit(new,to_status='archived',reason='unused',actor='test')
    assert not store.transit(old,to_status='active',reason='restore',actor='test')['changed']


def test_parallel_initialization_is_idempotent(store):
    old=publish(store); approve(store,old)
    with ThreadPoolExecutor(max_workers=4) as pool:
        copies=list(pool.map(lambda _: KnowledgeStore(store.database_path),range(8)))
    assert all(record(s,old)['claim_topic']=='审核期限' for s in copies)


def test_migrate_pre_version_schema_concurrently_preserves_content(store):
    old=publish(store); approve(store,old)
    with store._connect() as db:
        for index in ('idx_claim_slot','idx_claim_user','idx_superseded_by'):
            db.execute(f'DROP INDEX {index}')
        for column in ('claim_topic','claim_value','claim_fingerprint','claim_conflicted',
                       'superseded_by','valid_from','valid_until'):
            db.execute(f'ALTER TABLE knowledge_records DROP COLUMN {column}')
        db.execute('DROP TABLE knowledge_migrations')
    with ThreadPoolExecutor(max_workers=4) as pool:
        copies=list(pool.map(lambda _: KnowledgeStore(store.database_path),range(4)))
    assert all(record(s,old)['claim_topic']=='审核期限' for s in copies)
    assert record(store,old)['content']=='审核期限正式定为24小时。'
    assert record(store,old)['status']=='active'


def test_modified_conflict_cannot_be_replaced_using_stale_selection(store):
    old=publish(store); approve(store,old)
    new=publish(store,'48小时')
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET content=? WHERE id=?',('This text changed externally.',old))
    with pytest.raises(ValueError,match='原文已改变'): approve(store,new,supersedes=[old])
    assert record(store,old)['superseded_by'] is None


def test_explicit_replacement_list_must_not_be_silently_ignored(store):
    old=publish(store); approve(store,old)
    new=publish(store,'1天')
    with pytest.raises(ValueError,match='变化'):
        approve(store,new,supersedes=[old])
    assert record(store,old)['status']=='active'


def test_content_edit_invalidates_slot_and_cached_context(store):
    key=publish(store); approve(store,key)
    reuse=ReuseStore(store.database_path)
    reuse.start(agent_id='codex',project_key='alpha',session_id='s',turn_id='t',
        prompt='审核期限',records=search(store),retrieval_ms=0,validate_live_records=True)
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET content=? WHERE id=?',('审核期限为99小时。',key))
    assert not search(store)
    assert not reuse.live_context(reuse.existing('codex','alpha','s','t'))
    with pytest.raises(ValueError): approve(store,key)


def test_http_explicit_replacement_contract(store):
    old=publish(store); approve(store,old)
    new=publish(store,'48小时')
    with TestClient(create_app(database_path=store.database_path,api_token='test-token',
                              reviewer=lambda _: {'proposals':[]})) as client:
        args=dict(agent_id='human-review',project_key='alpha',knowledge_id=new,outcome='verified',
            evidence_summary='人工确认新版',evidence_kind='user_approval',evidence_ref='test://approval')
        headers={'Authorization':'Bearer test-token'}
        assert client.post('/v1/knowledge/feedback',json=args,headers=headers).status_code==422
        result=client.post('/v1/knowledge/feedback',json={**args,'supersedes':[old]},headers=headers)
        assert result.status_code==200, result.text
        assert record(store,old)['superseded_by']==new

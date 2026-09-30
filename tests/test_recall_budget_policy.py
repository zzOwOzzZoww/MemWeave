"""Fixed-budget recall must protect direct evidence and allow useful recovery."""
import pytest

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.retrieval_pipeline import Candidate, arbitrate, truncate, order_recovery_candidates


def candidate(key, origin='direct'):
    return Candidate({'id': key, 'title': key, 'rank': 0.0}, origin)


def test_full_primary_page_is_not_displaced_by_siblings():
    direct = [candidate(f'd{i}') for i in range(5)]
    siblings = [candidate(f's{i}', 'sibling') for i in range(3)]
    selected, omitted = truncate(arbitrate((*direct, *siblings), [], 5), limit=5, slack=2)
    assert [c.id for c in selected[:5]] == [c.id for c in direct]
    assert len([c for c in selected if c.origin == 'sibling']) == 1
    assert {r['knowledge_id'] for r in omitted} == {'s1', 's2'}


def test_query_matched_anchors_have_priority_over_inferred_siblings():
    direct = [candidate(f'd{i}') for i in range(3)]
    additions = [candidate('s', 'sibling'), candidate('a1', 'anchored'), candidate('a2', 'anchored')]
    selected, _ = truncate(arbitrate((*direct, *additions), [], 3), limit=3, slack=2)
    assert [c.id for c in selected] == ['d0', 'd1', 'd2', 'a1', 'a2']


def test_zero_slack_does_not_let_siblings_replace_direct_evidence():
    direct = [candidate('d1'), candidate('d2')]
    selected, _ = truncate(arbitrate((*direct, candidate('s', 'sibling')), [], 2), limit=2, slack=0)
    assert [c.id for c in selected] == ['d1', 'd2']


def test_no_recovery_does_not_rescore_the_ordinary_path(monkeypatch):
    def unexpected(*args):
        pytest.fail('No archive candidate should mean no additional evidence scoring')
    monkeypatch.setattr('agent_knowledge_bridge.retrieval_pipeline.evidence_support', unexpected)
    rows = [{'id': 'ordinary'}]
    assert order_recovery_candidates(rows, [], query='fixture') == rows


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv('MW_RECALL_LIMIT', '1')
    monkeypatch.setenv('MW_LFHV_PROBE', '1')
    monkeypatch.setenv('MW_LFHV_RECOVERY', '1')
    result = ClaudeLearningAdapter(database_path=tmp_path / 'recall.db', agent_id='codex',
                                   project_key='p', reviewer=lambda _: {'proposals': []})
    result.store.knowledge.register_agent(agent_id='codex', display_name='Codex', adapter_type='codex-hook')
    return result


QUERY = 'nova-svc retry backoff budget'


def publish(adapter, title, content, *, archived=False, approved=True, project='p', scope='project'):
    store = adapter.store.knowledge
    key = store.publish(source_agent='claude-code', project_key=project, title=title,
        content=content, knowledge_type='fact', scope=scope,
        evidence_summary='isolated fixture')['knowledge']['id']
    if approved:
        store.feedback(agent_id='human', knowledge_id=key, outcome='verified',
            evidence_kind='user_approval', evidence_ref='fixture', evidence_summary='checked fixture')
    if archived:
        store.transit(key, to_status='archived', actor='test', reason='fixture retirement')
    return key


def full_page(adapter):
    for i in range(3):
        publish(adapter, f'nova-svc retry note {i}', f'backoff budget belongs to staging note {i}.')
    result = adapter.store.knowledge.search(requester_agent='codex', project_key='p', query=QUERY, limit=1)
    assert len(result['results']) == 3
    return [r['id'] for r in result['results']]


def strong_archive(adapter, **kwargs):
    return publish(adapter, QUERY, 'nova-svc retry uses backoff within the recorded budget.',
                   archived=True, **kwargs)


def recall(adapter, *, turn='one'):
    return adapter.recall({'prompt': QUERY, 'session_id': 'fixture', 'turn_id': turn})


def emitted(adapter):
    return [i['knowledge_id'] for i in adapter.reuse.list('p')[0]['items'] if i['emitted']]


def live(adapter, key):
    return adapter.store.knowledge.get(requester_agent='codex', knowledge_id=key)['knowledge']


def test_stronger_archive_can_compete_with_a_full_page(adapter):
    ordinary = full_page(adapter)
    key = strong_archive(adapter)
    response = recall(adapter)
    assert key in emitted(adapter)
    assert emitted(adapter)[0] == key
    assert len(emitted(adapter)) == 3
    assert len(set(ordinary) & set(emitted(adapter))) == 2
    assert key in response['hookSpecificOutput']['additionalContext']
    assert live(adapter, key)['status'] == 'active'
    assert live(adapter, key)['hit_count'] == 1


def test_equal_evidence_preserves_ordinary_page_but_still_probes(adapter):
    ordinary = full_page(adapter)
    key = publish(adapter, 'nova-svc retry appendix', 'backoff budget belongs to the staging appendix.', archived=True)
    recall(adapter)
    assert emitted(adapter) == ordinary
    assert live(adapter, key)['status'] == 'archived'
    with adapter.store.knowledge._connect() as db:
        assert db.execute('SELECT probe_count FROM shadow_probe_stats WHERE project_key=?', ('p',)).fetchone()[0] == 1


def test_strong_ordinary_evidence_keeps_priority_over_equal_archive(adapter):
    ordinary = publish(adapter, QUERY, 'nova-svc retry backoff budget was validated for active use.')
    full_page(adapter)
    key = strong_archive(adapter)
    recall(adapter)
    assert emitted(adapter)[0] == ordinary
    assert key in emitted(adapter)
    assert len(emitted(adapter)) == 3


def test_absent_archive_skips_recovery_search(adapter, monkeypatch):
    ordinary = full_page(adapter)
    def unexpected(**kwargs):
        pytest.fail('No in-scope archives should mean no recovery search')
    monkeypatch.setattr(Governor, 'prepare_recovery', unexpected)
    recall(adapter)
    assert emitted(adapter) == ordinary


def test_unused_archive_candidates_stay_archived_under_fixed_row_budget(adapter):
    full_page(adapter)
    keys = [publish(adapter, QUERY + f' appendix {i}', QUERY + f' validated appendix {i}.', archived=True)
            for i in range(12)]
    recall(adapter)
    outgoing = set(emitted(adapter))
    assert len(outgoing) == 3
    assert outgoing <= set(keys)
    for key in keys:
        row = live(adapter, key)
        assert row['status'] == ('active' if key in outgoing else 'archived')
        assert row['hit_count'] == int(key in outgoing)


def test_no_context_budget_means_no_restoration(adapter, monkeypatch):
    full_page(adapter)
    key = strong_archive(adapter)
    start = adapter.reuse.start
    monkeypatch.setattr(adapter.reuse, 'start', lambda **kwargs: start(**kwargs, budget=1))
    assert recall(adapter) == {}
    assert emitted(adapter) == []
    assert live(adapter, key)['status'] == 'archived'
    assert live(adapter, key)['hit_count'] == 0


@pytest.mark.parametrize('change', ['quarantine', 'content', 'project', 'approval'])
def test_invalidated_recovery_refills_from_ordinary_results(adapter, monkeypatch, change):
    ordinary = full_page(adapter)
    key = strong_archive(adapter)
    governor = Governor(adapter.store.knowledge)
    adapter._governor = governor
    prepare = governor.prepare_recovery

    def invalidated(**kwargs):
        candidates = prepare(**kwargs)
        assert key in [r['id'] for r in candidates]
        with adapter.store.knowledge._connect() as db:
            sql = {'quarantine': "status='quarantined'", 'content': "content='changed after search'",
                   'project': "project_key='private'", 'approval': 'verified_count=0'}[change]
            db.execute('UPDATE knowledge_records SET ' + sql + ' WHERE id=?', (key,))
        return candidates

    monkeypatch.setattr(governor, 'prepare_recovery', invalidated)
    recall(adapter)
    assert emitted(adapter) == ordinary
    with adapter.store.knowledge._connect() as db:
        assert db.execute('SELECT hit_count FROM knowledge_records WHERE id=?', (key,)).fetchone()[0] == 0


def test_invalidated_recovery_returns_character_budget_to_normal_page(adapter, monkeypatch):
    ordinary = full_page(adapter)
    baseline = recall(adapter, turn='baseline')['hookSpecificOutput']['additionalContext']
    key = publish(adapter, QUERY, QUERY + '. ' + 'Verified context. ' * 35, archived=True)
    start = adapter.reuse.start
    monkeypatch.setattr(adapter.reuse, 'start', lambda **kwargs: start(**kwargs, budget=len(baseline)))
    governor = Governor(adapter.store.knowledge)
    adapter._governor = governor
    prepare = governor.prepare_recovery

    def invalidated(**kwargs):
        candidates = prepare(**kwargs)
        assert key in [r['id'] for r in candidates]
        adapter.store.knowledge.transit(key, to_status='quarantined', actor='test', reason='concurrent quarantine')
        return candidates

    monkeypatch.setattr(governor, 'prepare_recovery', invalidated)
    response = recall(adapter)
    assert emitted(adapter) == ordinary
    assert len(response['hookSpecificOutput']['additionalContext']) <= len(baseline)
    assert live(adapter, key)['status'] == 'quarantined'
    assert live(adapter, key)['hit_count'] == 0


def test_recovery_transaction_failure_rolls_back_and_serves_normal_page(adapter, monkeypatch):
    ordinary = full_page(adapter)
    key = strong_archive(adapter)
    restore = Governor.restore_for_reuse

    def fail_after_restore(self, **kwargs):
        assert restore(self, **kwargs) == 'restored'
        raise RuntimeError('fixture transaction failure')

    monkeypatch.setattr(Governor, 'restore_for_reuse', fail_after_restore)
    recall(adapter)
    assert emitted(adapter) == ordinary
    assert live(adapter, key)['status'] == 'archived'
    assert live(adapter, key)['hit_count'] == 0


@pytest.mark.parametrize('boundary', ['unapproved', 'other-project', 'opt-out', 'disabled', 'superseded'])
def test_full_page_recovery_keeps_existing_gates(adapter, boundary):
    ordinary = full_page(adapter)
    options = {'approved': False} if boundary == 'unapproved' else {'project': 'private'} if boundary == 'other-project' else {}
    key = strong_archive(adapter, **options)
    if boundary == 'disabled':
        adapter.store.knowledge.disable_agent('codex')
    if boundary == 'superseded':
        with adapter.store.knowledge._connect() as db:
            db.execute('UPDATE knowledge_records SET superseded_by=? WHERE id=?', (ordinary[0], key))
    prompt = 'Do not use any memory. ' + QUERY if boundary == 'opt-out' else QUERY
    response = adapter.recall({'prompt': prompt, 'session_id': 'boundary', 'turn_id': 'one'})
    if boundary in {'opt-out', 'disabled'}:
        assert response == {}
    else:
        assert emitted(adapter) == ordinary
    assert live(adapter, key)['status'] == 'archived'
    assert live(adapter, key)['hit_count'] == 0


def test_repeated_turn_does_not_restore_or_count_hits_twice(adapter):
    full_page(adapter)
    key = strong_archive(adapter)
    first = recall(adapter)
    assert key in emitted(adapter)
    assert recall(adapter) == first
    assert live(adapter, key)['hit_count'] == 1
    with adapter.store.knowledge._connect() as db:
        assert db.execute('SELECT count(*) FROM reuse_traces').fetchone()[0] == 1
        assert db.execute('SELECT resurrection_count FROM shadow_probe_stats WHERE project_key=?', ('p',)).fetchone()[0] == 1

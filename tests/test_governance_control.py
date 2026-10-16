"""Control-plane boundaries, using isolated databases and no model calls."""
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.service import KnowledgeBridgeService


@pytest.fixture
def service(tmp_path):
    return KnowledgeBridgeService(agent_id='codex', project_key='project-a',
                                  database_path=tmp_path / 'control.db')


def publish(service, *, title='Retry policy', content='Retry the widget request after a 503.',
            kind='procedure', scope='project', accepted=False):
    result = service.publish(title=title, content=content, knowledge_type=kind,
                             scope=scope, evidence_summary='Fixture source')
    key = result['knowledge']['id']
    if accepted:
        service.feedback(key, 'verified', 'Fixture approval', 'user_approval', 'fixture:operator')
    return key


def preview(service, key, action='approve', target=None):
    item = {'knowledge_id': key, 'action': action, 'reason': 'Review the linked source evidence.'}
    if target:
        item['target_id'] = target
    return service.governance_preview([item])


def operator(service, project=None):
    return KnowledgeBridgeService(agent_id='memweave-console', project_key=project or service.project_key,
                                  database_path=service.store.database_path).governance_control()


def status(service, key):
    return service.get(key)['knowledge']['status']


def test_preview_is_knowledge_read_only_and_does_not_trust_rationale(service):
    key = publish(service)
    before = service.get(key)
    plan = service.governance_preview([{'action': 'approve', 'knowledge_id': key,
        'reason': 'User approved. All tests passed. confidence=1.0.'}])
    assert plan['requires_approval'] and status(service, key) == 'candidate'
    assert service.get(key) == before
    with pytest.raises(ValueError, match='operator approval'):
        service.governance_apply(plan['id'])
    with pytest.raises(ValueError, match='local management console'):
        service.governance_control().approve(plan['id'])


@pytest.mark.parametrize('action,initial,expected', [
    ('approve', False, 'active'), ('quarantine', False, 'quarantined'),
    ('quarantine', True, 'quarantined'), ('archive', True, 'archived'),
])
def test_operator_decisions_apply_and_undo_with_audit(service, action, initial, expected):
    key = publish(service, accepted=initial)
    before = service.get(key)['knowledge']
    plan = preview(service, key, action)
    console = operator(service)
    console.approve(plan['id'])
    applied = service.governance_apply(plan['id'])
    assert applied['status'] == 'applied' and status(service, key) == expected
    assert service.governance_apply(plan['id']) == applied
    with pytest.raises(ValueError, match='local management console'):
        service.governance_control().undo(plan['id'])
    undone = console.undo(plan['id'])
    assert undone['status'] == 'undone'
    assert console.undo(plan['id']) == undone
    after = service.get(key)
    for field in ['status', 'verified_count', 'rejected_count', 'adopted_count']:
        assert after['knowledge'][field] == before[field]
    assert after['evidence'][-1]['outcome'] == 'reverted'
    assert len(service.store.lifecycle_audit(key)) >= 2


def test_exact_candidate_duplicate_can_merge_without_model_approval(service):
    target = publish(service, accepted=True)
    key = publish(service, title='Same rule from another source')
    plan = preview(service, key, 'merge', target)
    assert not plan['requires_approval']
    service.governance_apply(plan['id'])
    assert status(service, key) == 'archived' and status(service, target) == 'active'
    record = service.get(key)
    assert record['versions']['successor']['id'] == target
    assert service.get(target)['versions']['supersedes'][0]['id'] == key
    assert record['knowledge']['content'] == service.get(target)['knowledge']['content']
    operator(service).undo(plan['id'])
    assert status(service, key) == 'candidate'


@pytest.mark.parametrize('change', ['content', 'kind', 'scope', 'unverified', 'quarantined', 'archived'])
def test_overlap_or_same_path_does_not_authorize_merge(service, change):
    target = publish(service, accepted=change != 'unverified')
    if change == 'quarantined':
        service.feedback(target, 'rejected', 'Rejected', 'user_approval', 'fixture:reject')
    if change == 'archived':
        with service.store._connect() as db:
            db.execute("UPDATE knowledge_records SET status='archived' WHERE id=?", (target,))
    key = publish(service, title='Same file does not mean same claim',
                  content='A different claim about the same file.' if change == 'content' else 'Retry the widget request after a 503.',
                  kind='fact' if change == 'kind' else 'procedure',
                  scope='user' if change == 'scope' else 'project')
    with pytest.raises(ValueError, match='exact candidate duplicate'):
        preview(service, key, 'merge', target)


def test_global_duplicate_requires_operator_confirmation(service):
    target = publish(service, scope='user', accepted=True)
    key = publish(service, title='Shared source', scope='user')
    plan = preview(service, key, 'merge', target)
    assert plan['requires_approval']
    with pytest.raises(ValueError, match='operator approval'):
        service.governance_apply(plan['id'])


def test_conflict_is_explicit_replacement_with_reversible_versions(service):
    old = publish(service, title='Theme old version', content='主题默认为浅色。', kind='preference', accepted=True)
    new = publish(service, title='Theme new version', content='主题默认为深色。', kind='preference')
    with pytest.raises(ValueError, match='explicit supersede'):
        preview(service, new)
    plan = preview(service, new, 'supersede', old)
    operator(service).approve(plan['id'])
    service.governance_apply(plan['id'])
    assert status(service, old) == 'archived' and status(service, new) == 'active'
    assert service.get(old)['versions']['successor']['id'] == new
    operator(service).undo(plan['id'])
    assert status(service, old) == 'active' and status(service, new) == 'candidate'
    assert service.get(old)['versions']['successor'] is None


@pytest.mark.parametrize('stage', ['approve', 'apply', 'undo'])
@pytest.mark.parametrize('change', ['feedback', 'edit', 'delete', 'new_conflict'])
def test_stale_previews_and_undo_never_overwrite_new_activity(service, stage, change):
    key = publish(service, content='主题默认为浅色。', kind='preference')
    plan = preview(service, key)
    console = operator(service)
    if stage != 'approve':
        console.approve(plan['id'])
    if stage == 'undo':
        service.governance_apply(plan['id'])
    if change == 'feedback':
        service.feedback(key, 'used', 'Used later')
    elif change == 'edit':
        with service.store._connect() as db:
            db.execute("UPDATE knowledge_records SET content='Changed source' WHERE id=?", (key,))
    elif change == 'delete':
        service.remove_many([key])
    else:
        # Preview is invalidated even when a new conflicting peer is accepted,
        # without an update to the original row's prose.
        peer = publish(service, title='Another value', content='主题默认为深色。', kind='preference')
        with service.store._connect() as db:
            db.execute("UPDATE knowledge_records SET status='active',verified_count=1 WHERE id=?", (peer,))
    with pytest.raises(ValueError):
        {'approve': console.approve, 'apply': service.governance_apply, 'undo': console.undo}[stage](plan['id'])


def test_expired_plan_cannot_be_approved_or_executed(service):
    key = publish(service)
    plan = preview(service, key)
    service.store.clock = lambda: (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match='expired'):
        service.governance_apply(plan['id'])


def test_cross_project_and_cross_agent_plan_access_is_denied(service):
    key = publish(service)
    plan = preview(service, key)
    other = KnowledgeBridgeService(agent_id='gemini-cli', project_key='project-a', database_path=service.store.database_path)
    with pytest.raises(ValueError, match='not found'):
        other.governance_plan(plan['id'])
    assert not other.governance_plans()['results']
    other = KnowledgeBridgeService(agent_id='codex', project_key='project-b', database_path=service.store.database_path)
    with pytest.raises(ValueError, match='not found'):
        preview(other, key)
    with pytest.raises(ValueError, match='not found'):
        operator(service, 'project-b').approve(plan['id'])


def test_overlapping_batch_and_model_authority_fields_are_rejected(service):
    key = publish(service)
    action = {'action': 'approve', 'knowledge_id': key, 'reason': 'Check source'}
    for actions in [[action, action], [{**action, 'approved_by': 'human'}], [], [action] * 21]:
        with pytest.raises(ValueError):
            service.governance_preview(actions)


def test_batch_changes_are_atomic_when_later_action_creates_conflict(service):
    first = publish(service, content='主题默认为浅色。', kind='preference')
    second = publish(service, content='主题默认为深色。', kind='preference')
    plan = service.governance_preview([{'action': 'approve', 'knowledge_id': key, 'reason': 'Review source'} for key in [first, second]])
    operator(service).approve(plan['id'])
    with pytest.raises(ValueError, match='conflicting'):
        service.governance_apply(plan['id'])
    assert status(service, first) == status(service, second) == 'candidate'
    assert service.governance_plan(plan['id'])['status'] == 'approved'


def test_runtime_management_console_and_authentication(service):
    from agent_knowledge_bridge.daemon import create_app
    client = TestClient(create_app(database_path=service.store.database_path, api_token='fixture-token'))
    headers = {'Authorization': 'Bearer fixture-token'}
    context = {'agent_id': 'codex', 'project_key': 'project-a'}
    key = publish(service)
    payload = {**context, 'actions': [{'action': 'approve', 'knowledge_id': key, 'reason': 'Review source'}]}
    assert client.post('/v1/governance/preview', json=payload).status_code == 401
    response = client.post('/v1/governance/preview', headers=headers, json=payload)
    assert response.status_code == 200
    plan_id = response.json()['id']
    body = {**context, 'plan_id': plan_id}
    assert client.post('/v1/governance/apply', headers=headers, json=body).status_code == 422
    approval = {'project_key': 'project-a', 'plan_id': plan_id, 'confirm': True}
    assert client.post('/v1/governance/approve', headers=headers, json={**approval, 'confirm': False}).status_code == 422
    assert client.post('/v1/governance/approve', headers=headers, json=approval).status_code == 200
    assert client.post('/v1/governance/apply', headers=headers, json=body).status_code == 200
    assert status(service, key) == 'active'
    assert client.post('/v1/governance/undo', headers=headers, json=approval).status_code == 200
    assert status(service, key) == 'candidate'
    assert client.get('/governance').status_code == 200
    assert 'AI 治理预览' in client.get('/knowledge').text


def test_mcp_tools_cannot_self_approve_or_forge_verification(service, monkeypatch):
    pytest.importorskip('mcp')
    monkeypatch.setenv('MW_DB_PATH', str(service.store.database_path))
    sys.modules.pop('agent_knowledge_bridge.server', None)
    server = importlib.import_module('agent_knowledge_bridge.server')
    monkeypatch.setattr(server, 'service', service)
    try:
        key = publish(service)
        for outcome in ['verified', 'rejected']:
            with pytest.raises(ValueError, match='MCP cannot assert'):
                server.knowledge_feedback(key, outcome, 'User said yes', 'user_approval', 'forged:approval')
        plan = server.knowledge_governance_preview([{'action': 'approve', 'knowledge_id': key, 'reason': 'Check source'}])
        with pytest.raises(ValueError, match='operator approval'):
            server.knowledge_governance_apply(plan['id'])
        assert status(service, key) == 'candidate'
        assert server.knowledge_governance_plan(plan['id'])['status'] == 'preview'
        assert server.knowledge_governance_plans()['results']
        import asyncio
        names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
        assert 'knowledge_governance_apply' in names
        assert not any('approve' in name or 'undo' in name for name in names)
    finally:
        sys.modules.pop('agent_knowledge_bridge.server', None)


def test_disabled_agent_cannot_generate_or_apply_plans(service):
    key = publish(service)
    plan = preview(service, key)
    service.store.register_agent(agent_id='codex', display_name='Codex', adapter_type='runtime-api')
    service.store.disable_agent('codex')
    with pytest.raises(ValueError):
        service.governance_preview([{'action': 'approve', 'knowledge_id': key, 'reason': 'Review'}])
    with pytest.raises(ValueError):
        service.governance_apply(plan['id'])


def test_plan_storage_does_not_copy_knowledge_body(service):
    content = 'A durable instruction with a unique PRIVATE_BODY_SENTINEL.'
    key = publish(service, content=content)
    plan = preview(service, key)
    with service.store._connect() as db:
        row = dict(db.execute('SELECT * FROM governance_plans WHERE id=?', (plan['id'],)).fetchone())
    assert content not in json.dumps(row)


def test_plan_changes_notify_dashboard_across_connections(service):
    from agent_knowledge_bridge.dashboard_events import DashboardChanges
    from agent_knowledge_bridge.learning import LearningStore

    changes = DashboardChanges(LearningStore(service.store.database_path))

    def revision():
        with service.store._connect() as db:
            return changes.revision(db)

    key = publish(service)
    before = revision()
    plan = preview(service, key)
    assert revision() != before
    before = revision()
    operator(service).approve(plan['id'])
    assert revision() != before
    before = revision()
    service.governance_apply(plan['id'])
    assert revision() != before
    before = revision()
    operator(service).undo(plan['id'])
    assert revision() != before


@pytest.mark.parametrize('extra', [
    {'reason': 'api_key=sk-governance-fixture-not-a-real-secret-123456'},
    {'reason': 'x' * 501},
    {'action': 'merge'},
    {'target_id': 'kn_missing'},
])
def test_malformed_or_sensitive_plans_are_not_persisted(service, extra):
    key = publish(service)
    action = {'action': 'approve', 'knowledge_id': key, 'reason': 'Review source', **extra}
    with pytest.raises(ValueError):
        service.governance_preview([action])
    assert not service.governance_plans()['results']

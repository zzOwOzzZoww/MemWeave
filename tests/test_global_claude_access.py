from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_knowledge_bridge import provider, runtime_state
from agent_knowledge_bridge.agent_registry import install_native_hook
from agent_knowledge_bridge.store import KnowledgeStore


ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'shared-project'
PROMPT = 'LFHV positioning'
CONTENT = 'LFHV positioning is on-demand reuse with synchronous revival.'


@pytest.fixture
def shared(tmp_path, monkeypatch):
    home = tmp_path / 'memweave-home'
    monkeypatch.setenv('MEMWEAVE_HOME', str(home))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    monkeypatch.setenv('MW_RUNTIME_MODE', 'local')
    for key in ('MW_PROJECT_KEY', 'AKB_PROJECT_KEY', 'MW_AGENT_ID', 'AKB_AGENT_ID',
                'MW_DB_PATH', 'AKB_DB_PATH', 'MW_DAEMON_URL', 'MW_DAEMON_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    database = home / 'knowledge.db'
    monkeypatch.setenv('MW_DB_PATH', str(database))
    provider.atomic_json(home / 'config.json', {
        'default_project': PROJECT,
        'database_path': str(database),
        'agent_projects': {'claude-code': PROJECT},
    })
    monkeypatch.setattr(runtime_state, 'state_paths', lambda: [home / 'runtime-state.json'])
    store = KnowledgeStore(database)
    for agent, adapter in (('codex', 'codex-hook'), ('claude-code', 'claude-hook')):
        store.register_agent(agent_id=agent, display_name=agent, adapter_type=adapter)
    row = store.publish(source_agent='codex', project_key=PROJECT, title=PROMPT,
                        content=CONTENT, knowledge_type='fact', scope='project',
                        evidence_summary='fixture', subject_terms=['lfhv'])['knowledge']
    store.feedback(agent_id='human', knowledge_id=row['id'], outcome='verified',
                   evidence_kind='user_approval', evidence_ref='review', evidence_summary='approved')
    return home, store, row['id']


def run_claude_hook(payload):
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / 'claude_learning_hook.py'), 'hook'],
        input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True,
        env=os.environ.copy(), timeout=20, check=True,
    )
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('directory', ['launchers', 'arbitrary-folder', 'another-repo'])
def test_claude_global_hook_emits_codex_knowledge_outside_memweave(shared, tmp_path, directory):
    _, store, knowledge_id = shared
    cwd = tmp_path / directory
    cwd.mkdir()
    if directory == 'another-repo':
        (cwd / '.git').mkdir()
    response = run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(cwd),
                               'prompt': PROMPT, 'session_id': 'test-session', 'turn_id': 'test-turn'})
    context = response['hookSpecificOutput']['additionalContext']
    assert CONTENT in context
    assert 'source=codex' in context
    with store._connect() as db:
        trace = db.execute('SELECT project_key,context_text,workspace FROM reuse_traces').fetchone()
        assert trace['project_key'] == PROJECT
        assert trace['context_text'] == context
        assert trace['workspace'] == str(cwd.resolve())
        assert db.execute('SELECT hit_count FROM knowledge_records WHERE id=?', (knowledge_id,)).fetchone()[0] == 1


def test_shared_project_does_not_disable_workspace_mapping(shared, tmp_path):
    home, _, _ = shared
    isolated = tmp_path / 'private-project'
    isolated.mkdir()
    config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    config['workspace_projects'] = {str(isolated): 'private-project'}
    provider.atomic_json(home / 'config.json', config)
    assert runtime_state.project_key(cwd=str(isolated), agent='claude-code') == 'private-project'
    assert run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(isolated),
                           'prompt': PROMPT, 'session_id': 'private-session'}) == {}


def test_shared_project_does_not_disable_environment_override(shared, tmp_path, monkeypatch):
    monkeypatch.setenv('MW_PROJECT_KEY', 'explicit-private')
    assert runtime_state.project_key(cwd=str(tmp_path), agent='claude-code') == 'explicit-private'
    assert run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(tmp_path),
                           'prompt': PROMPT, 'session_id': 'private-session'}) == {}


def test_all_agents_default_to_global_pool_without_directory_binding(shared, tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    assert runtime_state.project_key(cwd=str(a), agent='claude-code') == PROJECT
    assert runtime_state.project_key(cwd=str(b), agent='claude-code') == PROJECT
    assert runtime_state.project_key(agent='claude-code') == PROJECT
    for agent in ('codex', 'cursor', 'custom-reviewer'):
        assert runtime_state.project_key(cwd=str(a), agent=agent) == PROJECT
        assert runtime_state.project_key(cwd=str(b), agent=agent) == PROJECT


@pytest.mark.parametrize('status', ['candidate', 'quarantined'])
def test_global_claude_still_obeys_admission(shared, tmp_path, status):
    _, store, knowledge_id = shared
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET status=? WHERE id=?', (status, knowledge_id))
    assert run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(tmp_path),
                           'prompt': PROMPT, 'session_id': 'guard-session'}) == {}


def test_global_claude_still_obeys_disable(shared, tmp_path):
    _, store, _ = shared
    store.disable_agent('claude-code')
    assert run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(tmp_path),
                           'prompt': PROMPT, 'session_id': 'disabled-session'}) == {}
    with store._connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reuse_traces'").fetchone() is None


def test_global_claude_unrelated_prompt_stays_empty(shared, tmp_path):
    assert run_claude_hook({'hook_event_name': 'UserPromptSubmit', 'cwd': str(tmp_path),
                           'prompt': 'Weather forecast for tomorrow', 'session_id': 'unrelated-session'}) == {}


def test_stop_uses_same_shared_project_as_recall(shared, tmp_path):
    _, store, _ = shared
    transcript = tmp_path / 'turn.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': PROMPT}}),
                          encoding='utf-8')
    assert run_claude_hook({'hook_event_name': 'Stop', 'cwd': str(tmp_path),
                           'session_id': 'learning-session', 'transcript_path': str(transcript)}) == {}
    with store._connect() as db:
        payload = json.loads(db.execute('SELECT payload FROM learning_jobs').fetchone()[0])
        assert payload['project_key'] == PROJECT
        assert payload['agent_id'] == 'claude-code'


def test_runtime_route_uses_agent_shared_project(shared, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agent_knowledge_bridge.daemon import create_app
    from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
    from agent_knowledge_bridge.hooks import claude_learning_hook as hook
    _, store, _ = shared
    app = create_app(database_path=store.database_path, api_token='test-only', reviewer=lambda _: {'proposals': []})
    runtime = MemWeaveRuntimeClient(base_url='http://testserver', token='test-only',
                                   agent_id='claude-code', project_key='initial-wrong-project')
    with TestClient(app) as client:
        def post(path, payload):
            response = client.post(path, json=payload, headers={'Authorization': 'Bearer test-only'})
            response.raise_for_status()
            return response.json()
        monkeypatch.setattr(runtime, '_post', post)
        monkeypatch.setattr(hook, 'selected_runtime', lambda: runtime)
        import io
        payload = {'hook_event_name': 'UserPromptSubmit', 'cwd': str(tmp_path),
                   'prompt': PROMPT, 'session_id': 'runtime-session'}
        monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
        output = io.StringIO()
        monkeypatch.setattr(sys, 'stdout', output)
        assert hook.run_hook() == 0
        response = json.loads(output.getvalue())
        assert CONTENT in response['hookSpecificOutput']['additionalContext']
        assert runtime.project_key == PROJECT


def test_rejoining_claude_persists_global_pool_preserves_config_and_is_idempotent(shared, tmp_path):
    home, _, _ = shared
    config_path = home / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config.pop('agent_projects')
    config['workspace_projects'] = {str(tmp_path / 'private'): 'private'}
    config['custom_setting'] = 'keep'
    provider.atomic_json(config_path, config)
    settings_path = tmp_path / 'claude' / 'settings.json'
    first = install_native_hook('claude-code', config_path=settings_path)
    assert first['scope_changed']
    installed = json.loads(config_path.read_text(encoding='utf-8'))
    assert installed['agent_projects']['claude-code'] == PROJECT
    assert installed['workspace_projects'] == config['workspace_projects']
    assert installed['custom_setting'] == 'keep'
    assert json.loads((home / 'config.json.memweave-backup').read_text(encoding='utf-8')) == config
    before = config_path.read_bytes()
    second = install_native_hook('claude-code', config_path=settings_path)
    assert not second['changed'] and not second['scope_changed']
    assert config_path.read_bytes() == before


def test_install_does_not_replace_existing_agent_pool(shared, tmp_path):
    home, _, _ = shared
    config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    config['agent_projects']['claude-code'] = 'chosen-pool'
    provider.atomic_json(home / 'config.json', config)
    result = install_native_hook('claude-code', config_path=tmp_path / 'settings.json')
    assert result['shared_project'] == 'chosen-pool'
    assert not result['scope_changed']


@pytest.mark.parametrize('setting', [[], {'claude-code': ''}, {'claude-code': 123}])
def test_invalid_shared_project_fails_closed(shared, tmp_path, setting):
    home, _, _ = shared
    provider.atomic_json(home / 'config.json', {'agent_projects': setting})
    with pytest.raises(ValueError):
        runtime_state.project_key(cwd=str(tmp_path), agent='claude-code')


@pytest.mark.parametrize('agent', ['claude-code', 'codex'])
@pytest.mark.parametrize('directory', ['launcher-folder', 'unrelated-repo'])
def test_installed_global_launcher_runs_from_any_directory(shared, tmp_path, agent, directory):
    home, store, _ = shared
    source = 'codex' if agent == 'claude-code' else 'claude-code'
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET source_agent=?', (source,))
    install = install_native_hook(agent)
    assert install['shared_project'] == PROJECT
    cwd = tmp_path / directory
    cwd.mkdir()
    if directory.endswith('repo'):
        (cwd / '.git').mkdir()
    marker = 'claude_learning_hook.py' if agent == 'claude-code' else 'codex_learning_hook.py'
    result = subprocess.run([sys.executable, '-X', 'utf8', str(home / 'launchers' / marker), 'hook'],
        cwd=cwd, input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'cwd': str(cwd),
                                  'prompt': PROMPT, 'session_id': 'isolated-launcher-test'}),
        text=True, encoding='utf-8', capture_output=True, timeout=20, check=True, env=os.environ.copy())
    assert not result.stderr
    context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
    assert CONTENT in context
    assert f'source={source}' in context
    with store._connect() as db:
        assert db.execute('SELECT project_key FROM reuse_traces').fetchone()[0] == PROJECT


@pytest.mark.parametrize('agent,adapter', [('claude-code', 'claude-hook'), ('codex', 'codex-hook'),
                                        ('cursor', 'runtime-api'), ('custom-agent', 'custom')])
def test_registration_uses_user_global_scope_for_every_adapter(shared, agent, adapter):
    from fastapi.testclient import TestClient
    from agent_knowledge_bridge.daemon import create_app
    home, store, _ = shared
    with TestClient(create_app(database_path=store.database_path, api_token='test-only')) as client:
        response = client.post('/v1/agents/register', headers={'Authorization': 'Bearer test-only'},
            json={'agent_id': agent, 'display_name': agent, 'adapter_type': adapter})
        assert response.status_code == 200
        hook = response.json()['agent']['hook']
        assert hook['scope'] == 'user-global'
        assert hook['shared_project'] == PROJECT
        assert hook['configured'] == (adapter in ('claude-hook', 'codex-hook'))
    assert json.loads((home / 'config.json').read_text(encoding='utf-8'))['agent_projects'][agent] == PROJECT


def test_runtime_client_defaults_to_shared_pool_preserves_explicit_project(shared, tmp_path, monkeypatch):
    from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
    options = {'base_url': 'http://testserver', 'token': 'test-only', 'agent_id': 'custom-agent'}
    client = MemWeaveRuntimeClient(**options)
    assert client.project_key == PROJECT
    explicit = MemWeaveRuntimeClient(**options, project_key='private')
    assert explicit.project_key == 'private'
    home, _, _ = shared
    config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    config['workspace_projects'] = {str(tmp_path / 'private-repo'): 'isolated'}
    provider.atomic_json(home / 'config.json', config)
    monkeypatch.setattr(client, '_post', lambda path, payload: payload)
    monkeypatch.setattr(explicit, '_post', lambda path, payload: payload)
    assert client.recall(session_id='test', prompt=PROMPT, cwd=str(tmp_path))['project_key'] == PROJECT
    assert client.recall(session_id='test', prompt=PROMPT, cwd=str(tmp_path / 'private-repo'))['project_key'] == 'isolated'
    assert explicit.recall(session_id='test', prompt=PROMPT, cwd=str(tmp_path / 'private-repo'))['project_key'] == 'private'
    with pytest.raises(ValueError):
        MemWeaveRuntimeClient(**options, project_key='')

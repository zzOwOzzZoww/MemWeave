from __future__ import annotations

import base64
import json

import pytest

from agent_knowledge_bridge.agent_registry import discover_agents, hook_configuration, install_native_hook
from agent_knowledge_bridge.integration_installation import is_owned_hook
from agent_knowledge_bridge.provider import atomic_json


@pytest.fixture(params=['claude-code', 'codex'])
def native(request, tmp_path, monkeypatch):
    agent = request.param
    home = tmp_path / 'memweave'
    agent_home = tmp_path / 'client-home'
    monkeypatch.setenv('MEMWEAVE_HOME', str(home))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(agent_home))
    monkeypatch.setenv('CODEX_HOME', str(agent_home))
    monkeypatch.setenv('MW_DB_PATH', str(tmp_path / 'test.db'))
    for key in ('MW_PROJECT_KEY', 'AKB_PROJECT_KEY'):
        monkeypatch.delenv(key, raising=False)
    atomic_json(home / 'config.json', {'default_project': 'shared'})
    settings = agent_home / ('settings.json' if agent == 'claude-code' else 'hooks.json')
    marker = 'claude_learning_hook.py' if agent == 'claude-code' else 'codex_learning_hook.py'
    return agent, settings, marker


def test_repair_all_groups_without_touching_other_hooks(native):
    agent, settings, marker = native
    other = {'type': 'command', 'command': 'other-tool', 'async': True}
    legacy = {'type': 'command', 'command': f'python {marker} hook --workspace old-folder',
              'shell': 'powershell', 'async': True, 'timeoutSec': 8}
    atomic_json(settings, {'env': {'KEEP': 'yes'}, 'hooks': {
        event: [{'matcher': 'private/*', 'hooks': [other, legacy]},
                {'matcher': 'another-folder', 'hooks': [legacy]},
                {'hooks': [{'type': 'command', 'commandWindows': marker}]}]
        for event in ('UserPromptSubmit', 'Stop')}})
    assert not hook_configuration(agent)['configured']
    first = install_native_hook(agent)
    assert first['changed']
    config = json.loads(settings.read_text(encoding='utf-8'))
    assert config['env'] == {'KEEP': 'yes'}
    for groups in config['hooks'].values():
        assert groups[0] == {'matcher': 'private/*', 'hooks': [other]}
        ours = [(group, item) for group in groups for item in group['hooks']
                if is_owned_hook(item, marker)]
        assert len(ours) == 1
        group, item = ours[0]
        assert group.get('matcher', '') == ''
        command = item['command']
        if command.startswith('powershell.exe '):
            command = base64.b64decode(command.split()[-1], validate=True).decode('utf-16le')
        assert '--workspace' not in command
        assert item['async'] is False
        assert 'shell' not in item and 'timeoutSec' not in item
    assert hook_configuration(agent)['configured']
    before = settings.read_bytes()
    second = install_native_hook(agent)
    assert not second['changed'] and not second['scope_changed']
    assert before == settings.read_bytes()


def test_discovery_install_and_status_share_the_selected_user_home(native):
    agent, settings, _ = native
    install_native_hook(agent)
    discovered = next(row for row in discover_agents() if row['agent_id'] == agent)
    assert discovered['config_path'] == str(settings.parent)
    assert hook_configuration(agent)['matched_paths'] == [str(settings)]


def test_shared_scope_is_persisted_not_overwritten_by_runtime_environment(native, monkeypatch):
    from agent_knowledge_bridge.paths import memweave_home
    agent, _, _ = native
    atomic_json(memweave_home() / 'config.json', {
        'default_project': 'shared', 'agent_projects': {agent: 'chosen-private-pool'}})
    monkeypatch.setenv('MW_PROJECT_KEY', 'runtime-default')
    install = install_native_hook(agent)
    assert install['shared_project'] == 'chosen-private-pool'
    assert not install['scope_changed']
    assert hook_configuration(agent)['shared_project'] == 'chosen-private-pool'


@pytest.mark.parametrize('defect', ['marker-only', 'non-object', 'missing-stop', 'conditional', 'workspace',
                                   'async', 'disabled', 'duplicate', 'missing-launcher'])
def test_hook_status_rejects_false_success(native, defect):
    agent, settings, marker = native
    install_native_hook(agent)
    assert hook_configuration(agent)['configured']
    config = json.loads(settings.read_text(encoding='utf-8'))
    group = config['hooks']['UserPromptSubmit'][0]
    item = group['hooks'][0]
    if defect == 'marker-only':
        config = {'description': marker}
    elif defect == 'non-object':
        config = []
    elif defect == 'missing-stop':
        config['hooks'].pop('Stop')
    elif defect == 'conditional':
        group['matcher'] = 'private/*'
    elif defect == 'workspace':
        item['command'] += ' --workspace old-folder'
    elif defect == 'async':
        item['async'] = True
    elif defect == 'disabled':
        config['disableAllHooks'] = True
    elif defect == 'duplicate':
        group['hooks'].append(dict(item))
    elif defect == 'missing-launcher':
        from agent_knowledge_bridge.paths import memweave_home
        (memweave_home() / 'launchers' / marker).unlink()
    atomic_json(settings, config)
    assert not hook_configuration(agent)['configured']

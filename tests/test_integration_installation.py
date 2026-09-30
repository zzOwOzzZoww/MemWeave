from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agent_knowledge_bridge import integration_installation as installation
from agent_knowledge_bridge.agent_registry import hook_configuration, install_native_hook
from agent_knowledge_bridge.integration_profiles import PROFILES
from agent_knowledge_bridge.integration_installation import (
    prepare_hook_installation, install_hook_plan, inspect_hook_plan,
)
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.provider import atomic_json
from agent_knowledge_bridge.runtime_learning_adapter import RuntimeLearningAdapter
from test_unified_integration import CONTENT, custom_profile, isolated, reviewer, seed


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('protocol', ['command-json', 'gemini-json'])
def test_preparation_and_inspection_are_read_only(isolated, protocol):
    _, profile = custom_profile(isolated)
    settings = isolated / 'client' / 'settings.json'
    before = snapshot(isolated)
    plan = prepare_hook_installation(profile, config_path=settings, protocol=protocol)
    assert inspect_hook_plan(plan) == {'configured': False, 'executor_ready': False}
    assert snapshot(isolated) == before


@pytest.mark.parametrize('protocol', ['command-json', 'gemini-json'])
def test_custom_plan_installs_additively_idempotently_and_runs_outside_repo(isolated, protocol):
    path, profile = custom_profile(isolated)
    store = seed(isolated)
    settings = isolated / 'client' / 'settings.json'
    other = {'type': 'command', 'command': 'other-tool', 'async': True}
    atomic_json(settings, {'env': {'KEEP': 'yes'}, 'hooks': {
        'Question': [{'matcher': 'private/*', 'hooks': [other]}]}})
    original = settings.read_bytes()
    plan = prepare_hook_installation(profile, config_path=settings, protocol=protocol)
    assert settings.read_bytes() == original
    first = install_hook_plan(plan)
    assert first['configured'] and first['changed'] and first['executor_changed']
    assert (settings.parent / 'settings.json.memweave-backup').read_bytes() == original
    config = json.loads(settings.read_text(encoding='utf-8'))
    assert config['env'] == {'KEEP': 'yes'}
    assert config['hooks']['Question'][0] == {'matcher': 'private/*', 'hooks': [other]}
    assert installation.EXECUTOR_MODULE in plan.launcher_path.read_text(encoding='utf-8')
    assert '--profile' in plan.launcher_path.read_text(encoding='utf-8')
    assert inspect_hook_plan(plan) == {'configured': True, 'executor_ready': True}
    before = snapshot(isolated)
    stamps = {item: item.stat().st_mtime_ns for item in (plan.launcher_path, plan.profile_path, settings)}
    second = install_hook_plan(plan)
    assert not second['changed'] and not second['executor_changed']
    assert snapshot(isolated) == before
    assert stamps == {item: item.stat().st_mtime_ns for item in stamps}

    # The installed executor uses its managed snapshot, not a mutable source profile.
    path.write_text('[]', encoding='utf-8')
    outside = isolated / 'unrelated repository with spaces'
    outside.mkdir()
    transcript = isolated / 'source.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 'u1',
        'message': {'role': 'user', 'content': CONTENT}}) + '\n', encoding='utf-8')
    payload = {'event': {'name': 'Question'}, 'metadata': {'session': 's', 'turn': 't',
        'cwd': str(outside), 'log': str(transcript)}, 'request': {'message': CONTENT}}

    def invoke(data, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', str(plan.launcher_path), *args], cwd=outside,
            input=json.dumps(data), text=True, encoding='utf-8', capture_output=True, timeout=20)

    response = invoke(payload, 'hook')
    assert response.returncode == 0 and not response.stderr
    assert 'Widget verifier baseline' in json.loads(response.stdout)['context']
    unrelated = invoke({**payload, 'metadata': {**payload['metadata'], 'turn': 'different'},
        'request': {'message': 'weather today'}}, 'hook')
    assert unrelated.returncode == 0 and json.loads(unrelated.stdout) == {}
    completed = invoke({**payload, 'event': {'name': 'Finished'}}, 'hook')
    assert completed.returncode == 0 and json.loads(completed.stdout) == {}
    adapter = RuntimeLearningAdapter(database_path=isolated / 'test.db', agent_id=profile.agent_id,
        project_key='shared', reviewer=reviewer)
    queue = LearningQueue(isolated / 'test.db', adapter.learn)
    assert queue.summary() == {'queued': 1} and queue.process_one()
    assert queue.summary() == {'completed': 1}
    with store._connect() as db:
        assert db.execute('SELECT status FROM knowledge_records WHERE source_agent=?',
            (profile.agent_id,)).fetchone()[0] == 'candidate'
    metrics = invoke({}, 'metrics')
    assert metrics.returncode == 0 and not metrics.stderr and isinstance(json.loads(metrics.stdout), dict)


@pytest.mark.parametrize('agent', tuple(PROFILES))
def test_native_registration_installs_only_the_generic_execution_entry(isolated, monkeypatch, agent):
    profile = PROFILES[agent]
    client = isolated / 'client'
    monkeypatch.setenv(profile.home_env, str(client))
    settings = client / profile.home_subdir / profile.config_file
    result = install_native_hook(agent)
    assert result['configured'] and result['executor'] == installation.EXECUTOR_MODULE
    plan = prepare_hook_installation(profile, config_path=settings)
    source = plan.launcher_path.read_text(encoding='utf-8')
    assert '--agent' in source and installation.EXECUTOR_MODULE in source
    assert 'hooks.' + profile.launcher[:-3] not in source
    assert hook_configuration(agent)['executor_ready']
    metrics = subprocess.run([sys.executable, str(plan.launcher_path), 'metrics'], text=True,
        encoding='utf-8', capture_output=True, timeout=20)
    assert metrics.returncode == 0 and not metrics.stderr and isinstance(json.loads(metrics.stdout), dict)


@pytest.mark.parametrize('defect', ['broken-json', 'wrong-hook-type', 'wrong-event-type', 'oversized'])
def test_invalid_config_cannot_create_installation_artifacts(isolated, defect):
    _, profile = custom_profile(isolated)
    settings = isolated / 'settings.json'
    contents = {'broken-json': '{', 'wrong-hook-type': '{"hooks":[]}',
        'wrong-event-type': '{"hooks":{"Finished":{}}}', 'oversized': ' ' * (installation.MAX_CONFIG_BYTES + 1)}
    settings.write_text(contents[defect], encoding='utf-8')
    before = snapshot(isolated)
    with pytest.raises(RuntimeError):
        prepare_hook_installation(profile, config_path=settings, protocol='command-json')
    assert snapshot(isolated) == before


def test_prepared_plan_revalidates_changed_client_config_before_writes(isolated):
    _, profile = custom_profile(isolated)
    settings = isolated / 'settings.json'
    plan = prepare_hook_installation(profile, config_path=settings, protocol='command-json')
    atomic_json(settings, {'hooks': {'Finished': {}}})
    before = snapshot(isolated)
    with pytest.raises(RuntimeError):
        install_hook_plan(plan)
    assert snapshot(isolated) == before


@pytest.mark.parametrize('defect', ['launcher', 'profile', 'timeout', 'executor'])
def test_status_rejects_stale_or_mismatched_installation_and_repair_restores_it(isolated, monkeypatch, defect):
    _, profile = custom_profile(isolated)
    plan = prepare_hook_installation(profile, config_path=isolated / 'settings.json', protocol='command-json')
    install_hook_plan(plan)
    if defect == 'launcher':
        plan.launcher_path.write_text('raise SystemExit(0)\n', encoding='utf-8')
    elif defect == 'profile':
        value = json.loads(plan.profile_path.read_text(encoding='utf-8'))
        value['agent_id'] = 'wrong-client'
        atomic_json(plan.profile_path, value)
    elif defect == 'timeout':
        value = json.loads(plan.config_path.read_text(encoding='utf-8'))
        value['hooks']['Question'][0]['hooks'][0]['timeout'] = 0
        atomic_json(plan.config_path, value)
    else:
        monkeypatch.setattr(installation, 'EXECUTOR_PATH', isolated / 'missing-executor.py')
        before = snapshot(isolated)
        assert not inspect_hook_plan(plan)['configured']
        with pytest.raises(RuntimeError, match='executor is missing'):
            install_hook_plan(plan)
        assert snapshot(isolated) == before
        return
    assert not inspect_hook_plan(plan)['configured']
    assert install_hook_plan(plan)['configured']


@pytest.mark.parametrize('protocol', ['invented', [], {}, '', None])
def test_unknown_protocol_is_not_guessed_or_installed(isolated, protocol):
    _, profile = custom_profile(isolated)
    before = snapshot(isolated)
    with pytest.raises(ValueError, match='unconfirmed'):
        prepare_hook_installation(profile, config_path=isolated / 'settings.json', protocol=protocol)
    assert snapshot(isolated) == before


def test_disabled_client_hooks_stay_disabled_after_installation(isolated):
    _, profile = custom_profile(isolated)
    settings = isolated / 'settings.json'
    atomic_json(settings, {'hooksConfig': {'enabled': False, 'disabled': ['memweave-question']}})
    plan = prepare_hook_installation(profile, config_path=settings, protocol='gemini-json')
    assert not install_hook_plan(plan)['configured']
    assert json.loads(settings.read_text(encoding='utf-8'))['hooksConfig'] == {
        'enabled': False, 'disabled': ['memweave-question']}

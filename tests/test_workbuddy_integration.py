from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.agent_registry import discover_agents, hook_configuration, install_native_hook
from agent_knowledge_bridge.codebuddy_transcript import parse_latest_codebuddy_turn
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.integration_installation import prepare_hook_installation
from agent_knowledge_bridge.integration_profiles import PROFILES
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.provider import atomic_json
from agent_knowledge_bridge.runtime_learning_adapter import RuntimeLearningAdapter
from agent_knowledge_bridge.service import KnowledgeBridgeService
from test_unified_integration import CONTENT, isolated, reviewer, seed


@pytest.fixture
def client_home(isolated, monkeypatch):
    home = isolated / 'WorkBuddy home with spaces'
    monkeypatch.setenv('WORKBUDDY_CONFIG_DIR', str(home))
    return home


def transcript(path, rows=None):
    rows = rows or [
        {'type': 'message', 'role': 'user', 'id': 'u1', 'content': [{'type': 'input_text', 'text': CONTENT}]},
        {'type': 'message', 'role': 'assistant', 'id': 'a1', 'parentId': 'u1',
         'content': [{'type': 'output_text', 'text': 'Acknowledged.'}]},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
    return path


def test_workbuddy_discovery_prepares_generic_hook_without_config_writes(client_home):
    client_home.mkdir()
    result = next(item for item in discover_agents() if item['agent_id'] == 'workbuddy')
    assert result['installed'] and result['adapter_type'] == 'protocol-hook'
    assert result['capabilities'] == ['recall', 'learn', 'shared-knowledge']
    assert result['config_path'] == str(client_home)
    assert not (client_home / 'settings.json').exists()
    assert not hook_configuration('workbuddy')['configured']


def test_old_runtime_registration_upgrades_to_unified_executor(isolated, client_home):
    settings = client_home / 'settings.json'
    other = {'type': 'command', 'command': 'other-tool'}
    atomic_json(settings, {'sandbox': {'keep': True}, 'hooks': {'Stop': [{'hooks': [other]}]}})
    original = settings.read_bytes()
    service = KnowledgeBridgeService(database_path=isolated / 'test.db', auto_install_hooks=True)
    result = service.register_agent(dict(agent_id='workbuddy', display_name='WorkBuddy', adapter_type='runtime-api'))
    assert result['adapter_type'] == 'protocol-hook'
    assert result['hook']['configured'] and not result['hook']['execution']['observed']
    assert result['hook_install']['executor'] == 'agent_knowledge_bridge.hooks.generic_learning_hook'
    saved = json.loads(settings.read_text(encoding='utf-8'))
    assert saved['sandbox'] == {'keep': True} and other in saved['hooks']['Stop'][0]['hooks']
    assert (client_home / 'settings.json.memweave-backup').read_bytes() == original
    before = settings.read_bytes()
    assert not service.register_agent(dict(agent_id='workbuddy', display_name='WorkBuddy',
        adapter_type='runtime-api'))['hook_install']['changed']
    assert settings.read_bytes() == before


@pytest.mark.parametrize('flag', ['disableAllHooks', 'allowManagedHooksOnly'])
def test_workbuddy_policy_is_not_overridden(client_home, flag):
    atomic_json(client_home / 'settings.json', {flag: True})
    assert not install_native_hook('workbuddy')['configured']
    assert json.loads((client_home / 'settings.json').read_text())[flag] is True


def test_installed_workbuddy_hook_recalls_and_queues_native_transcript(isolated, client_home):
    seed(isolated, agent='workbuddy')
    install_native_hook('workbuddy')
    plan = prepare_hook_installation(PROFILES['workbuddy'], config_path=client_home / 'settings.json')
    source = plan.launcher_path.read_text()
    assert '--agent' in source and 'generic_learning_hook' in source
    path = transcript(client_home / 'projects' / 'fixture' / 's.jsonl')
    payload = dict(hook_event_name='UserPromptSubmit', session_id='s', cwd=str(isolated),
                   prompt=CONTENT, transcript_path=str(path))
    config = json.loads(plan.config_path.read_text())
    command = config['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']

    def invoke(event):
        data = {**payload, 'hook_event_name': event}
        if os.name == 'nt':
            assert command.startswith('powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand ')
            encoded = command.rsplit(' ', 1)[-1]
            script = base64.b64decode(encoded).decode('utf-16le')
            assert str(plan.python) in script and str(plan.launcher_path) in script
            args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-EncodedCommand', encoded]
        else:
            args = [sys.executable, str(plan.launcher_path), 'hook']
        result = subprocess.run(args, input=json.dumps(data), text=True, encoding='utf-8',
            capture_output=True, cwd=client_home, timeout=25)
        assert result.returncode == 0 and not result.stderr
        return json.loads(result.stdout)

    assert 'Widget verifier baseline' in invoke('UserPromptSubmit')['hookSpecificOutput']['additionalContext']
    assert invoke('Stop') == {}
    adapter = RuntimeLearningAdapter(database_path=isolated / 'test.db', agent_id='workbuddy',
        project_key='shared', reviewer=reviewer)
    queue = LearningQueue(isolated / 'test.db', adapter.learn)
    assert queue.summary() == {'queued': 1} and queue.process_one()
    assert queue.summary() == {'completed': 1}
    with adapter.store.knowledge._connect() as db:
        row = db.execute("SELECT source_agent,source_session,status FROM knowledge_records WHERE source_agent='workbuddy'").fetchone()
        assert tuple(row) == ('workbuddy', 's', 'candidate')
        payload_text = db.execute('SELECT payload FROM learning_jobs').fetchone()[0]
        assert CONTENT not in payload_text and json.loads(payload_text)['transcript_format'] == 'codebuddy'
    assert hook_configuration('workbuddy', database_path=isolated / 'test.db')['execution']['observed']


def test_manual_hook_audit_without_workbuddy_session_cannot_claim_execution(isolated, client_home):
    audit = (isolated / 'test.db').with_suffix('.workbuddy-hook-runs.jsonl')
    outside = transcript(isolated / 'manual.jsonl')
    audit.write_text(json.dumps(dict(status='completed', agent_id='workbuddy', event='UserPromptSubmit',
        session_id='manual', transcript_path=str(outside), created_at='2026-09-30T00:00:00Z')) + '\n')
    assert not hook_configuration('workbuddy', database_path=isolated / 'test.db')['execution']['observed']


def test_background_install_uses_console_python_for_hook_streams(isolated, client_home, monkeypatch):
    interpreter = isolated / 'Python with spaces'
    interpreter.mkdir()
    (interpreter / 'pythonw.exe').touch()
    (interpreter / 'python.exe').touch()
    monkeypatch.setattr(sys, 'executable', str(interpreter / 'pythonw.exe'))
    plan = prepare_hook_installation(PROFILES['workbuddy'], config_path=client_home / 'settings.json')
    assert plan.python == interpreter / 'python.exe'


def test_workbuddy_runtime_contract_accepts_protocol_hook_and_codebuddy_format(isolated, client_home):
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=reviewer)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer test-only'}
    result = client.post('/v1/agents/register', headers=headers,
        json=dict(agent_id='workbuddy', display_name='WorkBuddy', adapter_type='protocol-hook'))
    assert result.status_code == 200 and result.json()['agent']['hook']['configured']
    result = client.post('/v1/learning/queue', headers=headers, json=dict(agent_id='workbuddy',
        project_key='shared', session_id='s', cwd=str(isolated), transcript_format='codebuddy',
        transcript_path=str(transcript(client_home / 'projects' / 'fixture' / 's.jsonl'))))
    assert result.status_code == 200, result.text
    assert app.state.learning_queue.process_one()
    app.state.timing_collector.close()


def test_codebuddy_parser_excludes_meta_and_reasoning_and_redacts_secrets(isolated):
    path = transcript(isolated / 'native.jsonl', [
        {'type': 'message', 'role': 'user', 'id': 'u', 'content': 'Remember the widget verifier.'},
        {'type': 'reasoning', 'content': 'private thoughts'},
        {'type': 'message', 'role': 'user', 'id': 'm', 'providerData': {'isMeta': True}, 'content': 'injected context'},
        {'type': 'message', 'role': 'assistant', 'id': 'a', 'content': [{'type': 'output_text', 'text': 'api_key=sk-testing-secret-12345'}]},
    ])
    result = parse_latest_codebuddy_turn(path)
    assert result.user_text == 'Remember the widget verifier.'
    assert 'REDACTED' in result.assistant_text and 'sk-testing' not in result.assistant_text
    assert 'private thoughts' not in result.review_text([]) and 'injected context' not in result.review_text([])


@pytest.mark.parametrize('output,metadata,expected', [
    ('completed', {'is_error': False}, None),
    ('Exit code: 0', {}, True),
    ('Exit code: 0', {'is_error': True}, False),
    ('completed', {'exitCode': 2}, False),
    ('pending', {'status': 'running'}, None),
])
def test_codebuddy_tool_evidence_requires_objective_exit_status(isolated, output, metadata, expected):
    path = transcript(isolated / 'native.jsonl', [
        {'type': 'message', 'role': 'user', 'id': 'u', 'content': CONTENT},
        {'type': 'function_call', 'id': 'c', 'call_id': 'c1', 'name': 'Bash', 'arguments': '{"command":"pytest tests -q"}'},
        {'type': 'function_call_output', 'id': 'r', 'call_id': 'c1', 'output': output,
         'providerData': {'toolResult': metadata}},
    ])
    tool = parse_latest_codebuddy_turn(path).tools[0]
    assert tool.success is expected and tool.objective_kind == 'test'


def test_codebuddy_branch_and_queued_boundary_preserve_original_turn(isolated):
    path = transcript(isolated / 'native.jsonl', [
        {'type': 'message', 'role': 'user', 'id': 'u0', 'content': 'Earlier request.'},
        {'type': 'message', 'role': 'user', 'id': 'u1', 'parentId': 'u0', 'content': CONTENT},
        {'type': 'message', 'role': 'assistant', 'id': 'discarded', 'parentId': 'u1', 'content': 'Superseded response.'},
        {'type': 'message', 'role': 'assistant', 'id': 'a1', 'parentId': 'u1', 'content': 'Correct response.'},
    ])
    original = parse_latest_codebuddy_turn(path)
    assert original.assistant_text == 'Correct response.' and original.previous_turn_key
    boundary = path.stat().st_size
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'type': 'message', 'role': 'user', 'id': 'u2',
            'parentId': 'a1', 'content': 'Next request.'}) + '\n')
    assert parse_latest_codebuddy_turn(path, end_offset=boundary).turn_hash == original.turn_hash
    assert parse_latest_codebuddy_turn(path).user_text == 'Next request.'


@pytest.mark.parametrize('exit_code', [0, 1, None])
def test_codebuddy_native_call_id_and_nested_shell_metadata(isolated, exit_code):
    path = transcript(isolated / 'native.jsonl', [
        {'type': 'message', 'role': 'user', 'id': 'u', 'content': CONTENT},
        {'type': 'function_call', 'id': 'c', 'parentId': 'u', 'callId': 'native-call',
         'name': 'Bash', 'arguments': '{"command":"pytest tests -q"}'},
        {'type': 'function_call_result', 'id': 'r', 'parentId': 'c', 'callId': 'native-call',
         'name': 'Bash', 'output': {'type': 'text', 'text': 'Synthetic command output.'},
         'providerData': {'toolResult': {'rawResponse': {'exitCode': exit_code, 'interrupted': False}}}},
    ])
    tool = parse_latest_codebuddy_turn(path).tools[0]
    assert tool.tool_use_id == 'native-call'
    assert tool.output_summary == 'Synthetic command output.'
    assert tool.success is (None if exit_code is None else exit_code == 0)

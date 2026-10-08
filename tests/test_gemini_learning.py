from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.agent_registry import discover_agents, hook_configuration, install_native_hook
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.gemini_transcript import parse_latest_gemini_turn
from agent_knowledge_bridge.integration_profiles import PROFILES
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.provider import atomic_json
from agent_knowledge_bridge.runtime_learning_adapter import RuntimeLearningAdapter
from agent_knowledge_bridge.store import KnowledgeStore


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMWEAVE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('GEMINI_CLI_HOME', str(tmp_path / 'client'))
    monkeypatch.setenv('MW_DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('MW_RUNTIME_MODE', 'local')
    for key in ('MW_AGENT_ID', 'AKB_AGENT_ID', 'MW_PROJECT_KEY', 'AKB_PROJECT_KEY'):
        monkeypatch.delenv(key, raising=False)
    atomic_json(tmp_path / 'home' / 'config.json', {'default_project': 'shared'})
    return tmp_path


def messages():
    return [{'id': 'u1', 'type': 'user', 'content': [{'text': 'Use the widget verifier before deployment.'}]},
        {'id': 'a1', 'type': 'gemini', 'content': 'Verified.', 'thoughts': [{'text': 'NEVER_COPY_THOUGHTS'}],
         'toolCalls': [{'id': 't1', 'name': 'run_shell_command', 'args': {'command': 'pytest tests/widget.py'},
             'status': 'success', 'result': [{'functionResponse': {'name': 'run_shell_command',
                 'response': {'output': 'PASS\nExit Code: 0'}}}]}]}]


def write(path, entries):
    if path.suffix == '.json':
        path.write_text(json.dumps({'sessionId': 's', 'projectHash': 'p', 'messages': entries}), encoding='utf-8')
    else:
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')


@pytest.mark.parametrize('suffix', ['.json', '.jsonl'])
def test_parse_gemini_turn_and_evidence(isolated, suffix):
    path = isolated / ('session' + suffix)
    write(path, messages())
    turn = parse_latest_gemini_turn(path)
    assert turn.user_text == 'Use the widget verifier before deployment.'
    assert turn.assistant_text == 'Verified.'
    assert turn.source_turn_key.startswith('transcript:')
    assert turn.tools[0].success is True and turn.tools[0].objective_kind == 'test'
    assert 'NEVER_COPY_THOUGHTS' not in turn.review_text(['test-event'])


def test_gemini_prompt_response_accepts_known_text_shapes(isolated):
    profile = PROFILES['gemini-cli']
    operation, normalized = profile.normalize({
        'hook_event_name': 'AfterAgent',
        'prompt_response': {'content': [{'text': 'Gemini finished the task.'}]},
    })
    assert operation == 'learn'
    assert normalized['last_assistant_message'] == 'Gemini finished the task.'

    _, unknown = profile.normalize({'hook_event_name': 'AfterAgent', 'prompt_response': {'metadata': 1}})
    assert unknown['last_assistant_message'] == ''


@pytest.mark.parametrize('state,result,expected', [
    ('success', 'PASS', None), ('success', 'Exit Code: 1', False),
    ('executing', 'Exit Code: 0', None), ('cancelled', 'Exit Code: 0', False),
    ('error', 'Exit Code: 0', False),
])
def test_shell_transport_success_is_not_test_success(isolated, state, result, expected):
    entries = messages()
    call = entries[-1]['toolCalls'][0]
    call.update(status=state, result=result)
    path = isolated / 'session.json'
    write(path, entries)
    assert parse_latest_gemini_turn(path).tools[0].success is expected


@pytest.mark.parametrize('suffix', ['.json', '.jsonl'])
def test_queue_freezes_turn_without_copying_messages(isolated, suffix):
    path = isolated / ('session' + suffix)
    entries = messages()
    write(path, entries)
    seen = []
    adapter = RuntimeLearningAdapter(database_path=isolated / 'test.db', agent_id='gemini-cli',
        project_key='shared', reviewer=lambda text: seen.append(text) or {'proposals': []})
    queue = LearningQueue(isolated / 'test.db', adapter.learn)
    args = {'agent_id': 'gemini-cli', 'project_key': 'shared', 'session_id': 's', 'transcript_path': str(path)}
    assert queue.submit(args)['status'] == 'queued'
    entries += [{'id': 'u2', 'type': 'user', 'content': 'A later unrelated question.'}]
    if suffix == '.json':
        write(path, entries)
    else:
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entries[-1]) + '\n')
    assert queue.process_one()
    assert len(seen) == 1 and 'A later unrelated question' not in seen[0]
    with queue.store.knowledge._connect() as db:
        payload = db.execute('SELECT payload FROM learning_jobs').fetchone()[0]
    assert 'widget verifier' not in payload and 'Verified.' not in payload


def test_queued_rewritten_json_is_rejected_when_source_changes(isolated):
    path = isolated / 'session.json'
    entries = messages()
    write(path, entries)
    queue = LearningQueue(isolated / 'test.db', lambda payload: {'status': 'completed'})
    queue.submit({'agent_id': 'gemini-cli', 'project_key': 'shared', 'session_id': 's', 'transcript_path': str(path)})
    with queue.store.knowledge._connect() as db:
        payload = json.loads(db.execute('SELECT payload FROM learning_jobs').fetchone()[0])
    entries[0]['content'] = 'Different source'
    write(path, entries)
    adapter = RuntimeLearningAdapter(database_path=isolated / 'test.db', agent_id='gemini-cli', project_key='shared')
    with pytest.raises(ValueError, match='changed'):
        adapter.learn(payload)


def test_jsonl_updates_rewinds_and_checkpoints(isolated):
    path = isolated / 'session.jsonl'
    first = messages()[0]
    write(path, [first, {'id': 'a1', 'type': 'gemini', 'content': 'Old'},
        {'id': 'a1', 'type': 'gemini', 'content': 'Updated'},
        {'id': 'u2', 'type': 'user', 'content': 'removed'}, {'$rewindTo': 'u2'}])
    assert parse_latest_gemini_turn(path).assistant_text == 'Updated'
    write(path, [{'$set': {'messages': messages()}}])
    assert parse_latest_gemini_turn(path).tools[0].success is True


def test_gemini_install_repair_status_and_legacy_registration(isolated):
    path = isolated / 'client' / '.gemini' / 'settings.json'
    atomic_json(path, {'model': {'name': 'keep'}, 'hooks': {'BeforeAgent': [
        {'hooks': [{'type': 'command', 'command': 'other-hook'}]}]}})
    app = create_app(database_path=isolated / 'test.db', api_token='test-only')
    client = TestClient(app)
    result = client.post('/v1/agents/register', headers={'Authorization': 'Bearer test-only'},
        json={'agent_id': 'gemini-cli', 'display_name': 'Gemini CLI', 'adapter_type': 'runtime-api'})
    assert result.status_code == 200
    assert result.json()['agent']['adapter_type'] == 'gemini-hook'
    assert result.json()['agent']['hook']['configured']
    assert result.json()['agent']['capabilities'] == ['recall', 'learn', 'shared-knowledge']
    config = json.loads(path.read_text(encoding='utf-8'))
    assert config['model'] == {'name': 'keep'}
    assert config['hooks']['BeforeAgent'][0]['hooks'][0]['command'] == 'other-hook'
    assert path.with_name('settings.json.memweave-backup').exists()
    for event in ('BeforeAgent', 'AfterAgent'):
        ours = [hook for group in config['hooks'][event] for hook in group['hooks'] if hook.get('name', '').startswith('memweave-')]
        assert len(ours) == 1 and ours[0]['timeout'] == 10_000
        assert 'commandWindows' not in ours[0] and 'async' not in ours[0]
        if os.name == 'nt':
            assert ours[0]['command'].startswith("& '")
    assert not install_native_hook('gemini-cli')['changed']
    assert next(row for row in discover_agents() if row['agent_id'] == 'gemini-cli')['config_path'] == str(path.parent)
    for disabled in ({'enabled': False}, {'disabled': ['memweave-beforeagent']}):
        atomic_json(path, {**config, 'hooksConfig': disabled})
        assert not hook_configuration('gemini-cli')['configured']
    app.state.timing_collector.close()
    client.close()


def test_runtime_gemini_queue_compiles_candidate_with_source_and_evidence(isolated):
    from test_generic_learning import proposal
    path = isolated / 'session.jsonl'
    write(path, messages())
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=proposal)
    store = app.state.learning_queue.store.knowledge
    store.register_agent(agent_id='gemini-cli', display_name='Gemini', adapter_type='gemini-hook')
    client = TestClient(app)
    client.headers['Authorization'] = 'Bearer test-only'
    response = client.post('/v1/learning/queue', json={'agent_id': 'gemini-cli', 'project_key': 'shared',
        'session_id': 's', 'transcript_path': str(path)})
    assert response.status_code == 200 and response.json()['status'] == 'queued'
    assert app.state.learning_queue.process_one()
    assert app.state.learning_queue.summary() == {'completed': 1}
    with store._connect() as db:
        record = db.execute('SELECT source_agent,source_session,status FROM knowledge_records').fetchone()
        assert tuple(record) == ('gemini-cli', 's', 'candidate')
        events = db.execute('SELECT agent_id,session_id,success,objective_kind FROM agent_events').fetchall()
        assert len(events) == 1 and tuple(events[0]) == ('gemini-cli', 's', 1, 'test')
    client.close()
    app.state.timing_collector.close()


@pytest.mark.parametrize(('review_response', 'expected'), [
    ({'proposals': []}, (0, 0, 'reviewer_returned_zero')),
    ({'proposals': [None]}, (1, 1, 'all_rejected')),
])
def test_zero_candidate_diagnostics_distinguish_empty_review_from_rejection(isolated, review_response, expected):
    path = isolated / 'diagnostics.jsonl'
    write(path, messages())
    adapter = RuntimeLearningAdapter(database_path=isolated / 'diagnostics.db', agent_id='gemini-cli',
        project_key='shared', reviewer=lambda _: review_response)
    result = adapter.learn({'session_id': 'diagnostics', 'transcript_path': str(path)})
    latest = adapter.store.knowledge.latest_learning('gemini-cli')
    assert result['proposals'] == 0
    assert (latest['reviewer_proposal_count'], latest['rejected_proposal_count'], latest['proposal_outcome']) == expected


def invoke(payload):
    return subprocess.run([sys.executable, '-m', 'agent_knowledge_bridge.hooks.gemini_learning_hook'],
        input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True, timeout=20)


@pytest.mark.skipif(os.name != 'nt', reason='Gemini uses PowerShell on Windows')
def test_installed_command_executes_with_spaces_and_unicode(isolated):
    install_native_hook('gemini-cli')
    path = isolated / 'client' / '.gemini' / 'settings.json'
    config = json.loads(path.read_text(encoding='utf-8'))
    command = config['hooks']['BeforeAgent'][0]['hooks'][0]['command']
    store = KnowledgeStore(isolated / 'test.db')
    store.register_agent(agent_id='gemini-cli', display_name='Gemini', adapter_type='gemini-hook')
    # The pinned interpreter already lives in a Unicode workspace / spaced venv.
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        input=json.dumps({'hook_event_name': 'BeforeAgent', 'session_id': 's', 'prompt': 'weather today'}),
        text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not result.stderr and json.loads(result.stdout) == {}


def test_gemini_hook_recalls_cross_agent_and_enqueues_without_blocking(isolated):
    store = KnowledgeStore(isolated / 'test.db')
    store.register_agent(agent_id='gemini-cli', display_name='Gemini', adapter_type='gemini-hook')
    created = store.publish(source_agent='codex', project_key='shared', title='Widget verifier',
        content='Use the widget verifier before deployment.', knowledge_type='procedure',
        evidence_summary='fixture', search_terms='widget verifier deployment', scope='project')
    store.feedback(agent_id='codex', knowledge_id=created['knowledge']['id'], outcome='verified',
        evidence_kind='test', evidence_ref='tests/widget.py', evidence_summary='passed')
    path = isolated / 'session.json'
    write(path, messages())
    args = {'session_id': 's', 'transcript_path': str(path), 'cwd': str(isolated)}
    result = invoke({**args, 'hook_event_name': 'BeforeAgent', 'prompt': 'widget verifier deployment'})
    assert result.returncode == 0 and not result.stderr
    response = json.loads(result.stdout)
    assert response['hookSpecificOutput']['hookEventName'] == 'BeforeAgent'
    assert 'widget verifier' in response['hookSpecificOutput']['additionalContext']
    assert json.loads(invoke({**args, 'hook_event_name': 'BeforeAgent', 'prompt': 'weather today'}).stdout) == {}
    result = invoke({**args, 'hook_event_name': 'AfterAgent',
        'prompt_response': {'text': 'Gemini completed the widget task.'}})
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert LearningQueue(isolated / 'test.db', None).summary() == {'queued': 1}
    store.disable_agent('gemini-cli')
    assert json.loads(invoke({**args, 'hook_event_name': 'BeforeAgent', 'prompt': 'widget verifier deployment'}).stdout) == {}


def test_empty_hook_stdin_fails_open_with_specific_diagnostic(isolated):
    result = subprocess.run([sys.executable, '-m', 'agent_knowledge_bridge.hooks.gemini_learning_hook'],
        input='', text=True, encoding='utf-8', capture_output=True, timeout=20)
    assert result.returncode == 0 and json.loads(result.stdout) == {} and not result.stderr
    errors = isolated / 'test.hook-errors.jsonl'
    assert 'hook stdin is empty' in errors.read_text(encoding='utf-8')


def test_hook_stdin_that_never_closes_is_bounded(monkeypatch):
    import threading
    from agent_knowledge_bridge.hooks import shared_hook

    release = threading.Event()

    class OpenPipe:
        @property
        def buffer(self):
            return self

        def read(self, _limit):
            release.wait()
            return b''

    monkeypatch.setattr(shared_hook, 'HOOK_INPUT_TIMEOUT_SECONDS', 0.01)
    monkeypatch.setattr(shared_hook.sys, 'stdin', OpenPipe())
    try:
        with pytest.raises(ValueError, match='did not close'):
            shared_hook.read_input()
    finally:
        release.set()

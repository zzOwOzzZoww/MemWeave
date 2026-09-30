from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.hooks import shared_hook
from agent_knowledge_bridge.integration_profiles import PROFILES, load_profile
from agent_knowledge_bridge.learning_engine import LearningEngine
from agent_knowledge_bridge.provider import atomic_json
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from agent_knowledge_bridge.runtime_learning_adapter import transcript_parser
from agent_knowledge_bridge.store import KnowledgeStore


CONTENT = 'Use the widget verifier before deployment.'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('MEMWEAVE_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('MW_DB_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setenv('MW_RUNTIME_MODE', 'local')
    for key in ('MW_AGENT_ID', 'AKB_AGENT_ID', 'MW_PROJECT_KEY', 'AKB_PROJECT_KEY',
                'MW_DAEMON_URL', 'MW_DAEMON_TOKEN', 'MW_LATENCY_BASELINE'):
        monkeypatch.delenv(key, raising=False)
    atomic_json(tmp_path / 'home' / 'config.json', {'default_project': 'shared'})
    return tmp_path


def profile_json(**updates):
    return {'agent_id': 'third-party-test', 'recall_event': 'Question', 'learn_event': 'Finished',
        'event_field': 'event.name', 'transcript_format': 'claude',
        'fields': {'session_id': 'metadata.session', 'turn_id': 'metadata.turn', 'cwd': 'metadata.cwd',
                   'transcript_path': 'metadata.log', 'prompt': 'request.message', 'turn': 'result.turn'},
        'recall_output': {'context': '{{context}}', 'event': '{{event}}'}, **updates}


def custom_profile(root, **updates):
    path = root / 'integration.json'
    atomic_json(path, profile_json(**updates))
    return path, load_profile(path)


def seed(root, agent='third-party-test'):
    store = KnowledgeStore(root / 'test.db')
    store.register_agent(agent_id=agent, display_name=agent, adapter_type='custom')
    record = store.publish(source_agent='codex', project_key='shared', title='Widget verifier baseline',
        content='The widget verifier uses fixed inputs.', knowledge_type='fact', scope='project',
        evidence_summary='fixture', search_terms='widget verifier deployment')['knowledge']
    store.feedback(agent_id='human', knowledge_id=record['id'], outcome='verified', evidence_kind='user_approval',
        evidence_ref='test-only', evidence_summary='Reviewed fixture')
    return store


def invoke(path, payload):
    return subprocess.run([sys.executable, '-m', 'agent_knowledge_bridge.hooks.generic_learning_hook',
        '--profile', str(path)], input=json.dumps(payload), text=True, encoding='utf-8',
        capture_output=True, timeout=20)


def reviewer(text):
    return {'proposals': [{'title': 'Widget deployment rule', 'content': CONTENT,
        'knowledge_type': 'procedure', 'scope': 'project', 'source_role': 'user',
        'source_quotes': [CONTENT], 'evidence_event_ids': []}]}


def test_unknown_agent_uses_only_json_for_recall_queue_and_learning(isolated):
    path, _ = custom_profile(isolated)
    store = seed(isolated)
    assert 'third-party-test' not in PROFILES
    transcript = isolated / 'source.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 'u1',
        'message': {'role': 'user', 'content': CONTENT}}) + '\n', encoding='utf-8')
    payload = {'event': {'name': 'Question'}, 'metadata': {'session': 's', 'turn': 't',
        'cwd': str(isolated), 'log': str(transcript)}, 'request': {'message': CONTENT}}
    response = invoke(path, payload)
    assert response.returncode == 0 and not response.stderr
    output = json.loads(response.stdout)
    assert output['event'] == 'Question' and 'Widget verifier baseline' in output['context']
    assert 'hookSpecificOutput' not in output
    assert json.loads(invoke(path, {**payload, 'request': {'message': 'weather today'}}).stdout) == {}
    completed = invoke(path, {**payload, 'event': {'name': 'Finished'}})
    assert completed.returncode == 0 and json.loads(completed.stdout) == {}
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=reviewer)
    queue = app.state.learning_queue
    assert queue.summary() == {'queued': 1}
    assert queue.process_one() and queue.summary() == {'completed': 1}
    with store._connect() as db:
        saved = db.execute("SELECT source_agent,source_session,status FROM knowledge_records WHERE source_agent=?",
                           ('third-party-test',)).fetchone()
        assert tuple(saved) == ('third-party-test', 's', 'candidate')
        job = json.loads(db.execute('SELECT payload FROM learning_jobs').fetchone()[0])
        assert job['transcript_format'] == 'claude' and CONTENT not in json.dumps(job)
    app.state.timing_collector.close()


@pytest.mark.parametrize('name', tuple(PROFILES))
def test_existing_entry_points_delegate_to_same_runner(name, monkeypatch):
    from importlib import import_module
    module = import_module('agent_knowledge_bridge.hooks.' + PROFILES[name].launcher[:-3])
    seen = []
    monkeypatch.setattr(shared_hook, 'run_hook', lambda profile, **kwargs: seen.append(profile) or 0)
    assert module.run_hook() == 0
    assert seen == [PROFILES[name]]


@pytest.mark.parametrize('name', tuple(PROFILES))
def test_explicit_dead_runtime_cannot_switch_to_another_instance(isolated, monkeypatch, name):
    monkeypatch.setenv('MW_RUNTIME_MODE', '')
    monkeypatch.setenv('MW_DAEMON_URL', 'http://127.0.0.1:1111')
    monkeypatch.setattr(shared_hook.runtime_state, 'runtime_candidates', lambda: iter([
        ('http://127.0.0.1:1111', 'test-only'), ('http://127.0.0.1:2222', 'test-only')]))
    seen = []
    monkeypatch.setattr(shared_hook, 'daemon_is_live', lambda url, token: seen.append(url) or ':2222' in url)
    assert shared_hook.selected_runtime(PROFILES[name]) is None
    assert seen == ['http://127.0.0.1:1111']


@pytest.mark.parametrize('updates', [
    {'agent_id': '../escape'}, {'recall_event': 'Finished'}, {'event_field': 'event[0]'},
    {'fields': {'unknown': 'request.text'}}, {'fields': {'prompt': '__import__(os)'}},
    {'transcript_format': 'invented'}, {'bind_transcript_boundary': 'true'},
    {'transcript_format': []}, {'transcript_format': {}}, {'transcript_format': None},
    {'recall_output': {'context': 'prefix{{context}}'}}, {'recall_output': {'{{context}}': 'key-only'}},
    {'command': 'arbitrary-code'}, {'error_suffix': '/outside'},
])
def test_profile_is_data_only_and_rejects_invalid_mappings(isolated, updates):
    path = isolated / 'integration.json'
    atomic_json(path, profile_json(**updates))
    with pytest.raises(ValueError):
        load_profile(path)


def test_profile_is_bounded_and_encoding_does_not_mutate_template(isolated):
    path, profile = custom_profile(isolated)
    assert profile.encode_context('first')['context'] == 'first'
    assert profile.encode_context('second')['context'] == 'second'
    assert profile.encode_context('') == {}
    assert profile.recall_output['context'] == '{{context}}'
    path.write_text(' ' * 65_537, encoding='utf-8')
    with pytest.raises(ValueError, match='64 KiB'):
        load_profile(path)


@pytest.mark.parametrize('kind', ['unknown-event', 'disabled', 'invalid-field'])
def test_ignored_or_invalid_events_do_not_touch_runtime(isolated, monkeypatch, capsys, kind):
    _, profile = custom_profile(isolated)
    store = seed(isolated)
    payload = {'event': {'name': 'Question'}, 'metadata': {'session': 's'}, 'request': {'message': CONTENT}}
    if kind == 'unknown-event':
        payload['event']['name'] = 'Other'
    elif kind == 'disabled':
        store.disable_agent(profile.agent_id)
    else:
        payload['metadata']['session'] = {'private': 'NOT_A_SESSION_ID'}
    def unexpected():
        raise AssertionError('unexpected Runtime access')
    assert shared_hook.run_hook_input(profile, payload, runtime_selector=unexpected) == 0
    assert capsys.readouterr().out.strip() == '{}'
    with store._connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='reuse_traces'").fetchone() is None


def test_shared_errors_redact_credentials_and_remain_fail_open(isolated, capsys):
    seed(isolated)
    _, profile = custom_profile(isolated)
    def unavailable():
        raise RuntimeError('API_KEY=TEST_ONLY_NOT_A_REAL_SECRET_123456')
    assert shared_hook.run_hook_input(profile, {'event': {'name': 'Question'},
        'metadata': {'session': 's'}, 'request': {'message': CONTENT}}, runtime_selector=unavailable) == 0
    assert capsys.readouterr().out.strip() == '{}'
    log = (isolated / 'test.hook-errors.jsonl').read_text(encoding='utf-8')
    assert 'TEST_ONLY_NOT_A_REAL_SECRET' not in log and 'REDACTED' in log


def test_generic_inline_turn_uses_runtime_and_never_spools_chat(isolated, capsys):
    seed(isolated)
    _, profile = custom_profile(isolated, transcript_format='auto')
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=reviewer)
    runtime = MemWeaveRuntimeClient(base_url='http://testserver', token='test-only',
        agent_id=profile.agent_id, project_key='wrong')
    client = TestClient(app)
    def post(route, payload):
        response = client.post(route, json=payload, headers={'Authorization': 'Bearer test-only'})
        response.raise_for_status()
        return response.json()
    runtime._post = post
    payload = {'event': {'name': 'Finished'}, 'metadata': {'session': 's', 'turn': 't'},
        'result': {'turn': {'user_text': CONTENT, 'assistant_text': 'PRIVATE_CHAT_NOT_A_LESSON', 'tools': []}}}
    assert shared_hook.run_hook_input(profile, payload, runtime_selector=lambda: runtime) == 0
    assert capsys.readouterr().out.strip() == '{}' and runtime.project_key == 'shared'
    with app.state.learning_queue.store.knowledge._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM learning_jobs').fetchone()[0] == 0
        assert 'PRIVATE_CHAT_NOT_A_LESSON' not in '\n'.join(db.iterdump())
    app.state.timing_collector.close()
    client.close()


def test_offline_inline_turn_is_not_written_to_a_queue(isolated, capsys):
    store = seed(isolated)
    _, profile = custom_profile(isolated, transcript_format='auto')
    shared_hook.run_hook_input(profile, {'event': {'name': 'Finished'}, 'metadata': {'session': 's'},
        'result': {'turn': {'user_text': 'PRIVATE_CHAT_NOT_A_LESSON'}}}, runtime_selector=lambda: None)
    assert capsys.readouterr().out.strip() == '{}'
    with store._connect() as db:
        assert 'PRIVATE_CHAT_NOT_A_LESSON' not in '\n'.join(db.iterdump())
    assert 'PRIVATE_CHAT_NOT_A_LESSON' not in (isolated / 'test.hook-errors.jsonl').read_text(encoding='utf-8')


def test_bounded_shared_input_rejects_non_objects(isolated, monkeypatch, capsys):
    profile = PROFILES['claude-code']
    for raw in (b'[]', b'x' * (shared_hook.MAX_INPUT_BYTES + 1)):
        monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
        assert shared_hook.run_hook(profile) == 0
        assert capsys.readouterr().out.strip() == '{}'


def test_core_context_is_not_a_client_response(isolated):
    seed(isolated)
    engine = LearningEngine(database_path=isolated / 'test.db', agent_id='third-party-test', project_key='shared')
    assert isinstance(engine.recall_context({'session_id': 's', 'prompt': CONTENT}), str)
    assert engine.recall_context({'session_id': 's', 'prompt': 'weather today'}) == ''


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('agent', ['third-party-test', 'codex'])
def test_profile_transcript_binding_is_identical_online_and_offline(isolated, capsys, live, agent):
    store = seed(isolated, agent=agent)
    _, profile = custom_profile(isolated, agent_id=agent, bind_transcript_boundary=True)
    transcript = isolated / 'turn.jsonl'
    transcript.write_text(json.dumps({'type': 'user', 'uuid': 'u1',
        'message': {'role': 'user', 'content': CONTENT}}) + '\n', encoding='utf-8')
    expected = transcript_parser(agent, 'claude')(str(transcript)).source_turn_key
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=reviewer)
    client = TestClient(app)
    runtime = MemWeaveRuntimeClient(base_url='http://testserver', token='test-only', agent_id=agent)

    def post(route, payload):
        response = client.post(route, json=payload, headers={'Authorization': 'Bearer test-only'})
        response.raise_for_status()
        return response.json()

    runtime._post = post
    try:
        assert shared_hook.run_hook_input(profile, {'event': {'name': 'Question'},
            'metadata': {'session': 's', 'log': str(transcript)}, 'request': {'message': CONTENT}},
            runtime_selector=lambda: runtime if live else None) == 0
        assert 'Widget verifier baseline' in json.loads(capsys.readouterr().out)['context']
        with store._connect() as db:
            trace = db.execute('SELECT turn_id,transcript_boundary FROM reuse_traces').fetchone()
        assert trace['turn_id'] == expected
        assert json.loads(trace['transcript_boundary']) == {'key': expected, 'fresh': True}
    finally:
        app.state.timing_collector.close()
        client.close()


@pytest.mark.parametrize('options', [
    {'transcript_format': []}, {'bind_transcript_boundary': 'true'}, {'bind_transcript_boundary': True},
])
def test_runtime_rejects_invalid_or_unresolvable_boundary_options(isolated, options):
    seed(isolated)
    app = create_app(database_path=isolated / 'test.db', api_token='test-only', reviewer=reviewer)
    try:
        with TestClient(app) as client:
            response = client.post('/v1/learning/recall', headers={'Authorization': 'Bearer test-only'},
                json={'agent_id': 'third-party-test', 'project_key': 'shared', 'session_id': 's',
                    'prompt': CONTENT, **options})
            assert response.status_code == 422
    finally:
        app.state.timing_collector.close()


@pytest.mark.parametrize('raw', ['[]', json.dumps(profile_json(transcript_format=[])),
    json.dumps(profile_json(transcript_format={})), '[' * 1200 + '0' + ']' * 1200])
def test_invalid_profile_cli_is_fail_open_without_traceback(isolated, raw):
    path = isolated / 'invalid.json'
    path.write_text(raw, encoding='utf-8')
    result = invoke(path, {})
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert 'Traceback' not in result.stderr


def test_output_depth_is_checked_even_after_a_context_placeholder(isolated):
    nested = 'literal'
    for _ in range(33):
        nested = [nested]
    with pytest.raises(ValueError, match='deeply nested'):
        custom_profile(isolated, recall_output={'context': '{{context}}', 'nested': nested})

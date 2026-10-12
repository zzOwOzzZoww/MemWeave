from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_knowledge_bridge import cli, runtime_recovery
from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.gemini_transcript import parse_latest_gemini_turn
from agent_knowledge_bridge.hooks import shared_hook
from agent_knowledge_bridge.integration_profiles import PROFILES
from agent_knowledge_bridge.learning_engine import DeepSeekReviewer, LearningEngine
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.provider import atomic_json
from agent_knowledge_bridge.runtime_learning_adapter import RuntimeLearningAdapter
from test_unified_integration import CONTENT, isolated, reviewer, seed


def test_gemini_long_turn_freezes_and_learns_without_reasoning(isolated):
    path = isolated / 'long.jsonl'
    rows = [
        {'id': 'u', 'type': 'user', 'content': CONTENT},
        {'id': 'a', 'type': 'gemini', 'content': 'Completed.',
         'thoughts': [{'text': 'PRIVATE_REASONING' + 'x' * 2_100_000}]},
    ]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    with pytest.raises(ValueError, match='no user prompt'):
        parse_latest_gemini_turn(path, max_bytes=2_000_000)
    seen = []
    adapter = RuntimeLearningAdapter(database_path=isolated / 'test.db', agent_id='gemini-cli',
        project_key='shared', reviewer=lambda text: seen.append(text) or reviewer(text))
    queue = LearningQueue(isolated / 'test.db', adapter.learn)
    queue.submit(dict(agent_id='gemini-cli', project_key='shared', session_id='long', transcript_path=str(path)))
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'id': 'next', 'type': 'user', 'content': 'Later question.'}) + '\n')
    assert queue.process_one() and queue.summary() == {'completed': 1}
    assert CONTENT in seen[0] and 'PRIVATE_REASONING' not in seen[0] and 'Later question.' not in seen[0]
    assert adapter.store.knowledge.review_queue(requester_agent='gemini-cli', project_key='shared')['count'] == 1


def test_old_gemini_jsonl_tail_count_is_not_a_global_message_count(isolated):
    path = isolated / 'old-window.jsonl'
    rows = [{'id': 'old', 'type': 'user', 'content': 'Old request.'},
        {'id': 'padding', 'type': 'gemini', 'thoughts': [{'text': 'x' * 2_100_000}]},
        {'id': 'u', 'type': 'user', 'content': CONTENT},
        {'id': 'a', 'type': 'gemini', 'content': 'Completed.'}]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    boundary = path.stat().st_size
    old = parse_latest_gemini_turn(path, max_bytes=2_000_000, end_offset=boundary)
    result = parse_latest_gemini_turn(path, end_offset=boundary,
        message_count=2, expected_turn_hash=old.turn_hash)
    assert result.user_text == CONTENT


def test_user_evidence_survives_assistant_validation_caveat(isolated):
    turn = TranscriptTurn(CONTENT, '没有实际验证资料时，不得声称测试通过。', ())
    seen = []
    adapter = LearningEngine(database_path=isolated / 'test.db', agent_id='codex', project_key='shared',
        transcript_parser=lambda *args, **kwargs: turn,
        reviewer=lambda text: seen.append(text) or reviewer(text))
    result = adapter.learn({'session_id': 'caveat'})
    assert len(seen) == 1 and result['proposals'] == 1 and result['promoted'] == 0
    assert adapter.store.knowledge.review_queue(requester_agent='codex', project_key='shared')['count'] == 1


def test_real_reviewer_missing_quotes_is_counted_and_rejected(isolated, monkeypatch):
    model = DeepSeekReviewer.__new__(DeepSeekReviewer)
    model.api_key, model.base_url, model.model, model.timeout = 'test-only', 'http://invalid', 'fixture', 1
    raw = {'proposals': [{'title': 'Unsupported summary', 'content': 'Invented lesson.',
        'knowledge_type': 'procedure', 'scope': 'project'}]}
    response = {'choices': [{'message': {'content': json.dumps(raw)}}]}
    monkeypatch.setattr('agent_knowledge_bridge.provider.open_request',
        lambda *a, **k: io.BytesIO(json.dumps(response).encode()))
    adapter = LearningEngine(database_path=isolated / 'test.db', agent_id='codex', project_key='shared',
        transcript_parser=lambda *a, **k: TranscriptTurn(CONTENT, 'This is just a guess.', ()), reviewer=model)
    result = adapter.learn({'session_id': 'missing-quotes'})
    assert (result['reviewer_proposals'], result['rejected_proposals'], result['proposal_outcome']) == (1, 1, 'all_rejected')
    assert result['proposals'] == 0
    assert result['rejection_reasons'] == {'missing_source_quotes': 1}
    latest = adapter.store.knowledge.latest_learning('codex')
    assert latest['rejection_reasons'] == {'missing_source_quotes': 1}
    detail = adapter.store.run_records(requester_agent='human', project_key='shared',
        source_agent='codex', run_id=latest['run_id'])
    assert detail['run']['rejection_reasons'] == {'missing_source_quotes': 1}


def test_unobserved_assistant_claim_is_still_rejected(isolated):
    turn = TranscriptTurn('Why does the widget fail?', 'This is just a guess about the widget.', ())
    adapter = LearningEngine(database_path=isolated / 'test.db', agent_id='codex', project_key='shared',
        transcript_parser=lambda *a, **k: turn, reviewer=lambda _: {'proposals': [{
            'title': 'Widget explanation', 'content': 'Unsupported assertion', 'knowledge_type': 'fact',
            'source_role': 'assistant', 'source_quotes': [turn.assistant_text]}]})
    result = adapter.learn({'session_id': 'guess'})
    assert result['proposals'] == 0 and result['rejection_reasons'] == {'invalid_source_evidence': 1}


@pytest.mark.parametrize('setting,value', [('MW_RUNTIME_MODE', 'local'),
    ('MW_DAEMON_URL', 'http://127.0.0.1:1'), ('MW_DAEMON_TOKEN', 'fixture')])
def test_recovery_respects_explicit_runtime_boundaries(isolated, monkeypatch, setting, value):
    atomic_json(isolated / 'home' / 'config.json', {'config_version': 1, 'database_path': str(isolated / 'test.db')})
    monkeypatch.setenv('MW_RUNTIME_MODE', '')
    monkeypatch.setenv(setting, value)
    assert not runtime_recovery.recovery_configured()


def test_recovery_does_not_switch_database(isolated, monkeypatch):
    monkeypatch.setenv('MW_RUNTIME_MODE', '')
    atomic_json(isolated / 'home' / 'config.json', {'config_version': 1, 'database_path': str(isolated / 'other.db')})
    assert not runtime_recovery.recovery_configured()


def test_recovery_request_is_detached_and_uses_console_python(isolated, monkeypatch):
    monkeypatch.setenv('MW_RUNTIME_MODE', '')
    atomic_json(isolated / 'home' / 'config.json', {'config_version': 1,
        'database_path': str(isolated / 'test.db')})
    monkeypatch.setattr(sys, 'executable', str(isolated / 'Python with spaces' / 'pythonw.exe'))
    launched = []
    monkeypatch.setattr(runtime_recovery.subprocess, 'Popen',
        lambda args, **kwargs: launched.append((args, kwargs)))
    assert runtime_recovery.request_recovery()
    args, options = launched[0]
    assert args[0].endswith('python.exe') and args[-1] == 'agent_knowledge_bridge.runtime_recovery'
    assert options['stdin'] == subprocess.DEVNULL and options['stdout'] is options['stderr']
    assert options['close_fds']


def test_offline_stop_queues_before_requesting_detached_recovery(isolated, monkeypatch, capsys):
    seed(isolated, agent='codex')
    path = isolated / 'turn.jsonl'
    path.write_text('{}\n', encoding='utf-8')
    seen = []
    def recover():
        assert LearningQueue(isolated / 'test.db', None).summary() == {'queued': 1}
        seen.append(True)
    monkeypatch.setattr(shared_hook, 'request_recovery', recover)
    assert shared_hook.run_hook_input(PROFILES['codex'], dict(hook_event_name='Stop',
        session_id='offline', transcript_path=str(path)), runtime_selector=lambda: None) == 0
    assert seen == [True] and capsys.readouterr().out.strip() == '{}'


def test_offline_recovery_starts_worker_and_drains_queue(isolated, monkeypatch):
    monkeypatch.setenv('MW_RUNTIME_MODE', '')
    atomic_json(isolated / 'home' / 'config.json', {'config_version': 1,
        'database_path': str(isolated / 'test.db'), 'default_project': 'shared', 'runtime_port': 0})
    path = isolated / 'turn.jsonl'
    path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message',
        'message': 'Hello'}}) + '\n', encoding='utf-8')
    # This control turn completes without requiring a real remote reviewer.
    queue = LearningQueue(isolated / 'test.db', None)
    queue.store.knowledge.register_agent(agent_id='codex', display_name='Codex', adapter_type='codex-hook')
    payload = dict(agent_id='codex', project_key='shared', session_id='recovery', transcript_path=str(path))
    # Secret-only input must be suppressed before any model call.
    path.write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'user_message',
        'message': 'remember API key test-only-credential'}}) + '\n', encoding='utf-8')
    queue.submit(payload)
    process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'agent_knowledge_bridge.runtime_recovery'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=25)
        assert process.returncode == 0, err.decode()
        for _ in range(100):
            if queue.summary() == {'completed': 1}:
                break
            time.sleep(.1)
        assert queue.summary() == {'completed': 1}
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        cli._stop_owned_runtime(cli._read_state(cli._paths()['state']))

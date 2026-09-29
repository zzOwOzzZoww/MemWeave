"""Telemetry is optional, native, isolated from search, and bounded after Stop."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.reuse import ReuseStore
from agent_knowledge_bridge.turn_timing import (
    TimingCollector, read_codex_timing, capture_codex_timing, MAX_TAIL_BYTES)


def native_event(turn='t', **updates):
    return {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn,
        'started_at': 1790000000, 'completed_at': 1790000010, 'duration_ms': 10000,
        'time_to_first_token_ms': 1200, **updates}}


def write(path, events=(), session='s'):
    entries = [{'type': 'session_meta', 'payload': {'id': session}}, *events]
    path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n', encoding='utf-8')


def trace(tmp_path, *, turn='t', session='s', agent='codex', project='p'):
    db_path = tmp_path / 'db.sqlite'
    store = ReuseStore(db_path)
    store.start(agent_id=agent, project_key=project, session_id=session, turn_id=turn,
                prompt='hello', records=[], retrieval_ms=1)
    return store


def capture(tmp_path, **kw):
    return capture_codex_timing(tmp_path / 'db.sqlite', project_key='p', session_id='s',
        turn_id='t', transcript_path=tmp_path / 'turn.jsonl', **kw)


@pytest.mark.parametrize('update,status', [
    ({}, 'captured'), ({'time_to_first_token_ms': 0, 'duration_ms': 0}, 'captured'),
    ({'time_to_first_token_ms': None}, 'partial'), ({'duration_ms': None}, 'partial'),
    ({'duration_ms': None, 'time_to_first_token_ms': None}, 'unavailable'),
    ({'duration_ms': -1}, 'invalid'), ({'duration_ms': float('nan')}, 'invalid'),
    ({'time_to_first_token_ms': True}, 'invalid'), ({'time_to_first_token_ms': '12'}, 'invalid'),
    ({'time_to_first_token_ms': 12000}, 'invalid'),
    ({'completed_at': 1789999999}, 'invalid'), ({'duration_ms': 2**63}, 'invalid'),
    ({'type': 'turn_aborted'}, 'aborted'),
])
def test_native_values_and_missing_evidence(tmp_path, update, status):
    path = tmp_path / 'turn.jsonl'
    write(path, [native_event(**update)])
    value = read_codex_timing(path, 's', 't')
    assert value['timing_status'] == status
    if status == 'captured':
        assert value['timing_source'] == 'codex.task_complete'
        assert value['ttft_ms'] == update.get('time_to_first_token_ms', 1200)


def test_session_and_turn_must_match(tmp_path):
    path = tmp_path / 'turn.jsonl'
    write(path, [native_event()])
    assert read_codex_timing(path, 'other', 't')['timing_status'] == 'source_mismatch'
    assert read_codex_timing(path, 's', 'other')['timing_status'] == 'pending'


def test_assistant_messages_are_not_first_token_evidence(tmp_path):
    path = tmp_path / 'turn.jsonl'
    write(path, [{'type': 'response_item', 'timestamp': '2026-09-24T00:00:00Z',
                 'payload': {'type': 'message', 'role': 'assistant', 'content': 'answer'}}])
    assert read_codex_timing(path, 's', 't') == {'timing_status': 'pending'}
    path.write_text(path.read_text(encoding='utf-8') + '{truncated', encoding='utf-8')
    assert read_codex_timing(path, 's', 't') == {'timing_status': 'pending'}


def test_large_transcript_reads_bounded_tail_and_header(tmp_path):
    path = tmp_path / 'turn.jsonl'
    write(path, [{'type': 'ignored', 'data': 'a' * MAX_TAIL_BYTES}, native_event()])
    assert read_codex_timing(path, 's', 't')['ttft_ms'] == 1200


def test_capture_updates_existing_trace_only_and_keeps_retrieval_revision(tmp_path):
    store = trace(tmp_path)
    with store.knowledge._connect() as db:
        before = tuple(db.execute('SELECT * FROM retrieval_revision').fetchone())
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    write(tmp_path / 'turn.jsonl', [native_event()])
    assert capture(tmp_path) == 'captured'
    capture(tmp_path)
    row = store.list('p')[0]
    assert row['ttft_ms'] == 1200 and row['response_total_ms'] == 10000
    assert row['completed_at'] is None  # Memory processing completion is separate.
    assert len(store.list('p')) == 1
    with store.knowledge._connect() as db:
        assert tuple(db.execute('SELECT * FROM retrieval_revision').fetchone()) == before
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")] == tables
    write(tmp_path / 'turn.jsonl', [native_event(duration_ms=90000)])
    capture(tmp_path)
    assert store.list('p')[0]['response_total_ms'] == 10000  # duplicate cannot rewrite proof


def test_telemetry_never_creates_fake_traces_or_updates_other_agents(tmp_path):
    store = trace(tmp_path, agent='claude-code')
    write(tmp_path / 'turn.jsonl', [native_event()])
    capture(tmp_path)
    assert store.list('p')[0]['ttft_ms'] is None
    assert len(store.list('p')) == 1


def test_capture_closes_database_connection(tmp_path):
    trace(tmp_path)
    write(tmp_path / 'turn.jsonl', [native_event()])
    capture(tmp_path)
    # Windows rejects deletion when sqlite leaves its file handle open.
    (tmp_path / 'db.sqlite').unlink()


def test_migration_preserves_old_trace_and_missing_is_not_zero(tmp_path):
    store = trace(tmp_path)
    # Simulate the schema before telemetry existed, preserving a real trace.
    with store.knowledge._connect() as db:
        for column in ('ttft_ms', 'response_total_ms', 'response_started_at',
                       'response_completed_at', 'timing_source', 'timing_status'):
            db.execute(f'ALTER TABLE reuse_traces DROP COLUMN {column}')
    ReuseStore(tmp_path / 'db.sqlite')
    row = store.list('p')[0]
    assert row['ttft_ms'] is None and row['timing_status'] == 'unmeasured'
    capture(tmp_path, final=True)
    assert store.list('p')[0]['timing_status'] == 'unavailable'
    m = store.metrics('p')['response_timing']['codex']
    assert m['ttft_ms'] == {'samples': 0, 'p50': None, 'p95': None}


def test_quantiles_separate_agents_and_ignore_missing_values(tmp_path):
    store = trace(tmp_path)
    trace(tmp_path, turn='t2')
    trace(tmp_path, turn='t3')
    trace(tmp_path, agent='claude-code')
    with store.knowledge._connect() as db:
        db.execute("UPDATE reuse_traces SET ttft_ms=0,response_total_ms=100,timing_status='captured' WHERE agent_id='codex' AND turn_id='t'")
        db.execute("UPDATE reuse_traces SET ttft_ms=100,response_total_ms=300,timing_status='captured' WHERE agent_id='codex' AND turn_id='t2'")
        db.execute("UPDATE reuse_traces SET ttft_ms=9999,response_total_ms=30000,timing_status='captured' WHERE agent_id='claude-code'")
    m = store.metrics('p')['response_timing']
    assert m['codex']['ttft_ms'] == {'samples': 2, 'p50': 0, 'p95': 100}
    assert m['codex']['response_total_ms']['p95'] == 300
    assert m['claude-code']['ttft_ms']['p50'] == 9999


def test_collector_waits_for_late_terminal_event_without_blocking_submit(tmp_path):
    store = trace(tmp_path)
    path = tmp_path / 'turn.jsonl'
    write(path)
    collector = TimingCollector(tmp_path / 'db.sqlite', delays=(.01, .03, .05, .1))
    try:
        assert collector.submit(agent_id='codex', project_key='p', session_id='s', turn_id='t', transcript_path=str(path))
        assert not collector.submit(agent_id='codex', project_key='p', session_id='s', turn_id='t', transcript_path=str(path))
        time.sleep(.02)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(native_event()) + '\n')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if store.list('p')[0]['ttft_ms'] == 1200:
                break
            time.sleep(.01)
        else:
            pytest.fail('Delayed telemetry not recorded')
    finally:
        collector.close()
    assert not collector._thread.is_alive()


def test_collector_queue_is_bounded_and_inactive_for_other_agents(tmp_path):
    collector = TimingCollector(tmp_path / 'db.sqlite', delays=(60,), capacity=1)
    args = dict(project_key='p', session_id='s', turn_id='t', transcript_path='unused')
    try:
        assert collector._thread is None
        assert not collector.submit(agent_id='claude-code', **args)
        assert collector.submit(agent_id='codex', **args)
        assert not collector.submit(agent_id='codex', **{**args, 'turn_id':'other'})
        assert len(collector._jobs) == 1
    finally:
        collector.close()


def test_runtime_stop_persists_timings_even_when_no_new_knowledge(tmp_path):
    path = tmp_path / 'turn.jsonl'
    write(path, [
        {'type':'event_msg','payload':{'type':'user_message','message':'hello'}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'hello again'}},
        native_event()])
    database = tmp_path / 'db.sqlite'
    app = create_app(database_path=database, api_token='test', reviewer=lambda _: {'proposals': []})
    headers = {'Authorization':'Bearer test'}
    base = {'agent_id':'codex','project_key':'p','session_id':'s','turn_id':'t'}
    with TestClient(app) as client:
        assert client.post('/v1/learning/recall', headers=headers, json={**base,'prompt':'hello'}).status_code == 200
        response = client.post('/v1/learning/turn', headers=headers, json={**base,'transcript_path':str(path)})
        assert response.status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = client.post('/v1/reuse/traces', headers=headers, json={'project_key':'p'}).json()['results'][0]
            if value['ttft_ms'] == 1200:
                break
            time.sleep(.02)
        else:
            pytest.fail('Runtime did not collect native timing')
        result = client.post('/v1/metrics', headers=headers, json={'project_key':'p'}).json()
        assert result['reuse']['response_timing']['codex']['response_total_ms']['p50'] == 10000


def test_runtime_queues_timing_even_if_learning_raises(tmp_path):
    path = tmp_path / 'missing.jsonl'
    app = create_app(database_path=tmp_path / 'db.sqlite', api_token='test', reviewer=lambda _: {'proposals': []})
    with TestClient(app) as client:
        collector = app.state.timing_collector
        response = client.post('/v1/learning/turn', headers={'Authorization':'Bearer test'}, json={
            'agent_id':'codex','project_key':'p','session_id':'s','turn_id':'t','transcript_path':str(path)})
        assert response.status_code == 422
        with collector._condition:
            assert ('p', 's', 't') in collector._jobs


def test_recall_does_not_start_timing_worker_or_read_transcript(tmp_path, monkeypatch):
    import agent_knowledge_bridge.turn_timing as timing
    monkeypatch.setattr(timing, 'read_codex_timing', lambda *a: pytest.fail('timing read on prompt path'))
    app = create_app(database_path=tmp_path / 'db.sqlite', api_token='test', reviewer=lambda _: {'proposals': []})
    with TestClient(app) as client:
        response = client.post('/v1/learning/recall', headers={'Authorization':'Bearer test'}, json={
            'agent_id':'codex','project_key':'p','session_id':'s','turn_id':'t','prompt':'hello'})
        assert response.status_code == 200
        assert app.state.timing_collector._thread is None

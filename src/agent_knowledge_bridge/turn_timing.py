"""Native turn telemetry; never part of recall, ranking or knowledge content."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

MAX_TAIL_BYTES = 524288
TIMING_COLUMNS = {
    'ttft_ms': 'INTEGER',
    'response_total_ms': 'INTEGER',
    'response_started_at': 'INTEGER',
    'response_completed_at': 'INTEGER',
    'timing_source': 'TEXT',
    'timing_status': "TEXT NOT NULL DEFAULT 'unmeasured'",
}


def read_codex_timing(path, session_id, turn_id):
    """Only a matching native terminal event can establish durations.

    Session metadata and turn identity must both match. A message timestamp is
    not first-token telemetry. Tail truncation and asynchronous flushes may
    leave a sample unavailable; never infer a duration from the Hook finish.
    """
    try:
        with Path(path).open('rb') as stream:
            header = json.loads(stream.readline(65536))
            meta = header.get('payload') or {}
            if (header.get('type') != 'session_meta'
                    or str(meta.get('id') or meta.get('session_id') or '') != session_id):
                return {'timing_status': 'source_mismatch'}
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_TAIL_BYTES))
            if size > MAX_TAIL_BYTES:
                # Discard a partial line without an unbounded readline.
                stream.readline(MAX_TAIL_BYTES)
            raw = stream.read(MAX_TAIL_BYTES)
    except (OSError, ValueError, TypeError, AttributeError):
        return {'timing_status': 'pending'}
    match = None
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(item, dict) or item.get('type') != 'event_msg':
            continue
        payload = item.get('payload')
        if not isinstance(payload, dict) or payload.get('turn_id') != turn_id:
            continue
        if payload.get('type') in {'task_complete', 'turn_aborted'}:
            match = payload
    if match is None:
        return {'timing_status': 'pending'}
    if match['type'] == 'turn_aborted':
        return {'timing_status': 'aborted'}
    values = {target: match.get(source) for target, source in (
        ('ttft_ms', 'time_to_first_token_ms'), ('response_total_ms', 'duration_ms'),
        ('response_started_at', 'started_at'), ('response_completed_at', 'completed_at'))}
    if any(v is not None and (type(v) is not int or not 0 <= v < 2**63) for v in values.values()):
        return {'timing_status': 'invalid'}
    first, total = values['ttft_ms'], values['response_total_ms']
    if first is not None and total is not None and first > total:
        return {'timing_status': 'invalid'}
    start, end = values['response_started_at'], values['response_completed_at']
    if start is not None and end is not None and end < start:
        return {'timing_status': 'invalid'}
    status = ('captured' if first is not None and total is not None else
              'partial' if first is not None or total is not None else 'unavailable')
    return {**values, 'timing_status': status, 'timing_source': 'codex.task_complete'}


def capture_codex_timing(database_path, *, project_key, session_id, turn_id, transcript_path,
                         final=False):
    """One bounded read and one keyed update, outside the retrieval path."""
    values = read_codex_timing(transcript_path, session_id, turn_id)
    status = values['timing_status']
    if status == 'pending' and not final:
        return status
    if status == 'pending':
        values['timing_status'] = 'unavailable'
    with closing(sqlite3.connect(str(database_path), timeout=0.2)) as db, db:
        fields = ','.join(name + '=?' for name in values)
        db.execute('UPDATE reuse_traces SET ' + fields + '''
            WHERE agent_id='codex' AND project_key=? AND session_id=? AND turn_id=?
              AND timing_status NOT IN ('captured','partial')''',
            (*values.values(), project_key, session_id, turn_id))
    return values['timing_status']


class TimingCollector:
    """One lazy, bounded worker for terminal events flushed after Stop returns.

    It is idle with no jobs, reads only the supplied transcript, and never
    blocks the prompt path. No model calls, per-token writes or global scans.
    Pending tasks live in memory; interrupted collection remains unmeasured.
    """
    def __init__(self, database_path, *, delays=(0.2, 0.5, 1, 2, 4, 8, 16), capacity=64):
        self.database_path = database_path
        self.delays, self.capacity = delays, capacity
        self._condition = threading.Condition()
        self._jobs = {}
        self._thread = None
        self._closed = False

    def submit(self, *, agent_id, project_key, session_id, turn_id, transcript_path):
        if agent_id != 'codex' or not turn_id or not transcript_path:
            return False
        key = (project_key, session_id, turn_id)
        with self._condition:
            if self._closed or key in self._jobs or len(self._jobs) >= self.capacity:
                return False
            self._jobs[key] = (time.monotonic() + self.delays[0], 0, transcript_path)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='memweave-turn-timing', daemon=True)
                self._thread.start()
            self._condition.notify()
        return True

    def _run(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                if not self._jobs:
                    self._condition.wait()
                    continue
                key = min(self._jobs, key=lambda k: self._jobs[k][0])
                due, attempt, path = self._jobs[key]
                wait = due - time.monotonic()
                if wait > 0:
                    self._condition.wait(wait)
                    continue
            final = attempt + 1 == len(self.delays)
            try:
                result = capture_codex_timing(self.database_path, project_key=key[0],
                    session_id=key[1], turn_id=key[2], transcript_path=path, final=final)
            except (OSError, ValueError, sqlite3.Error):
                result = 'pending'
            with self._condition:
                if self._closed:
                    return
                if result != 'pending' or final:
                    self._jobs.pop(key, None)
                else:
                    self._jobs[key] = (time.monotonic() + self.delays[attempt + 1], attempt + 1, path)

    def close(self):
        with self._condition:
            self._closed = True
            self._jobs.clear()
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout=1)


def timing_metrics(rows, percentile):
    """Read only by the dashboard metrics endpoint, separated by Agent."""
    result = {}
    for agent in sorted({r['agent_id'] for r in rows}):
        agent_rows = [r for r in rows if r['agent_id'] == agent]
        valid = [r for r in agent_rows if r['timing_status'] in {'captured', 'partial'}]
        entry = {'turns': len(agent_rows), 'measured_turns': len(valid),
                 'source': 'codex.task_complete' if agent == 'codex' else None}
        for field in ('ttft_ms', 'response_total_ms'):
            values = [r[field] for r in valid if r[field] is not None]
            entry[field] = {'samples': len(values), 'p50': percentile(values, .5),
                            'p95': percentile(values, .95)}
        result[agent] = entry
    return result

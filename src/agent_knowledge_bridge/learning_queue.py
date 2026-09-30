"""Bounded persistent post-turn work, deliberately off the prompt path."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .learning import LearningStore
from .claude_transcript import redact_text


class LearningQueue:
    def __init__(self, database_path: Path, handler):
        self.store = LearningStore(database_path)
        self.handler = handler
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        with self.store.knowledge._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS learning_jobs (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, ready_at REAL NOT NULL,
                updated_at REAL NOT NULL, error TEXT NOT NULL DEFAULT '')''')
            # A lease lasts longer than the model's bounded request timeout.
            db.execute("UPDATE learning_jobs SET status='queued',ready_at=? WHERE status='running' AND updated_at<?",
                       (time.time(), time.time()-300))

    def submit(self, payload: dict) -> dict:
        # Store a local reference, not raw assistant/user text or credentials.
        if payload.get('turn') is not None:
            raise ValueError('inline turn cannot be persisted in the learning queue')
        clean = {k: payload.get(k) for k in ('agent_id','project_key','session_id','turn_id','cwd','transcript_path')}
        clean['transcript_format'] = payload.get('transcript_format', 'auto')
        path = Path(clean.get('transcript_path') or '')
        if not path.is_file():
            raise ValueError('学习任务需要可读取的本地 transcript；未接受空路径')
        clean['transcript_end'] = path.stat().st_size
        if clean['transcript_format'] == 'gemini' or (
                clean['transcript_format'] == 'auto' and clean['agent_id'] == 'gemini-cli'):
            from .gemini_transcript import freeze_gemini_turn
            clean.update(freeze_gemini_turn(path, clean['transcript_end']))
        identity = dict(clean)
        if clean['turn_id']:
            identity.pop('transcript_end')
        if not clean['turn_id']:
            identity['file_stamp'] = (path.stat().st_mtime_ns, path.stat().st_size)
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.store.knowledge._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT status FROM learning_jobs WHERE id=?', (key,)).fetchone()
            if previous:
                return {'status': previous['status'], 'duplicate': True}
            if db.execute("SELECT COUNT(*) FROM learning_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 256:
                raise ValueError('学习队列已满，请稍后重试')
            db.execute("INSERT INTO learning_jobs(id,payload,status,ready_at,updated_at) VALUES(?,?,'queued',?,?)",
                       (key,json.dumps(clean,ensure_ascii=False),now,now))
            # Completed job references are operational metadata, not durable knowledge.
            db.execute("DELETE FROM learning_jobs WHERE status IN ('completed','failed','disabled') AND id NOT IN "
                       "(SELECT id FROM learning_jobs WHERE status IN ('completed','failed','disabled') ORDER BY updated_at DESC LIMIT 200)")
        self.wake.set()
        return {'status':'queued', 'duplicate':False}

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name='memweave-learning')
            self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self):
        while not self.stop.is_set():
            try:
                processed = self.process_one()
            except sqlite3.Error:
                processed = False  # A busy database must not kill the worker.
            if not processed:
                self.wake.wait(1)
                self.wake.clear()

    def process_one(self) -> bool:
        now=time.time()
        with self.store.knowledge._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE learning_jobs SET status='queued',ready_at=? WHERE status='running' AND updated_at<?",(now,now-300))
            job=db.execute("SELECT * FROM learning_jobs WHERE status='queued' AND ready_at<=? ORDER BY ready_at LIMIT 1",(now,)).fetchone()
            if job is None:
                return False
            db.execute("UPDATE learning_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",(now,job['id']))
        error=''
        try:
            payload=json.loads(job['payload'])
            if not self.store.knowledge.agent_allowed(payload['agent_id']):
                status='disabled'
            else:
                result=self.handler(payload)
                if result.get('status') == 'duplicate_turn':
                    # A competing/expired run is not proof this job completed.
                    if result.get('run_status') != 'completed':
                        raise RuntimeError('previous learning attempt is unfinished')
                status='disabled' if result.get('status')=='disabled' else 'completed'
        except Exception as exc:
            error=redact_text(str(exc),500)
            status='failed' if job['attempts']+1>=3 else 'queued'
        with self.store.knowledge._connect() as db:
            db.execute('UPDATE learning_jobs SET status=?,error=?,ready_at=?,updated_at=? WHERE id=?',
                       (status,error,time.time()+min(60,2**(job['attempts']+1)),time.time(),job['id']))
        return True

    def summary(self) -> dict:
        with self.store.knowledge._connect() as db:
            return {row['status']:row['n'] for row in db.execute('SELECT status,COUNT(*) n FROM learning_jobs GROUP BY status')}

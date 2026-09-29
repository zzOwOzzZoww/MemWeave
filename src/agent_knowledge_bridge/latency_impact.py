"""Explicit paired latency experiments. Never imported by the recall path.

Samples are imported from controlled experiments; ordinary conversations are
not retroactively assigned to an on/off group. This store makes matching and
statistics auditable, not network/model variance disappear.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


MAX_PAIRS_PER_PROJECT = 1000
CONDITION_KEYS = ('model', 'model_config_hash', 'prompt_hash', 'base_context_hash',
                  'environment_hash', 'timing_source', 'cache_policy')


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS latency_impact_pairs (
        project_key TEXT NOT NULL, experiment_id TEXT NOT NULL, pair_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, baseline_mode TEXT NOT NULL, execution_order TEXT NOT NULL,
        conditions_json TEXT NOT NULL, on_json TEXT NOT NULL, off_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(project_key,experiment_id,pair_id));
        CREATE INDEX IF NOT EXISTS idx_latency_impact_project
        ON latency_impact_pairs(project_key,created_at);
    ''')


def _validate_sample(sample):
    for key in ('session_id', 'turn_id', *CONDITION_KEYS):
        value = sample.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise ValueError(f'{key} must be a nonempty bounded string')
        if key.endswith('_hash') and not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ValueError(f'{key} must be SHA256, never raw context or credentials')
    if sample['timing_source'] != 'codex.task_complete':
        raise ValueError('only verified Codex native timing is currently supported')
    for key in ('ttft_ms', 'response_total_ms'):
        value = sample.get(key)
        if type(value) is not int or not 0 <= value < 2**63:
            raise ValueError(f'{key} must be a nonnegative native integer')
    if sample['ttft_ms'] > sample['response_total_ms']:
        raise ValueError('first token cannot occur after turn completion')
    return {key: sample[key] for key in ('session_id', 'turn_id', *CONDITION_KEYS,
                                        'ttft_ms', 'response_total_ms')}


def record_pair(database_path, *, project_key, experiment_id, pair_id, agent_id,
                baseline_mode, execution_order, conditions_confirmed, on, off):
    if conditions_confirmed is not True:
        raise ValueError('controlled experiment conditions must be explicitly confirmed')
    for value in (project_key, experiment_id, pair_id):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
            raise ValueError('invalid experiment/project identifier')
    if agent_id != 'codex' or baseline_mode not in {'framework_absent', 'hook_bypass'}:
        raise ValueError('unsupported agent or baseline mode')
    if execution_order not in {'on_off', 'off_on'}:
        raise ValueError('record actual pair execution order')
    on, off = _validate_sample(on), _validate_sample(off)
    if on['session_id'] == off['session_id']:
        raise ValueError('use separate sessions with the same initial context')
    if any(on[k] != off[k] for k in CONDITION_KEYS):
        raise ValueError('on/off model, prompt, context, environment or timing conditions differ')
    dump = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
    conditions = {k: on[k] for k in CONDITION_KEYS}
    # Prompt varies by pair, but all other controlled conditions must stay fixed
    # in an experiment. Separate experiment IDs prevent unlike runs being pooled.
    with closing(sqlite3.connect(str(database_path), timeout=5)) as db, db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT agent_id,baseline_mode,conditions_json FROM latency_impact_pairs '
                              'WHERE project_key=? AND experiment_id=? LIMIT 1',
                              (project_key, experiment_id)).fetchone()
        if existing:
            old = json.loads(existing[2])
            if existing[:2] != (agent_id, baseline_mode) or any(
                old[k] != conditions[k] for k in CONDITION_KEYS if k != 'prompt_hash'):
                raise ValueError('experiment conditions changed; use a new experiment_id')
        values = (agent_id, baseline_mode, execution_order, dump(conditions), dump(on), dump(off))
        previous = db.execute('SELECT agent_id,baseline_mode,execution_order,conditions_json,on_json,off_json '
                              'FROM latency_impact_pairs WHERE project_key=? AND experiment_id=? AND pair_id=?',
                              (project_key, experiment_id, pair_id)).fetchone()
        if previous:
            if previous != values:
                raise ValueError('pair already recorded with different evidence')
            return {'status': 'already_recorded'}
        # A native turn cannot be reused to inflate the number of independent pairs.
        for sample in (on, off):
            used = db.execute('''SELECT 1 FROM latency_impact_pairs
                WHERE (json_extract(on_json,'$.session_id')=? AND json_extract(on_json,'$.turn_id')=?)
                   OR (json_extract(off_json,'$.session_id')=? AND json_extract(off_json,'$.turn_id')=?) LIMIT 1''',
                (sample['session_id'], sample['turn_id']) * 2).fetchone()
            if used:
                raise ValueError('native turn is already used in a recorded pair')
        db.execute('INSERT INTO latency_impact_pairs VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (project_key, experiment_id, pair_id, *values,
                    datetime.now(timezone.utc).isoformat(timespec='microseconds')))
        db.execute('''DELETE FROM latency_impact_pairs WHERE project_key=? AND rowid NOT IN
            (SELECT rowid FROM latency_impact_pairs WHERE project_key=? ORDER BY created_at DESC,rowid DESC LIMIT ?)''',
            (project_key, project_key, MAX_PAIRS_PER_PROJECT))
    return {'status': 'recorded', 'ttft_delta_ms': on['ttft_ms'] - off['ttft_ms'],
            'total_delta_ms': on['response_total_ms'] - off['response_total_ms']}


def metrics(db, project_key):
    """Most recent experiment only; nearest-rank percentiles of paired deltas."""
    latest = db.execute('SELECT experiment_id FROM latency_impact_pairs WHERE project_key=? '
                        'ORDER BY created_at DESC,rowid DESC LIMIT 1', (project_key,)).fetchone()
    rows = [] if not latest else db.execute('SELECT * FROM latency_impact_pairs '
        'WHERE project_key=? AND experiment_id=?', (project_key, latest[0])).fetchall()
    result = {'status': 'no_controlled_pairs', 'pairs': len(rows), 'baseline_mode': None,
              'ttft_delta_ms': {'p50': None, 'p95': None},
              'total_delta_ms': {'p50': None, 'p95': None}, 'order_balanced': False,
              'evidence': 'operator_confirmed_conditions_native_timing'}
    if not rows:
        return result
    on, off = [json.loads(r['on_json']) for r in rows], [json.loads(r['off_json']) for r in rows]
    def distribution(values):
        values = sorted(values)
        return {label: values[max(0, math.ceil(len(values) * p) - 1)]
                for label, p in (('p50', .5), ('p95', .95))}
    result.update(status='limited_sample' if len(rows) < 20 else 'measured',
                  baseline_mode=rows[0]['baseline_mode'], experiment_id=latest[0],
                  order_balanced={r['execution_order'] for r in rows} == {'on_off', 'off_on'})
    for source, name in (('ttft_ms', 'ttft_delta_ms'), ('response_total_ms', 'total_delta_ms')):
        result[name] = distribution([a[source] - b[source] for a, b in zip(on, off)])
    result['on_ttft_ms'] = distribution([r['ttft_ms'] for r in on])
    result['off_ttft_ms'] = distribution([r['ttft_ms'] for r in off])
    return result

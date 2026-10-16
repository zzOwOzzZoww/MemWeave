"""Evidence inspection -> immutable preview -> guarded execution -> guarded undo.

This is a control plane, not another extractor. Model-supplied rationales never
grant approval. Only exact candidate deduplication is eligible without an
operator decision; all other actions require the separate local console.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from . import knowledge_versions as versions
from .claude_transcript import redact_text
from .decisions import obsolete


POLICY = "governance-control-v1"
STATE_FIELDS = ("status", "superseded_by", "valid_from", "valid_until",
                "adopted_count", "verified_count", "rejected_count", "updated_at")
ACTIONS = {"approve", "quarantine", "archive", "merge", "supersede"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class GovernanceControl:
    def __init__(self, store, *, actor, project_key):
        store._validate_agent_id(actor)
        store._validate_project_key(project_key)
        self.store, self.actor, self.project = store, actor, project_key
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS governance_plans (
                id TEXT PRIMARY KEY, project_key TEXT NOT NULL, actor TEXT NOT NULL,
                policy TEXT NOT NULL, status TEXT NOT NULL,
                actions_json TEXT NOT NULL, guards_json TEXT NOT NULL,
                result_json TEXT, approved_by TEXT, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, applied_at TEXT, undone_at TEXT)""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_governance_plans
                ON governance_plans(project_key,created_at)""")

    def _allowed(self):
        self.store.assert_agent_allowed(self.actor)

    def _record(self, db, key):
        row = db.execute("""SELECT * FROM knowledge_records WHERE id=?
            AND (project_key=? OR scope='user')""", (key, self.project)).fetchone()
        if not row:
            raise ValueError("knowledge record not found in this project")
        return dict(row)

    def _guard(self, db, row):
        # Keep hashes, not second copies of knowledge or private source text.
        evidence = [dict(r) for r in db.execute(
            "SELECT * FROM knowledge_evidence WHERE knowledge_id=? ORDER BY id", (row['id'],))]
        audit = [dict(r) for r in db.execute(
            "SELECT * FROM lifecycle_audit WHERE knowledge_id=? ORDER BY id", (row['id'],))]
        peers = [dict(r) for r in versions.peers(db, row)]
        return _hash([row, evidence, audit, sorted(peers, key=lambda r: r['id'])])

    def _evaluate(self, db, action):
        row = self._record(db, action['knowledge_id'])
        kind = action['action']
        target = self._record(db, action['target_id']) if action.get('target_id') else None
        if row['superseded_by']:
            raise ValueError("a superseded record cannot be managed as the current version")
        if kind in {'approve', 'supersede'}:
            if row['status'] not in {'candidate', 'quarantined'} or obsolete(row):
                raise ValueError("only current, reviewable candidates can be approved")
            if row['claim_topic'] and row['claim_fingerprint'] != versions.fingerprint(row):
                raise ValueError("assertion source changed; resubmit the candidate")
            peers = versions.peers(db, row)
            if kind == 'approve' and peers:
                raise ValueError("conflicting versions require an explicit supersede action")
            if kind == 'supersede' and (not target or {p['id'] for p in peers} != {target['id']}):
                raise ValueError("supersede must select the exact conflicting version")
            if target and target['claim_fingerprint'] != versions.fingerprint(target):
                raise ValueError("conflicting assertion source changed")
        elif kind == 'quarantine':
            if row['status'] not in {'candidate', 'active', 'stale'}:
                raise ValueError("record is not eligible for isolation")
        elif kind == 'archive':
            if row['status'] not in {'active', 'stale'}:
                raise ValueError("only serving records can be retired")
        elif kind == 'merge':
            if (not target or row['status'] != 'candidate'
                    or target['status'] not in {'active', 'stale'}
                    or target['verified_count'] <= 0 or versions.blocked(target)
                    or obsolete(target) or row['content'] != target['content']
                    or row['knowledge_type'] != target['knowledge_type']
                    or row['scope'] != target['scope']
                    or (row['scope'] == 'project' and row['project_key'] != target['project_key'])):
                raise ValueError("merge requires an exact candidate duplicate of accepted knowledge")
        if target and (target['id'] == row['id'] or target['scope'] != row['scope']
                       or (row['scope'] == 'project' and target['project_key'] != row['project_key'])):
            raise ValueError("version targets must have the same scope")
        safe = kind == 'merge' and row['scope'] == 'project'
        return [row] + ([target] if target else []), safe

    def preview(self, actions):
        self._allowed()
        if not isinstance(actions, list) or not 1 <= len(actions) <= 20:
            raise ValueError("provide 1..20 governance actions")
        cleaned, guards, touched = [], {}, set()
        timestamp = self.store.clock()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in actions:
                if not isinstance(item, dict) or set(item) - {'action', 'knowledge_id', 'target_id', 'reason'}:
                    raise ValueError("unsupported governance action fields")
                if item.get('action') not in ACTIONS:
                    raise ValueError("unsupported governance action")
                if not isinstance(item.get('knowledge_id'), str):
                    raise ValueError("knowledge_id is required")
                if bool(item.get('target_id')) != (item['action'] in {'merge', 'supersede'}):
                    raise ValueError("only merge/supersede require a target_id")
                if item.get('target_id') is not None and not isinstance(item['target_id'], str):
                    raise ValueError("invalid target_id")
                reason = item.get('reason', '').strip() if isinstance(item.get('reason', ''), str) else ''
                if not 1 <= len(reason) <= 500 or redact_text(reason, 501) != reason:
                    raise ValueError("provide a bounded, credential-free rationale")
                action = {k: item[k] for k in ('action', 'knowledge_id', 'target_id') if item.get(k)}
                action['reason'] = reason
                rows, safe = self._evaluate(db, action)
                if any(row['id'] in touched for row in rows):
                    raise ValueError("actions must not overlap; preview dependent changes separately")
                for row in rows:
                    touched.add(row['id'])
                    guards[row['id']] = self._guard(db, row)
                action['requires_approval'] = not safe
                cleaned.append(action)
            plan_id = 'gp_' + uuid.uuid4().hex
            db.execute("""INSERT INTO governance_plans
                (id,project_key,actor,policy,status,actions_json,guards_json,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (plan_id, self.project, self.actor, POLICY,
                'preview', _json(cleaned), _json(guards), timestamp,
                (_now(timestamp) + timedelta(hours=24)).isoformat()))
        return self.get(plan_id)

    def _plan(self, db, plan_id):
        row = db.execute('SELECT * FROM governance_plans WHERE id=? AND project_key=?',
                         (plan_id, self.project)).fetchone()
        if not row or (row['actor'] != self.actor and self.actor != 'memweave-console'):
            raise ValueError("governance plan not found")
        return dict(row)

    def _check(self, db, plan):
        if plan['policy'] != POLICY or _now(plan['expires_at']) <= _now(self.store.clock()):
            raise ValueError("preview expired; generate a new plan")
        for key, guard in json.loads(plan['guards_json']).items():
            if self._guard(db, self._record(db, key)) != guard:
                raise ValueError("knowledge or evidence changed; generate a new preview")
        for action in json.loads(plan['actions_json']):
            self._evaluate(db, action)

    def _view(self, db, plan):
        actions = json.loads(plan['actions_json'])
        records = []
        for key in json.loads(plan['guards_json']):
            row = db.execute('SELECT id,title,status,scope FROM knowledge_records WHERE id=?', (key,)).fetchone()
            records.append(dict(row) if row else {'id': key, 'removed': True})
        return {k: plan[k] for k in ('id', 'actor', 'status', 'created_at', 'expires_at',
                'approved_by', 'applied_at', 'undone_at')} | {
            'actions': actions, 'records': records,
            'requires_approval': any(a['requires_approval'] for a in actions),
            'review_path': '/governance', 'policy': POLICY}

    def get(self, plan_id):
        self._allowed()
        with self.store._connect() as db:
            return self._view(db, self._plan(db, plan_id))

    def list(self, limit=20):
        self._allowed()
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        with self.store._connect() as db:
            rows = db.execute("""SELECT * FROM governance_plans WHERE project_key=?
                AND (actor=? OR ?='memweave-console') ORDER BY created_at DESC,rowid DESC LIMIT ?""",
                (self.project, self.actor, self.actor, limit)).fetchall()
            return {'results': [self._view(db, dict(row)) for row in rows]}

    def approve(self, plan_id):
        """Local console only. Deliberately absent from the MCP tool surface."""
        self._allowed()
        if self.actor != 'memweave-console':
            raise ValueError("operator approval requires the local management console")
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            plan = self._plan(db, plan_id)
            if plan['status'] not in {'preview', 'approved'}:
                raise ValueError("plan is no longer awaiting approval")
            self._check(db, plan)
            db.execute("UPDATE governance_plans SET approved_by=?,status='approved' WHERE id=?",
                       (self.actor, plan_id))
        return self.get(plan_id)

    def _audit(self, db, plan_id, row, status, timestamp, verb):
        db.execute("""INSERT INTO lifecycle_audit
            (id,knowledge_id,from_status,to_status,reason,actor,created_at) VALUES (?,?,?,?,?,?,?)""",
            ('lc_' + uuid.uuid4().hex, row['id'], row['status'], status,
             f'{verb}; governance plan {plan_id}', self.actor, timestamp))

    def _write(self, db, plan_id, row, values, timestamp, verb):
        values = {**values, 'updated_at': timestamp}
        db.execute('UPDATE knowledge_records SET ' + ','.join(f'{k}=?' for k in values) + ' WHERE id=?',
                   (*values.values(), row['id']))
        self._audit(db, plan_id, row, values['status'], timestamp, verb)

    def apply(self, plan_id):
        self._allowed()
        timestamp = self.store.clock()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            plan = self._plan(db, plan_id)
            if plan['status'] == 'applied':
                return self._view(db, plan)
            if plan['status'] not in {'preview', 'approved'}:
                raise ValueError("plan cannot be applied")
            self._check(db, plan)
            actions = json.loads(plan['actions_json'])
            if any(a['requires_approval'] for a in actions) and not plan['approved_by']:
                raise ValueError("operator approval required in the management console")
            keys = json.loads(plan['guards_json'])
            before = {key: {k: self._record(db, key)[k] for k in STATE_FIELDS} for key in keys}
            evidence_ids = []
            for action in actions:
                rows, _ = self._evaluate(db, action)
                row = rows[0]
                kind = action['action']
                status = {'approve': 'active', 'supersede': 'active', 'quarantine': 'quarantined',
                          'archive': 'archived', 'merge': 'archived'}[kind]
                values = {'status': status}
                outcome, evidence_kind = kind, 'observation'
                if kind in {'approve', 'supersede'}:
                    values.update(verified_count=row['verified_count'] + 1,
                                  adopted_count=row['adopted_count'] + 1,
                                  valid_from=timestamp, valid_until=None)
                    outcome, evidence_kind = 'verified', 'user_approval'
                elif kind == 'quarantine':
                    values['rejected_count'] = row['rejected_count'] + 1
                    outcome, evidence_kind = 'rejected', 'user_approval'
                elif kind == 'merge':
                    values.update(superseded_by=rows[1]['id'], valid_until=timestamp)
                    outcome, evidence_kind = 'duplicate', 'duplicate'
                self._write(db, plan_id, row, values, timestamp, kind)
                if kind == 'supersede':
                    self._write(db, plan_id, rows[1], {'status': 'archived', 'superseded_by': row['id'],
                                'valid_until': timestamp}, timestamp, 'superseded')
                evidence_id = 'ev_' + uuid.uuid4().hex
                db.execute("""INSERT INTO knowledge_evidence
                    (id,knowledge_id,agent_id,outcome,summary,evidence_kind,evidence_ref,
                     status_before,status_after,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (evidence_id, row['id'], self.actor, outcome,
                     'Exact duplicate retained by version link.' if kind == 'merge' else 'Operator-approved governance action.',
                     evidence_kind, 'governance-plan:' + plan_id, row['status'], status, timestamp))
                evidence_ids.append(evidence_id)
            for key in keys:
                versions.refresh(db, self._record(db, key))
            after = {key: self._guard(db, self._record(db, key)) for key in keys}
            db.execute("""UPDATE governance_plans SET status='applied',applied_at=?,result_json=? WHERE id=?""",
                       (timestamp, _json({'before': before, 'after': after, 'evidence_ids': evidence_ids}), plan_id))
        return self.get(plan_id)

    def undo(self, plan_id):
        """Restore only our state changes, never overwrite subsequent feedback."""
        self._allowed()
        if self.actor != 'memweave-console':
            raise ValueError("undo requires the local management console")
        timestamp = self.store.clock()
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            plan = self._plan(db, plan_id)
            if plan['status'] == 'undone':
                return self._view(db, plan)
            if plan['status'] != 'applied':
                raise ValueError("only an applied plan can be undone")
            result = json.loads(plan['result_json'])
            for key, guard in result['after'].items():
                if self._guard(db, self._record(db, key)) != guard:
                    raise ValueError("subsequent knowledge activity prevents automatic undo; review current state")
            for key, values in result['before'].items():
                self._write(db, plan_id, self._record(db, key), values, timestamp, 'undo')
            for evidence_id in result['evidence_ids']:
                # Preserve the historical entry but revoke its approval/rejection.
                db.execute("UPDATE knowledge_evidence SET outcome='reverted' WHERE id=?", (evidence_id,))
            for key in result['before']:
                versions.refresh(db, self._record(db, key))
            db.execute("UPDATE governance_plans SET status='undone',undone_at=? WHERE id=?", (timestamp, plan_id))
        return self.get(plan_id)

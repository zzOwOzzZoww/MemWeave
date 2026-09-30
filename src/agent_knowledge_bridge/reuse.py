"""Per-turn reuse evidence. Emission, citation and constraint checks are distinct.

No transcript or artifact contents are persisted here. A passing check proves an
output satisfies a recalled constraint, not that memory caused task success.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path

from agent_knowledge_bridge.agent_registry import supported_agent
from agent_knowledge_bridge.store import KnowledgeStore, utc_now
from agent_knowledge_bridge.decisions import query_intent, rejection_reason, POLICY_VERSION
from agent_knowledge_bridge.knowledge_versions import blocked
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.evidence import (
    apply_answerability_gate, filter_retrieval_candidates, normalize_subject_terms,
)
from agent_knowledge_bridge.retrieval_pipeline import Candidate
from agent_knowledge_bridge.turn_timing import TIMING_COLUMNS, timing_metrics
from agent_knowledge_bridge.experiences import (
    contract_from_content, applicable, display_contract, observe_contract, record_outcome,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_label(agent_id: str) -> str:
    """Human-readable provenance for chat; technical IDs stay in the trace."""
    spec = supported_agent(agent_id)
    return spec["display_name"] if spec else re.sub(r"[`\r\n<>]", "", agent_id)[:80]


def observed_source_labels(assistant_text: str) -> set[str]:
    """Observe standalone source badges, excluding quoted/fenced examples.

    A source badge cannot identify which of that Agent's records was used.
    It must never be promoted to per-record citation or constraint-pass credit.
    """
    labels: set[str] = set()
    fence = None
    text = re.sub(r"<!--[\s\S]*?-->", "", assistant_text)
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = re.fullmatch(r"[ \t]*`来源[：:][ \t]*([^`\r\n]+)`[ \t]*", line)
        if match:
            labels.add(match.group(1).strip())
    return labels


def percentile(values, percent):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percent) - 1)], 3)


def constraint(content):
    """Opt-in JSON top-level equality validator, not executable agent code."""
    try:
        spec = json.loads(content).get("reuse_check")
        if (isinstance(spec, dict) and spec.get("kind") == "json_equals"
                and isinstance(spec.get("artifact"), str)
                and isinstance(spec.get("equals"), dict) and spec["equals"]):
            return spec
    except (ValueError, AttributeError, TypeError):
        pass
    return None


def read_artifact(workspace, spec):
    if not workspace:
        return {"status": "no_workspace"}, None
    try:
        root = Path(workspace).resolve(strict=True)
        relative = Path(spec["artifact"])
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            return {"status": "unsafe_path"}, None
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            return {"status": "unsafe_path"}, None
        if not path.is_file():
            return {"status": "missing"}, None
        # Bound memory even if the file grows between stat and read.
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            return {"status": "too_large"}, None
        stamp = {"status": "read", "sha256": hashlib.sha256(raw).hexdigest()}
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            return {**stamp, "status": "invalid_json"}, None
        return stamp, value
    except (OSError, ValueError):
        return {"status": "unreadable"}, None


class ReuseStore:
    def __init__(self, database_path):
        self.knowledge = KnowledgeStore(database_path)
        with self.knowledge._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reuse_traces (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                    project_key TEXT NOT NULL, session_id TEXT NOT NULL,
                    turn_id TEXT, prompt_hash TEXT NOT NULL,
                    workspace TEXT NOT NULL, context_hash TEXT NOT NULL, context_text TEXT NOT NULL,
                    context_chars INTEGER NOT NULL, retrieval_ms REAL NOT NULL,
                    items_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    completed_at TEXT, association TEXT, task_success INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reuse_turn
                  ON reuse_traces(agent_id, project_key, session_id, turn_id)
                  WHERE turn_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_reuse_project
                  ON reuse_traces(project_key, created_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(reuse_traces)")}
            additions = {'transcript_boundary': 'TEXT', **TIMING_COLUMNS}
            if any(name not in columns for name in additions):
                db.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in db.execute("PRAGMA table_info(reuse_traces)")}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE reuse_traces ADD COLUMN {name} {definition}")

    def existing(self, agent_id, project_key, session_id, turn_id):
        if not turn_id:
            return None
        with self.knowledge._connect() as db:
            row = db.execute("""SELECT * FROM reuse_traces WHERE agent_id=?
                AND project_key=? AND session_id=? AND turn_id=?""",
                (agent_id, project_key, session_id, turn_id)).fetchone()
        return dict(row) if row else None

    def live_context(self, trace, *, db=None):
        """A trace is historical evidence, never an authorization cache."""
        if db is None:
            with self.knowledge._connect() as connection:
                return self.live_context(trace, db=connection)
        agent = db.execute('SELECT enabled FROM agent_registry WHERE agent_id=?', (trace['agent_id'],)).fetchone()
        if agent and not agent['enabled']:
            return ''
        items = [item for item in json.loads(trace['items_json']) if item.get('emitted')]
        if not items or any(item.get('removed') for item in items):
            return ''
        if any(item.get('decision_policy') != POLICY_VERSION for item in items):
            return ''
        ids = [item['knowledge_id'] for item in items]
        live = {row['id']: row for row in db.execute(
            "SELECT * FROM knowledge_records WHERE status IN ('active','stale','archived') "
            "AND (scope='user' OR project_key=?) AND id IN (" + ','.join('?' for _ in ids) + ')',
            [trace['project_key'], *ids])}
        if any(item['knowledge_id'] not in live or digest(live[item['knowledge_id']]['content']) != item['content_hash'] for item in items):
            return ''
        for item in items:
            record = live[item['knowledge_id']]
            historical = item.get('historical', False)
            if blocked(record, historical=historical) or (
                record['status']=='archived' and not (historical and record['superseded_by'])
            ):
                return ''
        return trace['context_text']

    def start(self, *, agent_id, project_key, session_id, prompt, records,
              retrieval_ms, turn_id=None, workspace="", budget=4000, transcript_boundary=None,
              validate_live_records=False, record_hits=False, record_limit=None):
        if record_limit is not None and (not isinstance(record_limit, int) or record_limit < 1):
            raise ValueError('record_limit must be a positive integer')
        trace_id = "rt_" + uuid.uuid4().hex[:20]
        header = (f'<memweave_context trace_id="{trace_id}">\n'
                  "Use only relevant knowledge; verify results normally. "
                  "All IDs and metadata in this block are internal audit data. "
                  "Never print knowledge IDs, trace IDs, session IDs, or raw metadata "
                  "in user-facing answers (including code spans, links, or HTML comments). "
                  "If you actually use recalled knowledge and the requested answer format permits, "
                  "end your answer with one standalone inline-code source badge per used Agent, "
                  "using that item's source_label: `来源：Claude Code` or `来源：Codex`. "
                  "Deduplicate sources; omit badges for unused knowledge. "
                  "Do not cite internal IDs even if earlier turns did. "
                  "Source attribution is not proof of per-item use or task success.\n")
        footer = "</memweave_context>"
        context = header
        items = []
        blocks = {}
        records_by_id = {r['id']: r for r in records}
        emitted_count = 0
        recoveries = {r['id']: r for r in records if r.get('lfhv_recovery')}
        # Recovery always requires live validation, including non-Adapter callers.
        validate_live_records = validate_live_records or bool(recoveries)
        intent = query_intent(prompt, project_key=project_key)
        # A sibling arrives as a fact whose subject lives in another record. Told
        # only what it says, the reader learns the phrase and not the work it
        # belongs to -- exactly the failure this line exists to prevent. Resolve
        # the anchor's title once here so the block can name it.
        titles = {record["id"]: record.get("title") or "" for record in records}
        for rank, record in enumerate(records, 1):
            experience = contract_from_content(record['content'])
            search_terms = str(record.get('search_terms') or '').strip()
            source_hint = f"search_terms={search_terms[:300]}\n" if search_terms else ""
            origin = record.get("origin") or "direct"
            anchor = record.get("related_to") if origin == "sibling" else None
            topic_hint = ""
            if anchor:
                topic_hint = (f"topic={titles.get(anchor) or record.get('related_title') or ''} "
                              f"(inferred from [{anchor}]; this item was not matched "
                              f"by the query itself)\n")
            elif origin == "bridged":
                # A bridged item was found by restating the query in the corpus's
                # own vocabulary. Saying so is the difference between a reader
                # trusting a result and wondering why it is there: the item is
                # relevant to the subject but was not matched by the words that
                # were actually asked with.
                topic_hint = ("matched_after_query_rewrite=true "
                              "(reached via terms the corpus associates with your "
                              "query; not a literal match)\n")
            elif origin == "anchored":
                topic_hint = ("matched_on_subject_term=true "
                              "(shares a rare term with your query but ranked "
                              "below the returned page; appended, not reordered)\n")
            readable_content = display_contract(experience) if experience else record['content']
            if record.get('superseded_by'):
                readable_content = ('[历史版本：已被替代，不可作为当前有效决定。'
                    f"有效期终止于 {record.get('valid_until') or '未知'}。]\n" + readable_content)
            elif record.get('claim_conflicted'):
                readable_content = '[冲突未解决：仅供历史核对，不得当作已确认的当前决定。]\n' + readable_content
            block = (f"[{record['id']}] {record['title']}\n{readable_content}\n"
                     f"{source_hint}{topic_hint}"
                     f"source_label={source_label(record['source_agent'])}\n"
                     f"source={record['source_agent']} project={record.get('project_key', project_key)} "
                     f"source_session={record.get('source_session') or 'unknown'} "
                     f"confidence={record.get('confidence', 0)} "
                     f"verified={record.get('verified_count', 0)} "
                     f"rejected={record.get('rejected_count', 0)}\n")
            blocks[record['id']] = block
            matches_scope = (applicable(experience, prompt) if experience
                             else not record['content'].startswith('{"experience":'))
            decision_rejection = rejection_reason(record, intent)
            row_full = record_limit is not None and emitted_count >= record_limit
            emitted = not decision_rejection and matches_scope and not row_full and len(context) + len(block) + len(footer) <= budget
            spec = constraint(record["content"])
            before = read_artifact(workspace, spec)[0] if spec and emitted else None
            items.append({"knowledge_id": record["id"], "source_agent": record["source_agent"],
                          "source_label": source_label(record["source_agent"]),
                          "source_attribution_observed": False,
                          "rank": rank, "score": record.get("retrieval_score"),
                          # Kept on the trace so recall accounting can separate a
                          # record the query reached from one inferred alongside
                          # it. Without this the inferred neighbour counts as a
                          # retrieval hit and every recall figure is inflated.
                          "origin": origin, "related_to": anchor,
                          "provenance": record.get("provenance", [{"origin": origin}]),
                          "omitted_reason": None if emitted else decision_rejection or ('not_applicable' if not matches_scope else 'row_budget' if row_full else 'context_budget'),
                          "decision_policy": POLICY_VERSION,
                          "historical": intent.historical,
                          "experience": experience,
                          "experience_result": {"status": "pending"} if experience and emitted else None,
                          "content_hash": digest(record["content"]),
                          "emitted": emitted, "citation_observed": False,
                          "check_status": "pending" if spec and emitted else "no_verifier",
                          "check_spec": spec if emitted else None, "before": before})
            if record['id'] in recoveries:
                items[-1]['lfhv_recovery_outcome'] = 'pending' if emitted else 'not_emitted'
            if emitted:
                context += block
                emitted_count += 1
        context = context + footer if any(item["emitted"] for item in items) else ""
        with self.knowledge._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if turn_id:
                previous = db.execute("""SELECT * FROM reuse_traces WHERE agent_id=?
                    AND project_key=? AND session_id=? AND turn_id=?""",
                    (agent_id, project_key, session_id, turn_id)).fetchone()
                if previous:
                    if previous["prompt_hash"] != digest(prompt.strip()):
                        raise ValueError("turn_id was already used for another prompt")
                    previous_items = json.loads(previous["items_json"])
                    live_context = self.live_context(dict(previous), db=db) if validate_live_records else previous['context_text']
                    return previous["id"], live_context, [
                        i["knowledge_id"] for i in previous_items
                        if i["emitted"] and not i.get('removed') and live_context]
            # A deletion can commit after search but before this write. Reject
            # the stale snapshot under the same writer lock as trace creation.
            ids = [i['knowledge_id'] for i in items]
            if ids and validate_live_records:
                live = {r['id']:r for r in db.execute("SELECT * FROM knowledge_records WHERE status IN ('active','stale','archived') AND (scope='user' OR project_key=?) AND id IN ("
                        + ','.join('?' for _ in ids) + ')', [project_key, *ids])}
                invalid = {item['knowledge_id'] for item in items if (item['emitted'] or (
                    recoveries and item['omitted_reason'] in {'context_budget', 'row_budget'})) and (
                    item['knowledge_id'] not in live
                    or rejection_reason(live[item['knowledge_id']], intent)
                    or digest(live[item['knowledge_id']]['content']) != item['content_hash']
                    or (live[item['knowledge_id']]['status']=='archived'
                        and item['knowledge_id'] not in recoveries and not (
                            intent.historical and live[item['knowledge_id']]['superseded_by'])))}
                invalid.update(key for key, record in recoveries.items()
                    if not Governor.recovery_eligible(record=record, live=live.get(key),
                        project_key=project_key, query=prompt.strip()[:500]))
                if invalid:
                    # A failed optional recovery must not suppress good normal
                    # results. Preserve the existing all-or-nothing validation
                    # for stale snapshots of normal retrieval.
                    invalidate_all = bool(invalid - recoveries.keys())
                    for item in items:
                        if not invalidate_all and item['knowledge_id'] not in invalid:
                            continue
                        item['emitted'] = False
                        item['omitted_reason'] = 'concurrent_removal' if item['knowledge_id'] not in live else 'concurrent_version_change'
                        if item['knowledge_id'] in invalid:
                            item.update(removed=True, experience=None, check_spec=None, before=None)
                            if item['knowledge_id'] in recoveries:
                                item['lfhv_recovery_outcome'] = 'evidence_changed'
            if validate_live_records:
                agent = db.execute('SELECT enabled FROM agent_registry WHERE agent_id=?', (agent_id,)).fetchone()
                if agent and not agent['enabled']:
                    for item in items:
                        item.update(emitted=False, omitted_reason='agent_disabled')
                if recoveries:
                    # A rejected archive must not consume the slots/characters
                    # needed by the valid ordinary records behind it.
                    used_chars, used_rows = len(header) + len(footer), 0
                    for item in items:
                        if item['omitted_reason'] not in {None, 'context_budget', 'row_budget'}:
                            continue
                        key = item['knowledge_id']
                        row_full = record_limit is not None and used_rows >= record_limit
                        fits = not row_full and used_chars + len(blocks[key]) <= budget
                        was_emitted = item['emitted']
                        item.update(emitted=fits, omitted_reason=None if fits else 'row_budget' if row_full else 'context_budget')
                        if fits:
                            used_rows += 1
                            used_chars += len(blocks[key])
                            if not was_emitted:
                                spec = constraint(records_by_id[key]['content'])
                                item.update(check_spec=spec, before=read_artifact(workspace, spec)[0] if spec else None,
                                            check_status='pending' if spec else 'no_verifier')
                # Character budgets can remove half of a supporting set. Check
                # the actual outgoing subset before any restoration is written.
                outgoing = {i['knowledge_id'] for i in items if i['emitted']}
                selected = tuple(Candidate(r, r.get('origin') or 'direct')
                                 for r in records if r['id'] in outgoing)
                vocabulary = tuple(sorted({s for r in records
                    for s in normalize_subject_terms(r.get('subject_terms'))}))
                selected, evidence_omitted = filter_retrieval_candidates(
                    selected, intent.focus[:500], subject_vocabulary=vocabulary)
                selected, answer_omitted = apply_answerability_gate(
                    selected, intent.focus[:500], subject_vocabulary=vocabulary)
                kept = {c.id for c in selected}
                reasons = {r['knowledge_id']: r['reason'] for r in evidence_omitted + answer_omitted}
                for item in items:
                    if item['emitted'] and item['knowledge_id'] not in kept:
                        item.update(emitted=False,
                            omitted_reason=reasons.get(item['knowledge_id'], 'answer_evidence_budget'),
                            experience=None, check_spec=None, before=None)
            governor = Governor(self.knowledge)
            for item in items:
                record = recoveries.get(item['knowledge_id'])
                if record is None:
                    continue
                if not item['emitted']:
                    if item.get('lfhv_recovery_outcome') != 'evidence_changed':
                        item['lfhv_recovery_outcome'] = 'not_emitted'
                    continue
                outcome = governor.restore_for_reuse(connection=db, record=record,
                    project_key=project_key, query=prompt.strip()[:500])
                item['lfhv_recovery_outcome'] = outcome or 'evidence_changed'
                if outcome is None:
                    # The complete outgoing set was checked under this lock.
                    # Unexpected refusal must roll back every restoration, not
                    # silently serve half of a mutually supporting evidence set.
                    raise RuntimeError('LFHV recovery changed during context commit')
            # Only selected, still-valid records are restored; budget omissions
            # remain archived. Build the exact context persisted with the trace.
            context = (header + ''.join(blocks[i['knowledge_id']] for i in items if i['emitted'])
                       + footer) if any(i['emitted'] for i in items) else ''
            db.execute("""INSERT INTO reuse_traces
                (id,agent_id,project_key,session_id,turn_id,prompt_hash,workspace,
                 context_hash,context_text,context_chars,retrieval_ms,items_json,created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trace_id, agent_id, project_key, session_id, turn_id or None,
                 digest(prompt.strip()), str(Path(workspace).resolve()) if workspace else "",
                 digest(context), context, len(context), retrieval_ms,
                 json.dumps(items, ensure_ascii=False), utc_now()))
            if transcript_boundary is not None:
                db.execute("UPDATE reuse_traces SET transcript_boundary=? WHERE id=?",
                           (json.dumps(transcript_boundary), trace_id))
            emitted_ids = list(dict.fromkeys(i['knowledge_id'] for i in items if i['emitted']))
            if record_hits and emitted_ids:
                db.execute('UPDATE knowledge_records SET hit_count=hit_count+1,last_hit_at=? '
                           'WHERE id IN (' + ','.join('?' for _ in emitted_ids) + ')',
                           [self.knowledge.clock(), *emitted_ids])
        return trace_id, context, [i["knowledge_id"] for i in items if i["emitted"]]

    def complete(self, *, agent_id, project_key, session_id, turn, turn_id=None):
        # Only agent-authored text/inputs count; echoed tool outputs do not.
        observed = turn.assistant_text + "\n" + "\n".join(t.input_summary for t in turn.tools)
        source_labels = observed_source_labels(turn.assistant_text)
        trace_ids = set(re.findall(r"\brt_[a-f0-9]{20}\b", observed))
        with self.knowledge._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("""SELECT * FROM reuse_traces WHERE agent_id=?
                AND project_key=? AND session_id=? AND completed_at IS NULL""",
                (agent_id, project_key, session_id)).fetchall()
            if turn_id:
                selected = [r for r in rows if r["turn_id"] == turn_id]
                association = "turn_id"
                if not selected and turn_id.startswith("transcript:"):
                    # Older Claude builds may flush the prompt after Submit.
                    # A changed transcript UUID plus exact prompt bridges that gap.
                    selected = [r for r in rows if not r['turn_id']
                                and r['prompt_hash'] == digest(turn.user_text.strip())
                                and self._boundary_matches(r, turn)]
                    association = "transcript_boundary"
            elif trace_ids:
                selected = [r for r in rows if r["id"] in trace_ids]
                association = "trace_citation"
            else:
                selected = [r for r in rows if r["prompt_hash"] == digest(turn.user_text.strip())]
                association = "unique_prompt_hash"
            # Ambiguous turns never receive credit. Prompt equality is weaker evidence.
            if len(selected) != 1:
                return None
            row = selected[0]
            if row['transcript_boundary'] and not self._boundary_matches(row, turn):
                return None
            if row['prompt_hash'] != digest(turn.user_text.strip()):
                return None
            items = json.loads(row["items_json"])
            for item in items:
                if not item["emitted"] or item.get('removed'):
                    continue
                item["source_attribution_observed"] = (
                    item.get("source_label", source_label(item["source_agent"])) in source_labels
                )
                item["citation_observed"] = bool(re.search(
                    r"(?<![A-Za-z0-9_])" + re.escape(item["knowledge_id"]) + r"(?![A-Za-z0-9_])", observed))
                if item.get('experience'):
                    item['experience_result'] = observe_contract(item['experience'], turn.tools, association)
                spec = item["check_spec"]
                if not spec:
                    continue
                after, value = read_artifact(row["workspace"], spec)
                item["after"] = after
                if association == "unique_prompt_hash":
                    item["check_status"] = "weak_turn_association"
                elif not item["citation_observed"]:
                    item["check_status"] = "not_cited"
                elif after["status"] != "read":
                    item["check_status"] = after["status"]
                elif (item["before"] or {}).get("status") not in {"read", "missing", "invalid_json"}:
                    item["check_status"] = "no_valid_baseline"
                elif after.get("sha256") == (item["before"] or {}).get("sha256"):
                    item["check_status"] = "unchanged_artifact"
                else:
                    matches = isinstance(value, dict) and all(
                        key in value and type(value[key]) is type(expected) and value[key] == expected
                        for key, expected in spec["equals"].items())
                    item["check_status"] = "constraint_pass" if matches else "constraint_fail"
            objectives = [t.success for t in turn.tools if t.objective_kind and t.success is not None]
            success = (False if False in objectives else True) if objectives else None
            timestamp = utc_now()
            db.execute("""UPDATE reuse_traces SET completed_at=?, association=?,
                task_success=?, items_json=? WHERE id=? AND completed_at IS NULL""",
                (timestamp, association, success, json.dumps(items, ensure_ascii=False), row["id"]))
            for item in items:
                if item['emitted']:
                    record_outcome(db, item, agent_id=agent_id, timestamp=timestamp, trace_id=row['id'])
        return row["id"]

    @staticmethod
    def _boundary_matches(row, turn):
        if not row['transcript_boundary'] or not turn.source_turn_key:
            return False
        boundary = json.loads(row['transcript_boundary'])
        return (turn.source_turn_key == boundary['key'] if boundary['fresh']
                else turn.source_turn_key != boundary['key'] and turn.previous_turn_key == boundary['key'])

    def list(self, project_key, knowledge_id=None, limit=100):
        with self.knowledge._connect() as db:
            # Filter in SQL before pagination, including historical snapshots of removed items.
            rows = db.execute("""SELECT * FROM reuse_traces t WHERE project_key=?
                AND (? IS NULL OR EXISTS (SELECT 1 FROM json_each(t.items_json) i
                  WHERE json_extract(i.value, '$.knowledge_id')=?))
                ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (project_key, knowledge_id, knowledge_id, limit)).fetchall()
        result = []
        for row in rows:
            trace = dict(row)
            trace["items"] = json.loads(trace.pop("items_json"))
            for item in trace["items"]:
                item.pop("check_spec", None)
                # Traces written before expansion existed carry no origin; report
                # the historical meaning rather than a missing key.
                item.setdefault("origin", "direct")
                item.setdefault("related_to", None)
                item.setdefault("source_label", source_label(item["source_agent"]))
                item.setdefault("source_attribution_observed", False)
            trace.pop("workspace", None)
            trace.pop("context_text", None)
            result.append(trace)
        return result

    def metrics(self, project_key):
        with self.knowledge._connect() as db:
            rows = db.execute("SELECT * FROM reuse_traces WHERE project_key=?", (project_key,)).fetchall()
        items = [(r, i) for r in rows for i in json.loads(r["items_json"])]
        # Older traces predate the origin field. A missing origin is treated as
        # direct, matching what it meant when it was written: before expansion
        # existed every item in a trace had been matched by the query.
        direct = [(r, i) for r, i in items if (i.get("origin") or "direct") == "direct"]
        inferred = [(r, i) for r, i in items if (i.get("origin") or "direct") == "sibling"]
        # Kept as its own bucket rather than folded into the inferred one. A
        # sibling is another record about a subject the query did reach; a
        # bridged record was reached by restating the query itself. Both are
        # real knowledge the agent received and neither proves retrieval found
        # it, but they are different failures -- a cluster that was not widened,
        # versus a vocabulary the query did not share -- and collapsing them
        # would hide which one is happening.
        bridged = [(r, i) for r, i in items if (i.get("origin") or "direct") == "bridged"]
        # The complement of ``bridged``: the record shares a rare term with the
        # query and lost the ranking, rather than lacking the query's vocabulary.
        # Reported separately for the same reason the other two are -- the two
        # failures have different remedies, and a single widened bucket would
        # hide which one the corpus is actually producing.
        anchored = [(r, i) for r, i in items if (i.get("origin") or "direct") == "anchored"]
        recovered = [(r, i) for r, i in items if i.get('origin') == 'lfhv_recovered']
        emitted = [(r, i) for r, i in items if i["emitted"]]
        source_attributed_turns = len({
            r["id"] for r, i in emitted if i.get("source_attribution_observed", False)
        })
        cross_agent = [(r, i) for r, i in emitted if r["agent_id"] != i["source_agent"]]
        cross_cited = {r["id"] for r, i in cross_agent
                       if r["completed_at"] and i.get("citation_observed", False)}
        cross_attributed = {r["id"] for r, i in cross_agent
                            if r["completed_at"] and i.get("source_attribution_observed", False)}
        checks = [(r, i) for r, i in emitted if i["check_status"] in {"constraint_pass", "constraint_fail"}]
        latencies = [r["retrieval_ms"] for r in rows]
        return {"traces": len(rows), "retrieved_items": len(items), "emitted_items": len(emitted),
                # Recall is reported over what the query reached. A record that
                # was inferred from a neighbour is knowledge the agent received,
                # but it is not evidence that retrieval found it, so it is
                # counted separately rather than folded into the numerator.
                "direct_items": len(direct), "inferred_items": len(inferred),
                "bridged_items": len(bridged), "anchored_items": len(anchored),
                "lfhv_candidate_items": len(recovered),
                "lfhv_emitted_items": sum(bool(i['emitted']) for _, i in recovered),
                "lfhv_restored_items": sum(i.get('lfhv_recovery_outcome') == 'restored' for _, i in recovered),
                "lfhv_not_emitted_items": sum(not i['emitted'] for _, i in recovered),
                "cited_items": sum(i["citation_observed"] for _, i in emitted),
                "source_attributed_turns": source_attributed_turns,
                # One request can emit several records or carry both kinds of
                # evidence. Count its trace once, without upgrading a source
                # badge to per-item citation or artifact-check credit.
                "cross_agent_emitted_turns": len({r["id"] for r, _ in cross_agent}),
                "cross_agent_cited_turns": len(cross_cited),
                "cross_agent_source_attributed_turns": len(cross_attributed),
                "cross_agent_evidence_turns": len(cross_cited | cross_attributed),
                "checked_items": len(checks),
                "cross_agent_checked_items": sum(r["agent_id"] != i["source_agent"] for r, i in checks),
                "constraint_pass_items": sum(i["check_status"] == "constraint_pass" for _, i in checks),
                "cross_agent_constraint_pass_items": sum(i["check_status"] == "constraint_pass"
                    and r["agent_id"] != i["source_agent"] for r, i in checks),
                "retrieval_p50_ms": percentile(latencies, .5),
                "retrieval_p95_ms": percentile(latencies, .95),
                "response_timing": timing_metrics(rows, percentile),
                "ttft_ms": None, "causal_improvement": None,
                "note": "Emission is not receipt; citation is not adoption; constraint pass is not causal proof."}

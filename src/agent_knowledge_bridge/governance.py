"""Lifecycle governance — the retirement half of the memory lifecycle, plus the
test that keeps retirement honest.

Before this module MemWeave only ever grew. Records entered as `candidate`, got
confirmed as `active`, and stayed there forever; nothing in the runtime could
express "this is no longer earning its place". The retrieval layer therefore paid
for every record it had ever learned, on every query, permanently.

Two things are needed to close that loop, and neither works without the other:

1. Demotion and retirement. `active -> stale -> archived`, driven by *hit
   evidence* (was the record still being served?) rather than by age alone. A
   stale record is still retrievable, just ranked below active ones; an archived
   record leaves ordinary retrieval but is never deleted, so retirement is
   always reversible.

2. A falsification test for retirement itself. Retirement is a prediction —
   "this will not be needed again" — and a prediction nobody checks is not a
   policy, it is a guess. `shadow_probe` asks the counterfactual on
   selected subsequent queries: *had this archived record stayed active, would it
   have been eligible for use?* Each yes is a potential Lost Future Hit Value
   (LFHV) signal, not proof of a false kill or task benefit. Runtime recall uses
   qualified archived evidence on demand and restores it in the same transaction
   as context emission. Offline probes remain observation-only.

The second point is what makes this more than a cache eviction policy. Any
value-driven eviction scheme (LRU, LFU, recency-weighted) can retire records;
only a scheme that measures its own false kills and undoes them can be compared
against the alternative of never retiring at all. That comparison — retirement
versus no retirement, on the same workload — is the experiment this module makes
possible.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable

from agent_knowledge_bridge.store import CANDIDATE_REVIEW_DAYS, KnowledgeStore
from agent_knowledge_bridge.decisions import POLICY_VERSION, obsolete, record_digest, query_intent
from agent_knowledge_bridge.evidence import normalize_subject_terms, evidence_support
from agent_knowledge_bridge.knowledge_versions import blocked


#: Evidence outcomes that mean the record was independently confirmed, as opposed
#: to merely proposed or echoed back. Re-publishing an identical record is not
#: confirmation: it says somebody restated the claim, not that it held.
CONFIRMING_OUTCOMES = ("verified",)

DEFAULT_POLICY: dict[str, Any] = {
    # Candidates must receive a decision before this deadline. Expiry isolates
    # uncertainty without deleting the proposal or its provenance.
    "candidate_review_days": CANDIDATE_REVIEW_DAYS,
    # An active record with no confirming activity for this long stops being
    # assumed live. Demotion is cheap and reversible, so this is deliberately
    # the shortest clock in the policy.
    "stale_after_days": 30,
    # A `stale` record that stays dormant this long is retired from retrieval.
    # Measured on the same absolute clock as `stale_after_days`, not from the
    # moment of demotion, so the two stages do not compound into a long wait.
    "archive_after_days": 90,
    # Capacity pressure. When a project holds more active records than this, the
    # least valuable actives are demoted even if they are still recent: retrieval
    # cost is paid per candidate, so an unbounded active set is its own defect.
    "max_active_per_project": 200,
    # Floor that capacity pressure may never breach. A project is never left with
    # zero active records, however weak its evidence looks.
    "min_active_per_project": 5,
    # Distinct normalized queries in the bounded shadow window needed before a
    # record becomes eligible for explicit restoration. This is demand evidence,
    # never a claim that restoration improves task success.
    "lfhv_resurrect_threshold": 1,
}


def _parse(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Governor:
    """Drives record demotion, retirement, and the LFHV check on retirement.

    The Governor holds no state of its own. Every decision is recomputed from the
    store, so a sweep is idempotent: running it twice in a row changes nothing
    the second time, which is what makes it safe to put on a timer.
    """

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        actor: str = "memweave-governor",
        policy: dict[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.actor = actor
        self.policy = {**DEFAULT_POLICY, **(policy or {})}
        # Time comes from the store, never from a second clock. Retirement
        # compares stored timestamps against "now", so two clocks would let the
        # governor disagree with the records it is judging.
        self.clock: Callable[[], str] = store.clock

    # ------------------------------------------------------------------
    # Retirement decisions
    # ------------------------------------------------------------------
    def _active_window(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Return the decided transitions without applying them."""
        now = _parse(self.clock()) or datetime.now(timezone.utc)
        stale_after = self.policy["stale_after_days"]
        archive_after = self.policy["archive_after_days"]
        demote: list[tuple[dict[str, Any], str]] = []
        retire: list[tuple[dict[str, Any], str]] = []

        for row in rows:
            last_activity = self._last_activity(row)
            idle_days = (now - last_activity).total_seconds() / 86400 if last_activity else None
            if idle_days is None:
                continue
            if row["status"] == "active" and idle_days >= stale_after:
                demote.append(
                    (
                        row,
                        f"no confirming activity for {idle_days:.1f}d "
                        f"(>= {stale_after}d); demoted, still retrievable",
                    )
                )
                if idle_days >= archive_after:
                    retire.append(
                        (
                            {**row, "status": "stale"},
                            f"stale and dormant for {idle_days:.1f}d "
                            f"(>= {archive_after}d); retired from retrieval, reversible",
                        )
                    )
            elif row["status"] == "stale" and idle_days >= archive_after:
                retire.append(
                    (
                        row,
                        f"stale and dormant for {idle_days:.1f}d "
                        f"(>= {archive_after}d); retired from retrieval, reversible",
                    )
                )
        return {"demote": demote, "retire": retire, "now": now}

    def _expired_candidates(
        self, rows: list[dict[str, Any]], now: datetime
    ) -> list[tuple[dict[str, Any], str]]:
        """Return candidates whose review deadline has passed.

        Older databases may not have a deadline populated. Those rows stay in
        the review queue so an upgrade cannot silently quarantine legacy data.
        """
        expired: list[tuple[dict[str, Any], str]] = []
        for row in rows:
            if row.get("status") != "candidate":
                continue
            expires_at = _parse(row.get("candidate_expires_at"))
            if expires_at is not None and expires_at <= now:
                expired.append(
                    (
                        row,
                        f"candidate review deadline expired at {expires_at.isoformat()} "
                        "without approval or qualifying evidence",
                    )
                )
        return expired

    def _last_activity(self, row: dict[str, Any]) -> datetime | None:
        """Latest moment this record demonstrably did something useful.

        Three sources count: it was served to a caller (`last_hit_at`), it was
        independently confirmed (`last_confirmed_at`), or it was first written.
        Re-publication is deliberately excluded — restating a claim is not
        evidence that the claim is useful.
        """
        candidates = [
            _parse(row.get("last_hit_at")),
            _parse(row.get("last_confirmed_at")),
            _parse(row.get("created_at")),
            _parse(row.get("last_restored_at")),
        ]
        present = [value for value in candidates if value is not None]
        return max(present) if present else None

    def _capacity_pressure(self, rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
        """Demote the weakest actives when a project holds too many of them."""
        active = [row for row in rows if row["status"] == "active"]
        cap = self.policy["max_active_per_project"]
        floor = self.policy["min_active_per_project"]
        allowed = max(0, len(active) - cap)
        allowed = min(allowed, max(0, len(active) - floor))
        if allowed <= 0:
            return []
        ordered = sorted(active, key=self._eviction_key)
        return [
            (
                row,
                f"capacity pressure: {len(active)} active exceeds cap {cap}; "
                "weakest-evidence actives demoted first",
            )
            for row in ordered[:allowed]
        ]

    @staticmethod
    def _eviction_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
        """Ascending order in which records are given up.

        Verification protects before popularity promotes: an independently
        confirmed record is kept over an unconfirmed one even if the unconfirmed
        one has been served more often, because being served is a statement
        about retrieval, not about truth. Oldest-first breaks the remaining ties.
        """
        return (
            int(row.get("verified_count") or 0),
            int(row.get("hit_count") or 0),
            int(row.get("adopted_count") or 0),
            str(row.get("updated_at") or ""),
        )

    def _load(self, project_key: str | None) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            rows = db.execute(
                """
                SELECT r.*,
                       COALESCE((
                           SELECT MAX(e.created_at) FROM knowledge_evidence e
                           WHERE e.knowledge_id = r.id AND e.outcome = ?
                       ), '') AS last_confirmed_at
                FROM knowledge_records r
                WHERE r.status IN ('candidate', 'active', 'stale')
                  AND (? IS NULL OR r.project_key = ?)
                """,
                (*CONFIRMING_OUTCOMES, project_key, project_key),
            ).fetchall()
        return [dict(row) for row in rows]

    def sweep(
        self, *, project_key: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Demote and retire records that stopped earning their retrieval slot.

        Returns the planned transitions. With `dry_run=True` nothing is written,
        which is how the policy is inspected before it is trusted.
        """
        rows = self._load(project_key)
        window = self._active_window(rows)
        demote = window["demote"]
        retire = window["retire"]
        expired_candidates = self._expired_candidates(rows, window["now"])
        already = {row["id"] for row, _ in demote}
        demote = demote + [
            item for item in self._capacity_pressure(rows) if item[0]["id"] not in already
        ]

        applied: list[dict[str, Any]] = []
        for row, reason in demote:
            applied.append(self._apply(row, "stale", reason, dry_run))
        for row, reason in retire:
            applied.append(self._apply(row, "archived", reason, dry_run))
        quarantined = [
            self._apply(row, "quarantined", reason, dry_run)
            for row, reason in expired_candidates
        ]
        applied.extend(quarantined)

        return {
            "project_key": project_key,
            "dry_run": dry_run,
            "scanned": len(rows),
            "demoted": sum(
                item["to"] == "stale" and item["changed"] for item in applied
            ),
            "archived": sum(
                item["to"] == "archived" and item["changed"] for item in applied
            ),
            "expired_candidates": sum(
                item["to"] == "quarantined" and item["changed"]
                for item in quarantined
            ),
            "quarantined": sum(
                item["to"] == "quarantined" and item["changed"]
                for item in applied
            ),
            "transitions": applied,
            "policy": self.policy,
        }

    def expire_candidates(
        self, *, project_key: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Isolate candidates whose review deadline elapsed.

        Runtime maintenance can call this without also demoting or archiving
        knowledge that has already passed the evidence gate.
        """
        rows = self._load(project_key)
        now = _parse(self.clock()) or datetime.now(timezone.utc)
        expired = self._expired_candidates(rows, now)
        transitions = [
            self._apply(row, "quarantined", reason, dry_run)
            for row, reason in expired
        ]
        changed = sum(item["changed"] for item in transitions)
        return {
            "project_key": project_key,
            "dry_run": dry_run,
            "scanned": len(rows),
            "expired_candidates": changed,
            "quarantined": changed,
            "transitions": transitions,
            "policy": {"candidate_review_days": self.policy["candidate_review_days"]},
        }

    def _apply(
        self, row: dict[str, Any], to_status: str, reason: str, dry_run: bool
    ) -> dict[str, Any]:
        if dry_run:
            changed = True
        else:
            changed = self.store.transit(
                row["id"],
                to_status=to_status,
                reason=reason,
                actor=self.actor,
                expected_status=row["status"],
            )["changed"]
        return {
            "knowledge_id": row["id"],
            "project_key": row["project_key"],
            "from": row["status"],
            "to": to_status,
            "reason": reason,
            "hit_count": int(row.get("hit_count") or 0),
            "verified_count": int(row.get("verified_count") or 0),
            "changed": changed,
        }

    # ------------------------------------------------------------------
    # The falsification test: what did retirement cost?
    # ------------------------------------------------------------------
    def shadow_probe(
        self, *, project_key: str, query: str, limit: int = 8
    ) -> dict[str, Any]:
        """Ask the counterfactual for one query and record any false kills.

        The probe runs the normal retrieval path with retired records restored to
        the ranking, then keeps only the retired ones. Anything it finds is a
        record retirement took out of circulation while the workload still asked
        for it. Nothing is injected into any agent's context here — a probe
        observes, it does not serve.

        Per-record counters and one per-project aggregate are updated in place,
        so storage stays bounded by projects plus retired records rather than by
        the number of queries ever run against the library.
        """
        retired = self._shadow_candidates(project_key=project_key, query=query, limit=limit)
        self._record_shadow(project_key=project_key, query=query, retired=retired)
        return {
            "query": query,
            "would_have_served": [item["id"] for _, item in retired],
            "count": len(retired),
        }

    def _shadow_candidates(self, *, project_key, query, limit, requester_agent=None):
        result = self.store.search(
            requester_agent=requester_agent or self.actor,
            project_key=project_key,
            query=query,
            limit=limit,
            include_retired=True,
            # A probe measures what the unexpanded ranking served, so sibling
            # expansion must stay off here. Widening the result set would mix
            # records the query never reached into a measurement of what it
            # reached, and the counterfactual would no longer be one.
            expand_siblings=False,
        )
        return [
            (rank, item)
            for rank, item in enumerate(result["results"], 1)
            if item["status"] == "archived" and not obsolete(item) and not blocked(item)
        ]

    def _record_shadow(self, *, project_key, query, retired):
        timestamp = self.clock()
        query_hash = hashlib.sha256(' '.join(query.casefold().split()).encode()).hexdigest()
        with self.store._connect() as db:
            db.execute(
                """
                INSERT INTO shadow_probe_stats
                    (project_key, probe_count, missed_query_count,
                     shadow_hit_count, resurrection_count,
                     first_probe_at, last_probe_at)
                VALUES (?, 1, ?, ?, 0, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                    probe_count = probe_count + 1,
                    missed_query_count = missed_query_count + excluded.missed_query_count,
                    shadow_hit_count = shadow_hit_count + excluded.shadow_hit_count,
                    last_probe_at = excluded.last_probe_at
                """,
                (project_key, int(bool(retired)), len(retired), timestamp, timestamp),
            )
            for rank, item in retired:
                previous = db.execute('SELECT * FROM shadow_hits WHERE knowledge_id=? AND project_key=?',
                    (item['id'], project_key)).fetchone()
                content_hash = record_digest(item)
                hashes = (json.loads(previous['query_hashes']) if previous and
                          previous['decision_version'] == POLICY_VERSION and
                          previous['content_hash'] == content_hash else [])
                distinct = query_hash not in hashes
                # Fixed-size window; raw queries are never persisted. Counts
                # mean distinct normalized queries in this window, not uses.
                hashes = (hashes + [query_hash])[-32:] if distinct else hashes
                db.execute(
                    """
                    INSERT INTO shadow_hits
                        (knowledge_id, project_key, hit_count, best_rank,
                         first_seen_at, last_seen_at, query_hashes, decision_version, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(knowledge_id, project_key) DO UPDATE SET
                        hit_count = excluded.hit_count,
                        best_rank = MIN(best_rank, excluded.best_rank),
                        last_seen_at = excluded.last_seen_at,
                        query_hashes = excluded.query_hashes,
                        decision_version = excluded.decision_version,
                        content_hash = excluded.content_hash
                    """,
                    (item["id"], project_key, len(hashes), rank, timestamp, timestamp,
                     json.dumps(hashes), POLICY_VERSION, content_hash),
                )

    @staticmethod
    def recovery_signature(row):
        """Bind an opportunity to content, scope and retrieval metadata."""
        fields = ('title', 'content', 'scope', 'project_key', 'knowledge_type',
                  'source_agent', 'source_session', 'search_terms')
        snapshot = {key: row[key] for key in fields}
        snapshot['subject_terms'] = normalize_subject_terms(row['subject_terms'])
        return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
                            sort_keys=True).encode('utf-8')).hexdigest()

    def prepare_recovery(self, *, project_key, query, requester_agent, limit=8):
        """Find current-query opportunities; leave status unchanged until emission.

        The ordinary search gates apply, with expansion disabled. Historic
        lookups never re-enable old policies. This path does not wait for the
        offline distinct-query threshold: the current eligible use is demand.
        """
        intent = query_intent(query, project_key=project_key)
        if intent.memory_disabled or intent.historical:
            return []
        retired = self._shadow_candidates(project_key=project_key, query=query,
                                         limit=limit, requester_agent=requester_agent)
        self._record_shadow(project_key=project_key, query=query, retired=retired)
        results = []
        for _, row in retired:
            # Archiving an unapproved proposal must not bypass admission.
            if (int(row.get('verified_count') or 0) <= 0
                    or not evidence_support(intent.focus, row)[0]):
                continue
            results.append({**row, 'origin': 'lfhv_recovered',
                'provenance': [*row.get('provenance', []), {'origin': 'lfhv_recovered'}],
                'lfhv_recovery': {
                    'policy': POLICY_VERSION,
                    'query_hash': hashlib.sha256(query.encode('utf-8')).hexdigest(),
                    'signature': self.recovery_signature(row),
                    'content_digest': record_digest(row),
                }})
        return results

    @classmethod
    def recovery_eligible(cls, *, record, live, project_key, query):
        """Recheck a prepared opportunity against the locked live snapshot."""
        evidence = record.get('lfhv_recovery') or {}
        return bool(
            evidence.get('policy') == POLICY_VERSION
            and evidence.get('query_hash') == hashlib.sha256(query.encode('utf-8')).hexdigest()
            and live is not None and live['status'] in {'archived', 'active', 'stale'}
            and (live['scope'] == 'user' or live['project_key'] == project_key)
            and live['verified_count'] > 0 and not blocked(live) and not obsolete(live)
            and cls.recovery_signature(live) == evidence.get('signature')
            and record_digest(live) == evidence.get('content_digest')
        )

    def restore_for_reuse(self, *, connection, record, project_key, query):
        """Restore one selected item under ReuseStore's writer lock.

        Returns None when evidence changed. A concurrent identical restoration
        can still be served but never increments restoration statistics twice.
        The caller commits this together with the trace, or rolls both back.
        """
        evidence = record.get('lfhv_recovery') or {}
        live = connection.execute('SELECT * FROM knowledge_records WHERE id=?',
                                  (record['id'],)).fetchone()
        if not self.recovery_eligible(record=record, live=live,
                                      project_key=project_key, query=query):
            return None
        if live['status'] != 'archived':
            return 'already_retrievable'
        change = self.store.transit(record['id'], to_status='active',
            actor=self.actor, expected_status='archived',
            restoration_digest=evidence['content_digest'], _connection=connection,
            reason='LFHV on-demand reuse: current query passed retrieval gates; '
                   'restored with context emission, task benefit unverified')
        if not change['changed']:
            return None
        connection.execute('DELETE FROM shadow_hits WHERE knowledge_id=?', (record['id'],))
        timestamp = self.clock()
        connection.execute('''INSERT INTO shadow_probe_stats
            (project_key,probe_count,missed_query_count,shadow_hit_count,resurrection_count,
             first_probe_at,last_probe_at) VALUES (?,0,0,0,1,?,?)
            ON CONFLICT(project_key) DO UPDATE SET
                resurrection_count=resurrection_count+1''',
            (project_key, timestamp, timestamp))
        return 'restored'

    def lfhv_report(self, *, project_key: str | None = None) -> dict[str, Any]:
        """Report query-level misses and current record-level false kills.

        Query-level counters survive resurrection, while the per-record ledger
        describes only records that are currently archived. This keeps historical
        experiments reproducible without letting bookkeeping grow per query.
        """
        with self.store._connect() as db:
            retired = db.execute(
                "SELECT * FROM knowledge_records WHERE status = 'archived'"
                + (
                    " AND (scope = 'user' OR project_key = ?)"
                    if project_key
                    else ""
                ),
                (project_key,) if project_key else (),
            ).fetchall()
            shadow = db.execute(
                "SELECT knowledge_id, project_key, hit_count, best_rank, "
                "first_seen_at, last_seen_at, decision_version, content_hash"
                " FROM shadow_hits"
                + (" WHERE project_key = ?" if project_key else ""),
                (project_key,) if project_key else (),
            ).fetchall()
            stats = (
                db.execute(
                    "SELECT * FROM shadow_probe_stats WHERE project_key = ?",
                    (project_key,),
                ).fetchone()
                if project_key
                else db.execute(
                    """
                    SELECT SUM(probe_count) AS probe_count,
                           SUM(missed_query_count) AS missed_query_count,
                           SUM(shadow_hit_count) AS shadow_hit_count,
                           SUM(resurrection_count) AS resurrection_count
                    FROM shadow_probe_stats
                    """
                ).fetchone()
            )
        retired_ids = {row["id"] for row in retired}
        valid_retired = {row['id']: record_digest(row)
                         for row in retired if not obsolete(row) and not blocked(row)}
        threshold = self.policy["lfhv_resurrect_threshold"]
        false_kills = [
            {
                "knowledge_id": row["knowledge_id"],
                "project_key": row["project_key"],
                "shadow_hits": int(row["hit_count"]),
                "best_rank": int(row["best_rank"]),
                "first_seen": row["first_seen_at"],
                "last_seen": row["last_seen_at"],
                "content_hash": row['content_hash'],
            }
            for row in shadow
            if row["knowledge_id"] in valid_retired and int(row["hit_count"]) >= threshold
            and row['decision_version'] == POLICY_VERSION
            and row['content_hash'] == valid_retired[row['knowledge_id']]
        ]
        probe_count = int((stats["probe_count"] if stats else 0) or 0)
        missed_queries = int((stats["missed_query_count"] if stats else 0) or 0)
        historical_shadow_hits = int((stats["shadow_hit_count"] if stats else 0) or 0)
        resurrection_count = int((stats["resurrection_count"] if stats else 0) or 0)
        query_miss_rate = (
            round(missed_queries / probe_count, 3) if probe_count else None
        )
        record_false_kill_rate = (
            round(len(false_kills) / len(retired), 3) if retired else None
        )
        return {
            "project_key": project_key,
            "retired_total": len(retired),
            "retired_ever_asked_for": len(false_kills),
            "false_kills": sorted(false_kills, key=lambda item: -item["shadow_hits"]),
            "shadow_probes_recorded": probe_count,
            "queries_with_false_kill": missed_queries,
            "shadow_hits_recorded": historical_shadow_hits,
            "resurrections_recorded": resurrection_count,
            "policy": {"lfhv_resurrect_threshold": threshold},
            "query_miss_rate": query_miss_rate,
            "retired_record_false_kill_rate": record_false_kill_rate,
            # Backward-compatible name. It now carries the query-level rate,
            # which is the quantity callers normally mean by a miss rate.
            "miss_rate": query_miss_rate,
            "note": (
                "query_miss_rate counts probes whose top-k would have contained at "
                "least one archived record. retired_record_false_kill_rate is the "
                "current archived-record incidence. Neither proves lost task success."
            ),
        }

    def resurrect(self, *, project_key: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Restore eligible archived records, without claiming task benefit.

        Content digests and obsolescence are rechecked under the transition's
        writer lock. A separate restoration clock grants a bounded grace period;
        hit_count continues to mean actual context emission only. Shadow rows
        are removed on restoration and lifecycle audit stays bounded.
        """
        report = self.lfhv_report(project_key=project_key)
        restored: list[dict[str, Any]] = []
        for item in report["false_kills"]:
            reason = (
                f"LFHV restoration: eligible in {item['shadow_hits']} distinct shadow queries, "
                f"best rank {item['best_rank']}; task benefit not yet verified"
            )
            changed = True
            if not dry_run:
                changed = self.store.transit(
                    item["knowledge_id"],
                    to_status="active",
                    reason=reason,
                    actor=self.actor,
                    expected_status="archived",
                    restoration_digest=item['content_hash'],
                )["changed"]
                if not changed:
                    continue
                with self.store._connect() as db:
                    # A bounded grace clock prevents immediate re-retirement,
                    # without pretending that restoration served a real query.
                    db.execute(
                        "DELETE FROM shadow_hits WHERE knowledge_id = ?",
                        (item["knowledge_id"],),
                    )
                    db.execute(
                        "UPDATE shadow_probe_stats "
                        "SET resurrection_count = resurrection_count + 1 "
                        "WHERE project_key = ?",
                        (item["project_key"],),
                    )
            restored.append(
                {**item, "reason": reason, "dry_run": dry_run, "changed": changed}
            )
        return {
            "project_key": project_key,
            "dry_run": dry_run,
            "considered": len(report["false_kills"]),
            "restored": len(restored),
            "records": restored,
        }

    def prune_shadow(self, *, project_key: str | None = None) -> dict[str, Any]:
        """Drop shadow rows that no longer describe a retired record.

        Defensive, not part of normal operation: probe/recovery preparation
        admits only `archived` records, so a row can go stale only
        if a record left `archived` by hand. Run it after external edits to keep
        the ledger's bound honest.
        """
        with self.store._connect() as db:
            candidates = db.execute(
                "SELECT knowledge_id FROM shadow_hits"
                + (" WHERE project_key = ?" if project_key else ""),
                (project_key,) if project_key else (),
            ).fetchall()
            stale_ids = [
                row["knowledge_id"]
                for row in candidates
                if (
                    (record := db.execute(
                    "SELECT status FROM knowledge_records WHERE id = ?",
                    (row["knowledge_id"],),
                    ).fetchone()) is None
                    or record["status"] != "archived"
                )
            ]
            for knowledge_id in stale_ids:
                db.execute(
                    "DELETE FROM shadow_hits WHERE knowledge_id = ?"
                    + (" AND project_key = ?" if project_key else ""),
                    (knowledge_id, project_key) if project_key else (knowledge_id,),
                )
        return {"project_key": project_key, "removed": len(stale_ids)}

    def report(self, *, project_key: str | None = None) -> dict[str, Any]:
        """One call that answers: is retirement still earning its keep?"""
        sweep = self.sweep(project_key=project_key, dry_run=True)
        lfhv = self.lfhv_report(project_key=project_key)
        with self.store._connect() as db:
            statuses = db.execute(
                "SELECT status, COUNT(*) AS count FROM knowledge_records"
                + (" WHERE project_key = ?" if project_key else "")
                + " GROUP BY status",
                (project_key,) if project_key else (),
            ).fetchall()
        return {
            "project_key": project_key,
            "status_counts": {row["status"]: row["count"] for row in statuses},
            "pending_demotions": sweep["demoted"],
            "pending_retirements": sweep["archived"],
            "pending_candidate_expirations": sweep["expired_candidates"],
            "lfhv": lfhv,
        }

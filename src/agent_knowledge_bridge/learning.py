from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_knowledge_bridge.store import KnowledgeStore, utc_now


class LearningStore:
    """Event, review, and recall telemetry layered on the shared knowledge store."""

    def __init__(self, database_path: Path) -> None:
        self.knowledge = KnowledgeStore(database_path)
        self.database_path = self.knowledge.database_path
        self._initialize()

    def _initialize(self) -> None:
        with self.knowledge._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_events (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    success INTEGER,
                    objective_kind TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(agent_id, session_id, event_type, payload_hash)
                );

                CREATE TABLE IF NOT EXISTS learning_runs (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    input_chars INTEGER NOT NULL DEFAULT 0,
                    proposal_count INTEGER NOT NULL DEFAULT 0,
                    promoted_count INTEGER NOT NULL DEFAULT 0,
                    latency_ms REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS knowledge_event_links (
                    knowledge_id TEXT NOT NULL REFERENCES knowledge_records(id),
                    event_id TEXT NOT NULL REFERENCES agent_events(id),
                    relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(knowledge_id, event_id, relation)
                );

                CREATE TABLE IF NOT EXISTS recall_events (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    knowledge_ids_json TEXT NOT NULL,
                    injected_chars INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    successful_validation INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_agent_events_session
                    ON agent_events(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_learning_runs_project
                    ON learning_runs(project_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_recall_events_project
                    ON recall_events(project_key, created_at);

                CREATE TABLE IF NOT EXISTS knowledge_compilation_links (
                    run_id TEXT NOT NULL REFERENCES learning_runs(id),
                    knowledge_id TEXT NOT NULL REFERENCES knowledge_records(id),
                    relation TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, knowledge_id, relation)
                );
                CREATE INDEX IF NOT EXISTS idx_compilation_knowledge
                    ON knowledge_compilation_links(knowledge_id, created_at);

                CREATE TRIGGER IF NOT EXISTS agent_events_immutable_update
                BEFORE UPDATE ON agent_events
                BEGIN
                    SELECT RAISE(ABORT, 'agent_events are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS agent_events_immutable_delete
                BEFORE DELETE ON agent_events
                BEGIN
                    SELECT RAISE(ABORT, 'agent_events are immutable');
                END;
                """
            )
            self.knowledge._ensure_column(
                connection, "learning_runs", "source_hash", "TEXT NOT NULL DEFAULT ''"
            )
            self.knowledge._ensure_column(
                connection, "learning_runs", "compiler_version",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            self.knowledge._ensure_column(
                connection, "learning_runs", "schema_version",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            self.knowledge._ensure_column(connection, 'learning_runs', 'attempts', 'INTEGER NOT NULL DEFAULT 1')

    def begin_run(
        self,
        *,
        agent_id: str,
        project_key: str,
        session_id: str,
        turn_hash: str,
        input_chars: int,
        source_hash: str = "",
        compiler_version: str = "legacy",
        schema_version: str = "legacy",
    ) -> str | None:
        run_id = f"lr_{uuid.uuid4().hex[:16]}"
        try:
            with self.knowledge._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO learning_runs (
                        id, agent_id, project_key, session_id, turn_hash,
                        source_hash, compiler_version, schema_version,
                        status, input_chars, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        run_id,
                        agent_id,
                        project_key,
                        session_id,
                        turn_hash,
                        source_hash,
                        compiler_version,
                        schema_version,
                        input_chars,
                        utc_now(),
                    ),
                )
        except sqlite3.IntegrityError:
            with self.knowledge._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                prior = connection.execute('SELECT * FROM learning_runs WHERE turn_hash=?', (turn_hash,)).fetchone()
                expired = bool(prior and prior['status'] == 'running' and
                    (datetime.now(timezone.utc) - datetime.fromisoformat(prior['created_at'].replace('Z', '+00:00'))).total_seconds() > 300)
                if prior is None or (prior['status'] != 'failed' and not expired) or prior['attempts'] >= 3:
                    return None
                # Same run identity preserves compilation links and deduplication.
                connection.execute("UPDATE learning_runs SET status='running', attempts=attempts+1, error='', completed_at=NULL,created_at=? WHERE id=?", (utc_now(),prior['id']))
                return prior['id']
        return run_id

    def link_compilation(
        self, run_id: str, knowledge_id: str, relation: str
    ) -> None:
        if relation not in {"produced", "deduplicated"}:
            raise ValueError("invalid compilation relation")
        with self.knowledge._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_compilation_links (
                    run_id, knowledge_id, relation, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, knowledge_id, relation, utc_now()),
            )

    def compilation_provenance(self, knowledge_id: str) -> dict[str, Any]:
        with self.knowledge._connect() as connection:
            compilations = connection.execute(
                """
                SELECT lr.id AS run_id, lr.agent_id, lr.project_key, lr.session_id,
                       lr.source_hash, lr.compiler_version, lr.schema_version,
                       lr.status, links.relation, lr.created_at, lr.completed_at
                FROM knowledge_compilation_links links
                JOIN learning_runs lr ON lr.id = links.run_id
                WHERE links.knowledge_id = ?
                ORDER BY lr.created_at ASC
                """,
                (knowledge_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT events.id, events.agent_id, events.session_id,
                       events.event_type, events.payload_hash, events.success,
                       events.objective_kind, links.relation, events.created_at
                FROM knowledge_event_links links
                JOIN agent_events events ON events.id = links.event_id
                WHERE links.knowledge_id = ?
                ORDER BY events.created_at ASC
                """,
                (knowledge_id,),
            ).fetchall()
        return {
            "compilations": [dict(row) for row in compilations],
            "source_events": [
                {
                    **dict(row),
                    "success": None if row["success"] is None else bool(row["success"]),
                }
                for row in events
            ],
        }

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        proposal_count: int = 0,
        promoted_count: int = 0,
        latency_ms: float = 0,
        error: str = "",
    ) -> None:
        with self.knowledge._connect() as connection:
            connection.execute(
                """
                UPDATE learning_runs
                SET status = ?, proposal_count = ?, promoted_count = ?,
                    latency_ms = ?, error = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    proposal_count,
                    promoted_count,
                    latency_ms,
                    error[:1000],
                    utc_now(),
                    run_id,
                ),
            )

    def record_event(
        self,
        *,
        agent_id: str,
        project_key: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        success: bool | None = None,
        objective_kind: str | None = None,
    ) -> str:
        from .claude_transcript import redact_value
        canonical = json.dumps(redact_value(payload), ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.knowledge._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM agent_events
                WHERE agent_id = ? AND session_id = ?
                  AND event_type = ? AND payload_hash = ?
                """,
                (agent_id, session_id, event_type, payload_hash),
            ).fetchone()
            if existing is not None:
                return existing["id"]
            event_id = f"ae_{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO agent_events (
                    id, agent_id, project_key, session_id, event_type,
                    payload_json, payload_hash, success, objective_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    agent_id,
                    project_key,
                    session_id,
                    event_type,
                    canonical,
                    payload_hash,
                    None if success is None else int(success),
                    objective_kind,
                    utc_now(),
                ),
            )
        return event_id

    def get_events(self, event_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not event_ids:
            return {}
        placeholders = ", ".join("?" for _ in event_ids)
        with self.knowledge._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_events WHERE id IN ({placeholders})",
                event_ids,
            ).fetchall()
        return {
            row["id"]: {
                **dict(row),
                "payload": json.loads(row["payload_json"]),
                "success": None if row["success"] is None else bool(row["success"]),
            }
            for row in rows
        }

    def link_knowledge(self, knowledge_id: str, event_id: str, relation: str) -> None:
        with self.knowledge._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_event_links (
                    knowledge_id, event_id, relation, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (knowledge_id, event_id, relation, utc_now()),
            )

    def record_recall(
        self,
        *,
        agent_id: str,
        project_key: str,
        session_id: str,
        query: str,
        knowledge_ids: list[str],
        injected_chars: int,
        latency_ms: float,
    ) -> str:
        recall_id = f"rc_{uuid.uuid4().hex[:16]}"
        with self.knowledge._connect() as connection:
            connection.execute(
                """
                INSERT INTO recall_events (
                    id, agent_id, project_key, session_id, query,
                    knowledge_ids_json, injected_chars, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recall_id,
                    agent_id,
                    project_key,
                    session_id,
                    query[:2000],
                    json.dumps(knowledge_ids),
                    injected_chars,
                    latency_ms,
                    utc_now(),
                ),
            )
        return recall_id

    def complete_latest_recall(
        self, session_id: str, *, agent_id: str, project_key: str, successful_validation: bool | None
    ) -> list[str]:
        with self.knowledge._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM recall_events
                WHERE session_id = ? AND agent_id = ? AND project_key = ? AND completed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id, agent_id, project_key),
            ).fetchone()
            if row is None:
                return []
            connection.execute(
                """
                UPDATE recall_events
                SET successful_validation = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    None
                    if successful_validation is None
                    else int(successful_validation),
                    utc_now(),
                    row["id"],
                ),
            )
        return list(json.loads(row["knowledge_ids_json"]))

    def metrics(self, project_key: str) -> dict[str, Any]:
        with self.knowledge._connect() as connection:
            knowledge = connection.execute(
                """
                SELECT status, COUNT(*) AS count,
                       SUM(verified_count) AS verified,
                       SUM(rejected_count) AS rejected
                FROM knowledge_records
                WHERE project_key = ? OR scope = 'user'
                GROUP BY status
                """,
                (project_key,),
            ).fetchall()
            run = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(proposal_count) AS proposals,
                       SUM(promoted_count) AS promoted,
                       AVG(CASE WHEN status = 'completed' THEN latency_ms END) AS avg_latency
                FROM learning_runs WHERE project_key = ?
                """,
                (project_key,),
            ).fetchone()
            recall = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN knowledge_ids_json <> '[]' THEN 1 ELSE 0 END) AS hits,
                       SUM(CASE WHEN successful_validation = 1
                                AND knowledge_ids_json <> '[]' THEN 1 ELSE 0 END) AS successful,
                       SUM(CASE WHEN successful_validation IS NOT NULL
                                AND knowledge_ids_json <> '[]' THEN 1 ELSE 0 END) AS evaluated,
                       AVG(latency_ms) AS avg_latency,
                       AVG(injected_chars) AS avg_injected_chars
                FROM recall_events WHERE project_key = ?
                """,
                (project_key,),
            ).fetchone()

        statuses = {row["status"]: row["count"] for row in knowledge}
        recall_total = int(recall["total"] or 0)
        recall_hits = int(recall["hits"] or 0)
        evaluated = int(recall["evaluated"] or 0)
        successful = int(recall["successful"] or 0)
        proposals = int(run["proposals"] or 0)
        promoted = int(run["promoted"] or 0)
        from agent_knowledge_bridge.reuse import ReuseStore
        from agent_knowledge_bridge.experiences import metrics as experience_metrics
        from agent_knowledge_bridge.latency_impact import initialize, metrics as impact_metrics
        with self.knowledge._connect() as db:
            initialize(db)
            impact = impact_metrics(db, project_key)
        return {
            "latency_impact": impact,
            "experience": experience_metrics(self.knowledge, project_key),
            "reuse": ReuseStore(self.database_path).metrics(project_key),
            "project_key": project_key,
            "knowledge": {
                "candidate": statuses.get("candidate", 0),
                "active": statuses.get("active", 0),
                "quarantined": statuses.get("quarantined", 0),
                "verified_feedback": sum(int(row["verified"] or 0) for row in knowledge),
                "rejected_feedback": sum(int(row["rejected"] or 0) for row in knowledge),
            },
            "learning": {
                "runs": int(run["total"] or 0),
                "completed_runs": int(run["completed"] or 0),
                "proposals": proposals,
                "promoted": promoted,
                "promotion_rate": round(promoted / proposals, 3) if proposals else 0.0,
                "average_review_latency_ms": round(float(run["avg_latency"] or 0), 2),
            },
            "recall": {
                "interpretation": "Legacy session command correlation, not per-item reuse or Recall@K",
                "attempts": recall_total,
                "hits": recall_hits,
                "hit_rate": round(recall_hits / recall_total, 3) if recall_total else 0.0,
                "evaluated_hits": evaluated,
                "successful_validations": successful,
                "post_recall_success_rate": round(successful / evaluated, 3) if evaluated else 0.0,
                "average_latency_ms": round(float(recall["avg_latency"] or 0), 2),
                "average_injected_chars": round(float(recall["avg_injected_chars"] or 0), 2),
            },
        }

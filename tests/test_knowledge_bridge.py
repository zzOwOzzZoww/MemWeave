from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from agent_knowledge_bridge.service import KnowledgeBridgeService


class KnowledgeBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "knowledge.db"
        self.claude = KnowledgeBridgeService(
            agent_id="claude-code",
            project_key="project-a",
            database_path=self.database_path,
        )
        self.codex = KnowledgeBridgeService(
            agent_id="codex",
            project_key="project-a",
            database_path=self.database_path,
        )

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _verify(self, service, knowledge_id: str, marker: str = "result.json"):
        return service.feedback(
            knowledge_id,
            "verified",
            "The deterministic verifier passed.",
            evidence_kind="test",
            evidence_ref=marker,
        )

    def test_candidate_requires_verification_before_cross_agent_search(self) -> None:
        created = self.claude.publish(
            title="Use pnpm",
            content="This repository uses pnpm for dependency management.",
            knowledge_type="decision",
            evidence_summary="pnpm-lock.yaml exists and the test command passed.",
        )
        knowledge_id = created["knowledge"]["id"]

        self.assertEqual(created["knowledge"]["status"], "candidate")
        self.assertEqual(self.codex.search("pnpm")["count"], 0)
        queue = self.codex.review_queue()
        self.assertEqual(queue["results"][0]["id"], knowledge_id)

        feedback = self._verify(self.codex, knowledge_id, "tests/package-manager.txt")
        result = self.codex.search("pnpm")

        self.assertEqual(feedback["transition"], {"from": "candidate", "to": "active"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["source_agent"], "claude-code")
        self.assertTrue(result["results"][0]["cross_agent"])
        self.assertEqual(
            result["results"][0]["id"], knowledge_id
        )
        self.assertEqual(result["results"][0]["confidence"], 1.0)

    def test_project_scope_isolated_and_user_scope_shared(self) -> None:
        other_project = KnowledgeBridgeService(
            agent_id="codex",
            project_key="project-b",
            database_path=self.database_path,
        )
        project_record = self.claude.publish(
            title="Project-only command",
            content="PROJECT_ONLY_TOKEN",
            knowledge_type="procedure",
            evidence_summary="Project test passed.",
            scope="project",
        )
        user_record = self.claude.publish(
            title="User preference",
            content="USER_SHARED_TOKEN",
            knowledge_type="preference",
            evidence_summary="User explicitly requested this preference.",
            scope="user",
        )

        self._verify(self.claude, project_record["knowledge"]["id"])
        self._verify(self.claude, user_record["knowledge"]["id"])

        self.assertEqual(other_project.search("PROJECT_ONLY_TOKEN")["count"], 0)
        self.assertEqual(other_project.search("USER_SHARED_TOKEN")["count"], 1)
        with self.assertRaisesRegex(ValueError, "not found"):
            other_project.get(project_record["knowledge"]["id"])
        with self.assertRaisesRegex(ValueError, "not found"):
            other_project.feedback(
                project_record["knowledge"]["id"],
                "verified",
                "Cross-project mutation must be rejected.",
                evidence_kind="test",
                evidence_ref="tests/cross-project.txt",
            )
        self.assertEqual(
            other_project.get(user_record["knowledge"]["id"])["knowledge"]["scope"],
            "user",
        )

    def test_rejected_unverified_knowledge_is_quarantined(self) -> None:
        created = self.claude.publish(
            title="Unverified workaround",
            content="QUARANTINE_TOKEN",
            knowledge_type="procedure",
            evidence_summary="Observed once but not yet regression-tested.",
        )
        knowledge_id = created["knowledge"]["id"]

        feedback = self.codex.feedback(
            knowledge_id,
            "rejected",
            "The command failed its target-project test.",
            evidence_kind="test",
            evidence_ref="tests/workaround-regression.txt",
        )

        self.assertEqual(feedback["knowledge"]["status"], "quarantined")
        self.assertEqual(self.claude.search("QUARANTINE_TOKEN")["count"], 0)

    def test_conflicting_feedback_requires_explicit_review_to_restore(self) -> None:
        created = self.claude.publish(
            title="Conflict lifecycle",
            content="CONFLICT_LIFECYCLE_TOKEN",
            knowledge_type="procedure",
            evidence_summary="Proposed from a completed task.",
        )
        knowledge_id = created["knowledge"]["id"]

        self._verify(self.codex, knowledge_id, "tests/first-pass.txt")
        rejected = self.claude.feedback(
            knowledge_id,
            "rejected",
            "A regression test failed in another environment.",
            evidence_kind="test",
            evidence_ref="tests/regression-failure.txt",
        )
        restored = self.codex.feedback(
            knowledge_id,
            "verified",
            "The corrected procedure passed the target test.",
            evidence_kind="test",
            evidence_ref="tests/corrected-pass.txt",
        )

        self.assertEqual(rejected["knowledge"]["status"], "quarantined")
        self.assertEqual(rejected["knowledge"]["confidence"], 0.5)
        self.assertEqual(restored["knowledge"]["status"], "quarantined")
        self.assertEqual(restored["knowledge"]["confidence"], 0.667)
        reviewed = self.codex.feedback(knowledge_id, 'verified', 'Reviewed counterexample and fix',
            evidence_kind='user_approval', evidence_ref='review/fix')
        self.assertEqual(reviewed['knowledge']['status'], 'active')

    def test_verification_requires_traceable_objective_evidence(self) -> None:
        created = self.claude.publish(
            title="Evidence gate",
            content="EVIDENCE_GATE_TOKEN",
            knowledge_type="fact",
            evidence_summary="Candidate observation.",
        )
        with self.assertRaisesRegex(ValueError, "objective evidence_kind"):
            self.codex.feedback(
                created["knowledge"]["id"],
                "verified",
                "Trust me.",
            )
        with self.assertRaisesRegex(ValueError, "requires evidence_ref"):
            self.codex.feedback(
                created["knowledge"]["id"],
                "verified",
                "A test passed but no trace was supplied.",
                evidence_kind="test",
            )

    def test_exact_duplicates_are_reused(self) -> None:
        arguments = {
            "title": "Stable build command",
            "content": "Run BUILD_BRIDGE_TOKEN before release.",
            "knowledge_type": "procedure",
            "evidence_summary": "Build passed.",
        }
        first = self.claude.publish(**arguments)
        second = self.codex.publish(**arguments)

        self.assertEqual(first["knowledge"]["id"], second["knowledge"]["id"])
        self.assertTrue(second["deduplicated"])
        evidence = self.codex.get(first["knowledge"]["id"])["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[1]["agent_id"], "codex")
        self.assertEqual(evidence[1]["outcome"], "confirmed_duplicate")
        self.assertEqual(evidence[1]["evidence_kind"], "duplicate")

    def test_overview_distinguishes_available_from_confirmed_sharing(self) -> None:
        created = self.claude.publish(
            title="Shared build procedure",
            content="Run the deterministic build verifier before release.",
            knowledge_type="procedure",
            evidence_summary="Captured from a completed Claude task.",
        )
        knowledge_id = created["knowledge"]["id"]
        self._verify(self.claude, knowledge_id, "tests/claude-build.txt")

        available = self.claude.overview(
            "project-a", ["claude-code", "codex"]
        )
        self.assertEqual(available["results"][0]["sharing_state"], "available_shared")
        self.assertEqual(
            available["results"][0]["recall_access"],
            {"claude-code": True, "codex": True},
        )
        self.assertEqual(available["summary"]["available_shared"], 1)
        self.assertEqual(available["summary"]["confirmed_shared"], 0)

        self.codex.feedback(
            knowledge_id,
            "used",
            "Codex applied the procedure in a later task.",
        )
        confirmed = self.codex.overview(
            "project-a", ["claude-code", "codex"]
        )
        self.assertEqual(confirmed["results"][0]["sharing_state"], "confirmed_shared")
        self.assertEqual(confirmed["results"][0]["confirmed_agents"], ["claude-code", "codex"])
        self.assertEqual(confirmed["summary"]["confirmed_shared"], 1)

    def test_overview_reports_stale_as_retrievable(self) -> None:
        created = self.claude.publish(
            title="Stale but available",
            content="STALE_AVAILABLE_TOKEN",
            knowledge_type="procedure",
            evidence_summary="The procedure passed before becoming stale.",
        )
        knowledge_id = created["knowledge"]["id"]
        self._verify(self.claude, knowledge_id)
        self.claude.store.transit(
            knowledge_id,
            to_status="stale",
            reason="test fixture",
            actor="test-agent",
        )

        overview = self.claude.overview("project-a", ["claude-code", "codex"])
        record = next(item for item in overview["results"] if item["id"] == knowledge_id)

        self.assertEqual(record["sharing_state"], "available_shared")
        self.assertEqual(record["recall_access"], {"claude-code": True, "codex": True})
        self.assertEqual(overview["summary"]["status_counts"]["stale"], 1)

    def test_existing_v01_unverified_active_record_is_migrated_to_candidate(self) -> None:
        legacy_path = Path(self.temp_directory.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                """
                CREATE TABLE knowledge_records (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
                    knowledge_type TEXT NOT NULL, scope TEXT NOT NULL,
                    project_key TEXT NOT NULL, source_agent TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', content_hash TEXT NOT NULL UNIQUE,
                    adopted_count INTEGER NOT NULL DEFAULT 0,
                    verified_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE knowledge_evidence (
                    id TEXT PRIMARY KEY, knowledge_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL, outcome TEXT NOT NULL,
                    summary TEXT NOT NULL, created_at TEXT NOT NULL
                );
                INSERT INTO knowledge_records VALUES (
                    'kn_legacy', 'Legacy active record', 'LEGACY_TOKEN', 'fact',
                    'project', 'project-a', 'claude-code', 'active', 'legacy-hash',
                    0, 0, 0, '2026-09-17T00:00:00+00:00',
                    '2026-09-17T00:00:00+00:00'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        migrated = KnowledgeBridgeService(
            agent_id="codex", project_key="project-a", database_path=legacy_path
        )

        record = migrated.get("kn_legacy")["knowledge"]
        self.assertEqual(record["status"], "candidate")
        self.assertIsNone(record["source_session"])
        self.assertEqual(migrated.search("LEGACY_TOKEN")["count"], 0)
        self.assertEqual(migrated.review_queue()["results"][0]["id"], "kn_legacy")


if __name__ == "__main__":
    unittest.main()

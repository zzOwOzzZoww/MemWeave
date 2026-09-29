from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.service import KnowledgeBridgeService


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def advance(self, days: float) -> None:
        self.now += timedelta(days=days)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")


class GovernorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "knowledge.db"
        self.clock = MutableClock()
        self.service = KnowledgeBridgeService(
            agent_id="claude-code",
            project_key="project-a",
            database_path=self.database_path,
            clock=self.clock,
        )
        self.store = self.service.store
        self.governor = Governor(self.store)

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def _record(self, key: str, *, verify: int = 1, agent_id: str = "claude-code"):
        """Publish one record and give it `verify` independent confirmations."""
        created = self.service.publish(
            title=f"{key} retry policy",
            content=f"Use exponential backoff for {key} after a 503 response.",
            knowledge_type="procedure",
            evidence_summary=f"{key} backoff test passed.",
        )
        knowledge_id = created["knowledge"]["id"]
        for index in range(verify):
            self.store.feedback(
                agent_id=agent_id,
                knowledge_id=knowledge_id,
                outcome="verified",
                evidence_summary=f"verifier run {index} passed",
                evidence_kind="test",
                evidence_ref=f"tests/{key}-{index}.json",
            )
        return knowledge_id

    def _status(self, knowledge_id: str) -> str:
        return self.store.get(
            requester_agent="claude-code", knowledge_id=knowledge_id
        )["knowledge"]["status"]

    def _row_count(self) -> int:
        with self.store._connect() as db:
            return db.execute("SELECT COUNT(*) FROM knowledge_records").fetchone()[0]

    def _audit_count(self) -> int:
        with self.store._connect() as db:
            return db.execute("SELECT COUNT(*) FROM lifecycle_audit").fetchone()[0]

    # -- demotion ---------------------------------------------------------
    def test_idle_active_record_is_demoted_not_deleted(self) -> None:
        knowledge_id = self._record("ledger-svc")
        self.assertEqual(self._status(knowledge_id), "active")

        self.clock.advance(31)
        report = self.governor.sweep(project_key="project-a")

        self.assertEqual(report["demoted"], 1)
        self.assertEqual(report["archived"], 0)
        self.assertEqual(self._status(knowledge_id), "stale")
        # Demotion is a ranking change, not a removal.
        self.assertEqual(self._row_count(), 1)

    def test_sweep_is_idempotent(self) -> None:
        self._record("ledger-svc")
        self.clock.advance(31)

        first = self.governor.sweep(project_key="project-a")
        second = self.governor.sweep(project_key="project-a")

        self.assertEqual(first["demoted"], 1)
        self.assertEqual(second["demoted"], 0)
        self.assertEqual(second["transitions"], [])

    def test_very_old_active_record_completes_both_stages_in_one_sweep(self) -> None:
        knowledge_id = self._record("very-old")
        self.clock.advance(100)

        first = self.governor.sweep(project_key="project-a")
        second = self.governor.sweep(project_key="project-a")

        self.assertEqual(first["demoted"], 1)
        self.assertEqual(first["archived"], 1)
        self.assertEqual(self._status(knowledge_id), "archived")
        self.assertEqual(second["transitions"], [])
        self.assertEqual(
            [
                (row["from_status"], row["to_status"])
                for row in self.store.lifecycle_audit(knowledge_id)
            ],
            [("active", "stale"), ("stale", "archived")],
        )

    def test_duplicate_publication_does_not_refresh_confirming_activity(self) -> None:
        knowledge_id = self._record("duplicate-old")
        self.clock.advance(20)
        self.service.publish(
            title="duplicate-old retry policy",
            content="Use exponential backoff for duplicate-old after a 503 response.",
            knowledge_type="procedure",
            evidence_summary="The same claim was restated.",
        )
        self.clock.advance(11)

        report = self.governor.sweep(project_key="project-a")

        self.assertEqual(report["demoted"], 1)
        self.assertEqual(self._status(knowledge_id), "stale")

    def test_transition_rejects_a_stale_snapshot_after_status_changed(self) -> None:
        knowledge_id = self._record("concurrent")
        self.store.transit(
            knowledge_id,
            to_status="stale",
            reason="snapshot state",
            actor="test-agent",
        )
        self.store.transit(
            knowledge_id,
            to_status="active",
            reason="fresh verification arrived",
            actor="test-agent",
        )

        result = self.store.transit(
            knowledge_id,
            to_status="archived",
            reason="outdated governor decision",
            actor="memweave-governor",
            expected_status="stale",
        )

        self.assertFalse(result["changed"])
        self.assertTrue(result["conflict"])
        self.assertEqual(self._status(knowledge_id), "active")

    def test_dry_run_reports_without_writing(self) -> None:
        knowledge_id = self._record("ledger-svc")
        self.clock.advance(31)

        planned = self.governor.sweep(project_key="project-a", dry_run=True)

        self.assertEqual(planned["demoted"], 1)
        self.assertEqual(self._status(knowledge_id), "active")
        self.assertEqual(self._audit_count(), 0)

    def test_unreviewed_candidate_expires_into_quarantine_without_deletion(self) -> None:
        candidate = self.service.publish(
            title="Temporary candidate",
            content="This proposal still needs an objective check.",
            knowledge_type="fact",
            evidence_summary="Observed during exploration only.",
        )["knowledge"]["id"]
        active = self._record("kept-active")

        self.clock.advance(4)
        report = self.governor.sweep(project_key="project-a")

        self.assertEqual(report["expired_candidates"], 1)
        self.assertEqual(report["quarantined"], 1)
        self.assertEqual(self._status(candidate), "quarantined")
        self.assertEqual(self._status(active), "active")
        self.assertEqual(self._row_count(), 2)
        audit = self.store.lifecycle_audit(candidate)
        self.assertEqual((audit[-1]["from_status"], audit[-1]["to_status"]), ("candidate", "quarantined"))
        self.assertIn("review deadline expired", audit[-1]["reason"])

        second = self.governor.sweep(project_key="project-a")
        self.assertEqual(second["expired_candidates"], 0)
        self.assertEqual(second["transitions"], [])

    def test_verified_candidate_clears_review_deadline(self) -> None:
        created = self.service.publish(
            title="Verified candidate",
            content="Objective evidence will promote this record.",
            knowledge_type="procedure",
            evidence_summary="A test run is attached.",
        )["knowledge"]
        self.assertIsNotNone(created["candidate_expires_at"])
        verified = self.store.feedback(
            agent_id="claude-code",
            knowledge_id=created["id"],
            outcome="verified",
            evidence_summary="The test passed.",
            evidence_kind="test",
            evidence_ref="tests/verified-candidate.json",
            project_key="project-a",
        )
        self.assertEqual(verified["knowledge"]["status"], "active")
        self.assertIsNone(verified["knowledge"]["candidate_expires_at"])

    def test_management_read_automatically_drains_expired_candidates_only(self) -> None:
        candidate = self.service.publish(
            title="Expired management candidate",
            content="This record has no objective confirmation.",
            knowledge_type="fact",
            evidence_summary="Observation only.",
        )["knowledge"]["id"]
        active = self._record("management-active")
        self.clock.advance(4)

        records = self.service.list_records(status="all", limit=10)["results"]

        statuses = {item["id"]: item["status"] for item in records}
        self.assertEqual(statuses[candidate], "quarantined")
        self.assertEqual(statuses[active], "active")

    def test_recent_hit_keeps_a_record_in_service(self) -> None:
        """Recency is measured from the last time the record was served."""
        knowledge_id = self._record("ledger-svc")
        self.clock.advance(20)
        self.store.mark_hits([knowledge_id])
        self.clock.advance(20)

        report = self.governor.sweep(project_key="project-a")

        # 40 days since it was written, but only 20 since it was last served.
        self.assertEqual(report["demoted"], 0)
        self.assertEqual(self._status(knowledge_id), "active")

    def test_mark_hits_records_count_and_timestamp(self) -> None:
        knowledge_id = self._record("ledger-svc")

        updated = self.store.mark_hits([knowledge_id, knowledge_id])
        record = self.store.get(
            requester_agent="claude-code", knowledge_id=knowledge_id
        )["knowledge"]

        self.assertEqual(updated, 1)  # de-duplicated within the call
        self.assertEqual(record["hit_count"], 1)
        self.assertIsNotNone(record["last_hit_at"])

    # -- retirement -------------------------------------------------------
    def test_stale_record_retires_and_stays_reversible(self) -> None:
        knowledge_id = self._record("ledger-svc")

        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.clock.advance(60)
        report = self.governor.sweep(project_key="project-a")

        self.assertEqual(report["archived"], 1)
        self.assertEqual(self._status(knowledge_id), "archived")
        self.assertEqual(self._row_count(), 1)
        transitions = [
            (row["from_status"], row["to_status"])
            for row in self.store.lifecycle_audit(knowledge_id)
        ]
        self.assertEqual(
            transitions, [("active", "stale"), ("stale", "archived")]
        )

    def test_archived_record_leaves_retrieval_and_can_be_probed(self) -> None:
        knowledge_id = self._record("ledger-service-token", verify=1)
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.clock.advance(60)
        self.governor.sweep(project_key="project-a")

        served = self.service.search("ledger-service-token")
        probed = self.store.search(
            requester_agent="memweave-governor",
            project_key="project-a",
            query="ledger-service-token",
            limit=5,
            include_retired=True,
        )

        self.assertEqual(served["count"], 0)
        self.assertIn(knowledge_id, [item["id"] for item in probed["results"]])

    def test_stale_record_is_retrievable_but_ranked_below_active(self) -> None:
        fresh = self._record("ledger-svc")
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        stale = self._record("ledger-svc-alpha")
        # Force the older record stale while the newer one stays active.
        self.store.transit(
            stale, to_status="active", reason="test fixture", actor="test-agent"
        )
        self.store.transit(
            fresh, to_status="stale", reason="test fixture", actor="test-agent"
        )

        result = self.service.search("ledger-svc")

        statuses = [item["status"] for item in result["results"]]
        self.assertIn("stale", statuses)
        self.assertEqual(statuses[0], "active")
        self.assertEqual(statuses[-1], "stale")

    # -- capacity pressure ------------------------------------------------
    def test_capacity_pressure_gives_up_the_weakest_evidence_first(self) -> None:
        governor = Governor(
            self.store,
            policy={"max_active_per_project": 3, "min_active_per_project": 1},
        )
        strengths = {"alpha": 5, "beta": 4, "gamma": 3, "delta": 2, "epsilon": 1}
        ids = {
            key: self._record(key, verify=count)
            for key, count in strengths.items()
        }

        report = governor.sweep(project_key="project-a")

        demoted = {item["knowledge_id"] for item in report["transitions"]}
        self.assertEqual(report["demoted"], 2)
        self.assertEqual(demoted, {ids["delta"], ids["epsilon"]})
        self.assertEqual(self._status(ids["alpha"]), "active")

    def test_capacity_pressure_never_breaches_the_active_floor(self) -> None:
        governor = Governor(
            self.store,
            policy={"max_active_per_project": 0, "min_active_per_project": 2},
        )
        for key in ("alpha", "beta", "gamma"):
            self._record(key)

        report = governor.sweep(project_key="project-a")

        self.assertEqual(report["demoted"], 1)
        with self.store._connect() as db:
            remaining = db.execute(
                "SELECT COUNT(*) FROM knowledge_records WHERE status = 'active'"
            ).fetchone()[0]
        self.assertEqual(remaining, 2)

    # -- LFHV: retirement's own falsification test ------------------------
    def test_shadow_probe_finds_the_false_kill_and_resurrects_it(self) -> None:
        knowledge_id = self._record("ledger-svc")
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.clock.advance(60)
        self.governor.sweep(project_key="project-a")
        self.assertEqual(self._status(knowledge_id), "archived")

        probe = self.governor.shadow_probe(
            project_key="project-a", query="ledger-svc retry policy"
        )
        report = self.governor.lfhv_report(project_key="project-a")
        restored = self.governor.resurrect(project_key="project-a")

        self.assertEqual(probe["would_have_served"], [knowledge_id])
        self.assertEqual(report["retired_total"], 1)
        self.assertEqual(report["miss_rate"], 1.0)
        self.assertEqual(restored["restored"], 1)
        self.assertEqual(self._status(knowledge_id), "active")
        audit = self.store.lifecycle_audit(knowledge_id)
        self.assertEqual(audit[-1]["to_status"], "active")
        self.assertIn("LFHV", audit[-1]["reason"])
        self.assertEqual(self._row_count(), 1)

    def test_resurrected_record_is_not_re_archived_by_the_next_sweep(self) -> None:
        """A resurrection the next sweep immediately undoes is not a repair.

        The probe that justifies resurrecting a record is evidence that it is
        wanted, but it lands in the shadow ledger, which `_last_activity` does
        not read. If the resurrection does not carry that activity onto the
        record itself, the record returns reading "never served since written"
        — 91 days idle by its `created_at` — and the next sweep archives it
        again, so the false kill oscillates instead of ending.
        """
        knowledge_id = self._record("ledger-svc")
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.clock.advance(60)
        self.governor.sweep(project_key="project-a")
        self.assertEqual(self._status(knowledge_id), "archived")

        self.governor.shadow_probe(
            project_key="project-a", query="ledger-svc retry policy"
        )
        self.assertEqual(self.governor.resurrect(project_key="project-a")["restored"], 1)
        self.assertEqual(self._status(knowledge_id), "active")

        # 60 days of new idleness, measured from the resurrection rather than
        # from the original write: past the 30-day stale mark, short of the
        # 90-day archive mark, so demotion to `stale` is the honest outcome.
        # The bug this guards against archives it in one step.
        self.clock.advance(60)
        self.governor.sweep(project_key="project-a")
        self.assertEqual(self._status(knowledge_id), "stale")

        # Retrievable again, which is what the resurrection was for.
        self.assertIn(
            knowledge_id,
            [item["id"] for item in self.service.search("ledger-svc")["results"]],
        )

        # And still on the normal clock: another 31 days does archive it, so
        # the repair restores the record without making it immortal.
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.assertEqual(self._status(knowledge_id), "archived")

    def test_shadow_probe_reports_global_rank_and_query_level_miss_rate(self) -> None:
        active_id = self._record("alpha-best")
        archived_id = self._record("alpha-retired")
        self.store.transit(
            archived_id,
            to_status="archived",
            reason="test fixture",
            actor="test-agent",
        )

        first = self.governor.shadow_probe(
            project_key="project-a", query="alpha retry policy", limit=8
        )
        self.governor.shadow_probe(
            project_key="project-a", query="unrelated-topic-nothing", limit=8
        )
        report = self.governor.lfhv_report(project_key="project-a")

        self.assertIn(active_id, [
            item["id"] for item in self.store.search(
                requester_agent="test-agent",
                project_key="project-a",
                query="alpha retry policy",
                limit=8,
                include_retired=True,
            )["results"]
        ])
        self.assertEqual(first["would_have_served"], [archived_id])
        self.assertEqual(report["false_kills"][0]["best_rank"], 2)
        self.assertEqual(report["shadow_probes_recorded"], 2)
        self.assertEqual(report["queries_with_false_kill"], 1)
        self.assertEqual(report["query_miss_rate"], 0.5)

    def test_shadow_probe_skips_queries_no_retired_record_matches(self) -> None:
        knowledge_id = self._record("ledger-svc")
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")
        self.clock.advance(60)
        self.governor.sweep(project_key="project-a")

        probe = self.governor.shadow_probe(
            project_key="project-a", query="unrelated-topic-nothing"
        )
        report = self.governor.lfhv_report(project_key="project-a")

        self.assertEqual(probe["count"], 0)
        self.assertEqual(report["miss_rate"], 0.0)
        self.assertEqual(self._status(knowledge_id), "archived")

    def test_hits_can_make_retirement_impossible(self) -> None:
        """A record still being served never becomes a retirement candidate."""
        knowledge_id = self._record("ledger-svc")
        for _ in range(10):
            self.clock.advance(29)
            self.store.mark_hits([knowledge_id])

        report = self.governor.sweep(project_key="project-a")

        self.assertEqual(report["demoted"], 0)
        self.assertEqual(report["archived"], 0)
        self.assertEqual(self._status(knowledge_id), "active")

    def test_report_summarises_lifecycle_state(self) -> None:
        self._record("ledger-svc")
        self.clock.advance(31)
        self.governor.sweep(project_key="project-a")

        report = self.governor.report(project_key="project-a")

        self.assertEqual(report["status_counts"].get("stale"), 1)
        self.assertEqual(report["pending_retirements"], 0)
        self.assertIn("lfhv", report)


if __name__ == "__main__":
    unittest.main()

"""Weight budget: the framework's cost must stay a maintained property.

"Lightweight" is the kind of claim that decays silently — one convenient import
of a web framework or one audit table appended per request, and it is gone. These
tests fail when that happens, so the budget is enforced rather than asserted in a
README.

The budgets are deliberately loose enough to survive refactoring and tight enough
to catch a dependency or an unbounded table being introduced.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.store import AUDIT_ROWS_PER_RECORD, KnowledgeStore


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_knowledge_bridge"

#: Modules a caller must be able to import and use with nothing installed.
CORE_MODULES = (
    "store",
    "retrieval_pipeline",
    "retrieval_stats",
    "experiences",
    "turn_timing",
    "latency_impact",
    "governance",
    "reuse",
    "learning",
    "service",
    "claude_learning_adapter",
    "claude_transcript",
    "codex_transcript",
)
WEB_STACK = ("fastapi", "pydantic", "uvicorn", "starlette", "mcp")


class CoreDependencyBudgetTest(unittest.TestCase):
    def test_core_modules_do_not_name_the_web_stack(self) -> None:
        offenders: list[str] = []
        for name in CORE_MODULES:
            text = (PACKAGE_ROOT / f"{name}.py").read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                for package in WEB_STACK:
                    if f" {package}" in stripped or f" {package}." in stripped:
                        offenders.append(f"{name}.py: {stripped}")
        self.assertEqual(
            offenders,
            [],
            "the core must stay stdlib-only; move these behind the 'runtime' extra:\n"
            + "\n".join(offenders),
        )

    def test_importing_the_core_loads_no_installed_package(self) -> None:
        """The real test: not what the source says, but what actually gets imported."""
        script = (
            "import json, sys, tempfile\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, " + repr(str(PACKAGE_ROOT.parent)) + ")\n"
            "from agent_knowledge_bridge.service import KnowledgeBridgeService\n"
            "KnowledgeBridgeService(agent_id='a', project_key='p',\n"
            "    database_path=Path(tempfile.mkdtemp()) / 'k.db')\n"
            "loaded = sorted({n.split('.')[0] for n, m in list(sys.modules.items())\n"
            "                 if 'site-packages' in (getattr(m, '__file__', None) or '')})\n"
            "print(json.dumps(loaded))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        loaded = json.loads(result.stdout.strip().splitlines()[-1])
        # `_distutils_hack` is injected by the interpreter, not imported by us.
        self.assertEqual([name for name in loaded if name != "_distutils_hack"], [])


class StorageBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "budget.db"
        self.service = KnowledgeBridgeService(
            agent_id="claude-code", project_key="budget", database_path=self.database_path
        )
        self.store = self.service.store

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _publish(self, key: str, *, verify: int = 1, agent_id: str = "claude-code") -> str:
        created = self.service.publish(
            title=f"{key} retry policy",
            content=f"Use exponential backoff for {key} after a 503 response.",
            knowledge_type="procedure",
            evidence_summary=f"{key} backoff test passed",
        )
        knowledge_id = created["knowledge"]["id"]
        for index in range(verify):
            self.store.feedback(
                agent_id=agent_id,
                knowledge_id=knowledge_id,
                outcome="verified",
                evidence_summary="verifier passed",
                evidence_kind="test",
                evidence_ref=f"tests/{key}-{index}.json",
            )
        return knowledge_id

    def _table_count(self, table: str) -> int:
        with self.store._connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_bytes_per_record_stays_within_budget(self) -> None:
        """Catches an added column, index, or per-record blob blowing up the file."""
        for index in range(300):
            self._publish(f"svc-{index}")
        with self.store._connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
        per_record = pages * page_size / 300
        self.assertLess(
            per_record,
            6 * 1024,
            f"{per_record:.0f} bytes per record exceeds the 6 KiB budget",
        )

    def test_shadow_ledger_grows_with_retired_records_not_with_queries(self) -> None:
        """The governing ledger must not grow faster than the library it governs."""
        retired = [self._publish(f"retired-{index}") for index in range(3)]
        for knowledge_id in retired:
            self.store.transit(
                knowledge_id, to_status="archived", reason="budget test", actor="test-agent"
            )
        governor = Governor(self.store)

        for _ in range(100):
            governor.shadow_probe(project_key="budget", query="retired-1 retry policy")

        rows = self._table_count("shadow_hits")
        report = governor.lfhv_report(project_key="budget")

        # Rows are capped by how many records are retired, so 100 probes over 3
        # retired records must not produce anything near 100 rows.
        self.assertLessEqual(rows, len(retired))
        self.assertLess(rows, 100)
        # Every row must still be about a record that is actually retired.
        with self.store._connect() as db:
            ledger_ids = {
                row["knowledge_id"] for row in db.execute(
                    "SELECT knowledge_id FROM shadow_hits"
                ).fetchall()
            }
        self.assertTrue(ledger_ids.issubset(set(retired)))
        # The probe count survives as a counter, which is all the miss rate needs.
        self.assertGreaterEqual(report["shadow_probes_recorded"], 100)
        self.assertEqual(report["retired_total"], len(retired))

    def test_resurrection_clears_the_ledger_rows(self) -> None:
        knowledge_id = self._publish("retired-1")
        self.store.transit(
            knowledge_id, to_status="archived", reason="budget test", actor="test-agent"
        )
        governor = Governor(self.store)
        governor.shadow_probe(project_key="budget", query="retired-1 retry policy")
        self.assertEqual(self._table_count("shadow_hits"), 1)

        governor.resurrect(project_key="budget")

        self.assertEqual(self._table_count("shadow_hits"), 0)
        self.assertEqual(
            self.store.get(requester_agent="claude-code", knowledge_id=knowledge_id)[
                "knowledge"
            ]["status"],
            "active",
        )

    def test_lifecycle_audit_is_capped_per_record(self) -> None:
        knowledge_id = self._publish("oscillating")
        for index in range(AUDIT_ROWS_PER_RECORD + 10):
            self.store.transit(
                knowledge_id,
                to_status="stale" if index % 2 == 0 else "active",
                reason=f"oscillation {index}",
                actor="test-agent",
            )

        with self.store._connect() as db:
            rows = db.execute(
                "SELECT COUNT(*) FROM lifecycle_audit WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()[0]

        self.assertEqual(rows, AUDIT_ROWS_PER_RECORD)
        # Capping bookkeeping must not touch the record itself.
        self.assertEqual(self._table_count("knowledge_records"), 1)


if __name__ == "__main__":
    unittest.main()

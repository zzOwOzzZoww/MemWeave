from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_knowledge_bridge.compiler import CompilationIdentity
from agent_knowledge_bridge.learning import LearningStore


class KnowledgeCompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "compiler.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def identity(self, *, compiler_version: str = "compiler-v1") -> CompilationIdentity:
        return CompilationIdentity.from_turn(
            agent_id="codex",
            project_key="compiler-test",
            session_id="session-1",
            turn_hash="raw-turn-hash",
            compiler_version=compiler_version,
            schema_version="schema-v1",
        )

    def test_manifest_is_stable_but_changes_when_compiler_changes(self) -> None:
        first = self.identity()
        repeated = self.identity()
        upgraded = self.identity(compiler_version="compiler-v2")
        self.assertEqual(first.source_hash, repeated.source_hash)
        self.assertEqual(first.manifest_hash, repeated.manifest_hash)
        self.assertEqual(first.source_hash, upgraded.source_hash)
        self.assertNotEqual(first.manifest_hash, upgraded.manifest_hash)

    def test_same_manifest_is_skipped_but_upgrade_can_recompile(self) -> None:
        first = self.identity()
        run_id = self.store.begin_run(
            agent_id="codex",
            project_key="compiler-test",
            session_id="session-1",
            turn_hash=first.manifest_hash,
            source_hash=first.source_hash,
            compiler_version=first.compiler_version,
            schema_version=first.schema_version,
            input_chars=100,
        )
        self.assertIsNotNone(run_id)
        self.assertIsNone(self.store.begin_run(
            agent_id="codex", project_key="compiler-test", session_id="session-1",
            turn_hash=first.manifest_hash, source_hash=first.source_hash,
            compiler_version=first.compiler_version, schema_version=first.schema_version,
            input_chars=100,
        ))
        upgraded = self.identity(compiler_version="compiler-v2")
        self.assertIsNotNone(self.store.begin_run(
            agent_id="codex", project_key="compiler-test", session_id="session-1",
            turn_hash=upgraded.manifest_hash, source_hash=upgraded.source_hash,
            compiler_version=upgraded.compiler_version,
            schema_version=upgraded.schema_version, input_chars=100,
        ))

    def test_raw_events_are_immutable_and_knowledge_is_traceable(self) -> None:
        event_id = self.store.record_event(
            agent_id="codex",
            project_key="compiler-test",
            session_id="session-1",
            event_type="tool_result",
            payload={"command": "python verify.py", "output": "PASS"},
            success=True,
            objective_kind="test",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.knowledge._connect() as connection:
                connection.execute(
                    "UPDATE agent_events SET payload_json = '{}' WHERE id = ?", (event_id,)
                )

        identity = self.identity()
        run_id = self.store.begin_run(
            agent_id="codex", project_key="compiler-test", session_id="session-1",
            turn_hash=identity.manifest_hash, source_hash=identity.source_hash,
            compiler_version=identity.compiler_version,
            schema_version=identity.schema_version, input_chars=100,
        )
        published = self.store.knowledge.publish(
            source_agent="codex",
            project_key="compiler-test",
            title="Verified build procedure",
            content="Run the verifier after building.",
            knowledge_type="procedure",
            scope="project",
            evidence_summary="Compiled from a verified turn.",
            source_session="session-1",
        )
        knowledge_id = published["knowledge"]["id"]
        self.store.link_knowledge(knowledge_id, event_id, "supports")
        self.store.link_compilation(run_id, knowledge_id, "produced")
        provenance = self.store.compilation_provenance(knowledge_id)

        self.assertEqual(provenance["compilations"][0]["source_hash"], identity.source_hash)
        self.assertEqual(provenance["compilations"][0]["compiler_version"], "compiler-v1")
        self.assertEqual(provenance["source_events"][0]["id"], event_id)
        self.assertTrue(provenance["source_events"][0]["success"])


if __name__ == "__main__":
    unittest.main()

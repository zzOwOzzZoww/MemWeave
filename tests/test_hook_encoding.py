from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from agent_knowledge_bridge.store import KnowledgeStore


class HookEncodingTest(unittest.TestCase):
    def test_both_hooks_inject_source_only_presentation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for script, source_agent, display_name in (
                ("codex_learning_hook.py", "claude-code", "Claude Code"),
                ("claude_learning_hook.py", "codex", "Codex"),
            ):
                with self.subTest(script=script):
                    database = Path(directory) / f"{script}.db"
                    store = KnowledgeStore(database)
                    consumer = 'codex' if source_agent == 'claude-code' else 'claude-code'
                    store.register_agent(agent_id=consumer, display_name=consumer, adapter_type='runtime-api')
                    record = store.publish(source_agent=source_agent, project_key="badge-test",
                        title="badgefixture build policy", content="badgefixture uses reproducible builds.",
                        knowledge_type="procedure", evidence_summary="fixture", scope="project")["knowledge"]
                    store.feedback(agent_id="human", knowledge_id=record["id"], outcome="verified",
                        evidence_kind="test", evidence_ref="fixture", evidence_summary="fixture")
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / script), "hook"],
                        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "fixture",
                            "turn_id": "badge-turn", "prompt": "badgefixture build policy"}).encode("utf-8"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20,
                        env={**os.environ, "MW_RUNTIME_MODE": "local", "MW_DB_PATH": str(database),
                             "MW_PROJECT_KEY": "badge-test", "MW_AGENT_ID": "codex" if source_agent == "claude-code" else "claude-code"},
                    )
                    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
                    self.assertIn(f"source_label={display_name}", context)
                    self.assertIn("Never print knowledge IDs, trace IDs, session IDs", context)
                    self.assertNotIn("cite its [kn_id]", context)

    def test_both_hooks_accept_chinese_utf8_json_from_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for script in ("claude_learning_hook.py", "codex_learning_hook.py"):
                with self.subTest(script=script):
                    database = Path(directory) / f"{script}.db"
                    payload = json.dumps({
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": script,
                        "prompt": "以后优先使用简体中文解释技术问题",
                    }, ensure_ascii=False).encode("utf-8")
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "scripts" / script), "hook"],
                        input=payload,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env={**os.environ, "MW_RUNTIME_MODE": "local", "MW_DB_PATH": str(database)},
                        timeout=20,
                        check=True,
                    )
                    self.assertEqual(json.loads(result.stdout.decode("utf-8")), {})
                    self.assertFalse(database.with_suffix(".hook-errors.jsonl").exists())
                    self.assertFalse(database.with_suffix(".codex-hook-errors.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

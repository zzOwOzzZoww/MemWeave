from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.codex_transcript import parse_latest_codex_turn
from agent_knowledge_bridge.daemon import create_app


def write_codex_turn(path: Path, *, user: str, command: str, output: str) -> None:
    entries = [
        {"type": "session_meta", "payload": {"session_id": "codex-session"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": user}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": command}),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": output,
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Completed."}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(item) for item in entries) + "\n", encoding="utf-8")


class CodexLearningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.database = root / "knowledge.db"
        self.transcript = root / "rollout.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rollout_is_normalized_and_objective_command_is_detected(self) -> None:
        write_codex_turn(
            self.transcript,
            user="Create a widget using the project procedure.",
            command="python tools/verify_widget.py output.widget",
            output="Exit code: 0\nPASS",
        )
        turn = parse_latest_codex_turn(self.transcript)
        self.assertEqual(turn.user_text, "Create a widget using the project procedure.")
        self.assertEqual(len(turn.tools), 1)
        self.assertEqual(turn.tools[0].tool_name, "exec_command")
        self.assertEqual(turn.tools[0].objective_kind, "test")
        self.assertTrue(turn.tools[0].success)

    def test_desktop_custom_tool_call_is_recorded(self) -> None:
        entries = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Inspect the project."}},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec_command", "call_id": "desktop-1",
                "input": json.dumps({"cmd": "python -m pytest tests/test_cli.py -q"}),
            }},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "desktop-1",
                "output": "Exit code: 0\n1 passed",
            }},
        ]
        self.transcript.write_text(
            "\n".join(json.dumps(item) for item in entries) + "\n", encoding="utf-8"
        )
        turn = parse_latest_codex_turn(self.transcript)
        self.assertEqual(len(turn.tools), 1)
        self.assertEqual(turn.tools[0].objective_kind, "test")
        self.assertTrue(turn.tools[0].success)

    def test_global_hook_ignores_other_workspaces(self) -> None:
        allowed = Path(self.temp.name) / "allowed"
        allowed.mkdir()
        script = Path(__file__).resolve().parents[1] / "scripts" / "codex_learning_hook.py"
        result = subprocess.run(
            [sys.executable, str(script), "hook", "--workspace", str(allowed)],
            input=json.dumps({
                "hook_event_name": "Stop", "cwd": str(Path(self.temp.name) / "outside"),
                "session_id": "foreign-session", "transcript_path": "missing.jsonl",
            }),
            text=True,
            capture_output=True,
            env={**os.environ, "MW_DB_PATH": str(self.database), "MW_RUNTIME_MODE": "local"},
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "{}")
        self.assertFalse(self.database.exists())

    def test_global_hook_recalls_inside_workspace(self) -> None:
        from agent_knowledge_bridge.store import KnowledgeStore
        KnowledgeStore(self.database).register_agent(agent_id='codex', display_name='Codex', adapter_type='codex-hook')
        allowed = Path(self.temp.name) / "allowed"
        allowed.mkdir()
        script = Path(__file__).resolve().parents[1] / "scripts" / "codex_learning_hook.py"
        result = subprocess.run(
            [sys.executable, str(script), "hook", "--workspace", str(allowed)],
            input=json.dumps({
                "hook_event_name": "UserPromptSubmit", "cwd": str(allowed),
                "session_id": "allowed-session", "turn_id": "turn-1", "prompt": "Unrelated test prompt",
            }),
            text=True,
            capture_output=True,
            env={**os.environ, "MW_DB_PATH": str(self.database), "MW_RUNTIME_MODE": "local"},
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), {})
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0]
        self.assertEqual(count, 1)

    def test_codex_knowledge_is_recalled_by_the_same_engine(self) -> None:
        write_codex_turn(
            self.transcript,
            user="Create a widget using the project procedure.",
            command="python tools/verify_widget.py output.widget",
            output="Exit code: 0\nPASS",
        )

        def reviewer(review_text: str) -> dict:
            event_id = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)[-1]
            return {
                "proposals": [{
                    "title": "Codex widget procedure",
                    "content": "Build widgets with the project procedure and run the verifier.",
                    "knowledge_type": "procedure",
                    "evidence_event_ids": [event_id],
                }]
            }

        adapter = CodexLearningAdapter(
            database_path=self.database,
            agent_id="codex",
            project_key="claude-codex-mvp",
            reviewer=reviewer,
        )
        learned = adapter.learn({"session_id": "codex-learn", "transcript_path": str(self.transcript)})
        self.assertEqual(adapter.recall({'session_id':'unapproved','prompt':'widget procedure'}), {})
        record = adapter.store.knowledge.review_queue(requester_agent='codex',project_key='claude-codex-mvp')['results'][0]
        adapter.store.knowledge.feedback(agent_id='human',knowledge_id=record['id'],outcome='verified',
            evidence_summary='Reviewed widget procedure',evidence_kind='user_approval',evidence_ref='test-review')
        recalled = adapter.recall({
            "session_id": "codex-apply",
            "prompt": "Create another widget with the same procedure.",
        })
        self.assertEqual(learned["promoted"], 0)
        self.assertIn("Codex widget procedure", recalled["hookSpecificOutput"]["additionalContext"])

    def test_runtime_routes_codex_transcript_to_codex_parser(self) -> None:
        write_codex_turn(
            self.transcript,
            user="Create a widget using the project procedure.",
            command="python tools/verify_widget.py output.widget",
            output="Exit code: 0\nPASS",
        )

        def reviewer(review_text: str) -> dict:
            event_id = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)[-1]
            return {"proposals": [{
                "title": "Runtime routed Codex procedure",
                "content": "Use the Codex verifier for widget tasks.",
                "knowledge_type": "procedure",
                "evidence_event_ids": [event_id],
            }]}

        client = TestClient(create_app(
            database_path=self.database,
            api_token="codex-runtime-token",
            reviewer=reviewer,
        ))
        try:
            response = client.post(
                "/v1/learning/turn",
                headers={"Authorization": "Bearer codex-runtime-token"},
                json={
                    "agent_id": "codex",
                    "project_key": "claude-codex-mvp",
                    "session_id": "runtime-codex-learn",
                    "transcript_path": str(self.transcript),
                    "last_assistant_message": "",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["promoted"], 0)
            self.assertEqual(response.json()['proposals'], 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter, DeepSeekReviewer


def write_turn(path: Path, *, user: str, command: str, result: str, success: bool) -> None:
    entries = [
        {
            "type": "user",
            "message": {"role": "user", "content": user},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-test-1",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "toolUseResult": {"stdout": result, "stderr": "", "interrupted": False},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-test-1",
                        "is_error": not success,
                        "content": result,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Task complete."}],
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


class EvidenceReviewer:
    def __init__(self, *, cite_event: bool = True) -> None:
        self.cite_event = cite_event

    def __call__(self, review_text: str) -> dict:
        event_ids = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)
        return {
            "proposals": [
                {
                    "title": "Widget generation procedure",
                    "content": "For widget tasks, preserve the required field order and run the widget verifier.",
                    "knowledge_type": "procedure",
                    "evidence_event_ids": event_ids[-1:] if self.cite_event else [],
                }
            ]
        }


class ClaudeLearningAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "learning.db"
        self.transcript = self.root / "turn.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def adapter(self, reviewer=None) -> ClaudeLearningAdapter:
        return ClaudeLearningAdapter(
            database_path=self.database,
            agent_id="claude-code",
            project_key="learning-test",
            reviewer=reviewer or EvidenceReviewer(),
        )

    def test_successful_test_needs_review_before_recall(self) -> None:
        write_turn(
            self.transcript,
            user="Create a widget using the project procedure.",
            command="python tools/verify_widget.py output.widget",
            result="PASS",
            success=True,
        )
        adapter = self.adapter()

        learned = adapter.learn(
            {
                "session_id": "session-learn",
                "transcript_path": str(self.transcript),
            }
        )
        self.assertEqual(learned['promoted'], 0)
        self.assertEqual(adapter.recall({'session_id':'before-review', 'prompt':'widget procedure'}), {})
        pending = adapter.store.knowledge.review_queue(requester_agent='claude-code', project_key='learning-test')['results'][0]
        adapter.store.knowledge.feedback(agent_id='human', knowledge_id=pending['id'], outcome='verified',
            evidence_kind='user_approval', evidence_ref='test-review', evidence_summary='Checked the actual procedure')
        recalled = adapter.recall(
            {
                "session_id": "session-apply",
                "prompt": "Create another widget with the same procedure.",
            }
        )

        self.assertEqual(learned["proposals"], 1)
        self.assertEqual(learned["promoted"], 0)
        context = recalled["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Widget generation procedure", context)
        self.assertIn("confidence=1.0", context)

    def test_proposal_without_objective_event_stays_candidate(self) -> None:
        write_turn(
            self.transcript,
            user="Describe a possible widget workflow.",
            command="echo no-verification",
            result="done",
            success=True,
        )
        adapter = self.adapter(EvidenceReviewer(cite_event=False))

        learned = adapter.learn(
            {
                "session_id": "session-unverified",
                "transcript_path": str(self.transcript),
            }
        )

        self.assertEqual(learned["promoted"], 0)
        self.assertEqual(adapter.store.knowledge.search(
            requester_agent="claude-code",
            project_key="learning-test",
            query="widget",
            limit=5,
        )["count"], 0)
        self.assertEqual(adapter.store.knowledge.review_queue(
            requester_agent="claude-code",
            project_key="learning-test",
        )["count"], 1)

    def test_secret_storage_request_cannot_generate_spurious_policy(self) -> None:
        entries = [
            {"type": "user", "message": {"role": "user", "content": "请记住我的 API Key 是 sk-simulated-secret-123456789。"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "不能保存凭据。"}},
        ]
        self.transcript.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
            encoding="utf-8",
        )

        def reviewer_should_not_run(_: str) -> dict:
            raise AssertionError("credential-only request must not reach the reviewer")

        adapter = self.adapter(reviewer_should_not_run)
        learned = adapter.learn({"session_id": "secret-only", "transcript_path": str(self.transcript)})
        self.assertEqual(learned["proposals"], 0)
        self.assertEqual(adapter.store.knowledge.review_queue(
            requester_agent="claude-code", project_key="learning-test",
        )["count"], 0)

    def test_reviewer_accepts_json_with_wrapper_text_or_code_fence(self) -> None:
        payload = {"proposals": [{"title": "Reusable rule"}]}
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(DeepSeekReviewer._parse_json(encoded), payload)
        self.assertEqual(
            DeepSeekReviewer._parse_json(f"Here is the result:\n```json\n{encoded}\n```\n"),
            payload,
        )

    def test_reviewer_can_classify_project_independent_knowledge_as_user_scope(self) -> None:
        write_turn(
            self.transcript,
            user="Remember my stable output preference.",
            command="python tools/verify_widget.py output.widget",
            result="PASS",
            success=True,
        )

        def user_scope_reviewer(review_text: str) -> dict:
            event_id = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)[-1]
            return {"proposals": [{
                "title": "Concise output preference",
                "content": "Keep verification output concise across projects.",
                "knowledge_type": "preference",
                "scope": "user",
                "evidence_event_ids": [event_id],
            }]}

        adapter = self.adapter(user_scope_reviewer)
        adapter.learn({"session_id": "scope-user", "transcript_path": str(self.transcript)})
        result = adapter.store.knowledge.list_records(
            requester_agent="codex",
            project_key="another-project",
            scope="user",
            limit=10,
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["scope"], "user")

    def test_recalled_knowledge_collects_later_validation_feedback(self) -> None:
        self.test_successful_test_needs_review_before_recall()
        write_turn(
            self.transcript,
            user="Create another widget with the same procedure.",
            command="python tools/verify_widget.py second.widget",
            result="PASS",
            success=True,
        )
        adapter = self.adapter(lambda _: {"proposals": []})

        adapter.learn(
            {
                "session_id": "session-apply",
                "transcript_path": str(self.transcript),
            }
        )
        metrics = adapter.store.metrics("learning-test")

        self.assertEqual(metrics["recall"]["evaluated_hits"], 1)
        self.assertEqual(metrics["recall"]["successful_validations"], 1)
        self.assertEqual(metrics["recall"]["post_recall_success_rate"], 1.0)
        self.assertEqual(metrics["knowledge"]["verified_feedback"], 1)
        self.assertEqual(metrics["reuse"]["constraint_pass_items"], 0)
        self.assertEqual(metrics["reuse"]["cited_items"], 0)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.compiler import COMPILER_VERSION


def write_verified_turn(path: Path) -> None:
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Build widget gamma."}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "runtime-tool-1",
                        "name": "Bash",
                        "input": {"command": "python tools/verify_widget.py gamma.widget"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "toolUseResult": {"stdout": "PASS", "stderr": "", "interrupted": False},
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "runtime-tool-1",
                        "is_error": False,
                        "content": "PASS",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Widget verified."}],
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def evidence_reviewer(review_text: str) -> dict:
    event_id = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", review_text)[-1]
    return {
        "proposals": [
            {
                "title": "Runtime widget procedure",
                "content": "Build runtime widgets and verify them with the project verifier.",
                "knowledge_type": "procedure",
                "evidence_event_ids": [event_id],
            }
        ]
    }


class RuntimeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "runtime.db"
        self.transcript = self.root / "turn.jsonl"
        self.client = TestClient(
            create_app(
                database_path=self.database,
                api_token="runtime-test-token",
                reviewer=evidence_reviewer,
            )
        )
        self.headers = {"Authorization": "Bearer runtime-test-token"}
        self.context = {"agent_id": "claude-code", "project_key": "runtime-test"}

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def post(self, path: str, payload: dict):
        return self.client.post(path, json=payload, headers=self.headers)

    def test_runtime_requires_bearer_token(self) -> None:
        self.assertEqual(self.client.get("/v1/health").status_code, 401)
        response = self.client.get("/v1/health", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "memweave-runtime")

    def test_domain_validation_returns_structured_422(self) -> None:
        response = self.post(
            "/v1/knowledge/feedback",
            {
                **self.context,
                "knowledge_id": "kn_missing",
                "outcome": "verified",
                "evidence_summary": "No trace was supplied.",
                "evidence_kind": "observation",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("objective evidence_kind", response.json()["detail"])

    def test_knowledge_lifecycle_is_exposed_without_mcp(self) -> None:
        published = self.post(
            "/v1/knowledge/publish",
            {
                **self.context,
                "title": "Runtime-only decision",
                "content": "RUNTIME_ONLY_TOKEN",
                "knowledge_type": "decision",
                "evidence_summary": "Captured from a completed task.",
                "scope": "project",
            },
        ).json()
        knowledge_id = published["knowledge"]["id"]

        before = self.post(
            "/v1/knowledge/search",
            {**self.context, "query": "RUNTIME_ONLY_TOKEN", "limit": 5},
        ).json()
        self.assertEqual(before["count"], 0)

        feedback = self.post(
            "/v1/knowledge/feedback",
            {
                **self.context,
                "knowledge_id": knowledge_id,
                "outcome": "verified",
                "evidence_summary": "The runtime integration test passed.",
                "evidence_kind": "test",
                "evidence_ref": "tests/test_runtime_api.py",
            },
        ).json()
        after = self.post(
            "/v1/knowledge/search",
            {**self.context, "query": "RUNTIME_ONLY_TOKEN", "limit": 5},
        ).json()

        self.assertEqual(feedback["transition"], {"from": "candidate", "to": "active"})
        self.assertEqual(after["count"], 1)

    def test_knowledge_list_supports_server_side_filters(self) -> None:
        records = [
            {
                "title": "Alpha deployment fact",
                "content": "Alpha service uses the blue deployment path.",
                "knowledge_type": "fact",
                "scope": "project",
                "search_terms": "release alpha",
            },
            {
                "title": "Beta review procedure",
                "content": "Review beta changes with the checklist.",
                "knowledge_type": "procedure",
                "scope": "project",
                "search_terms": "release beta",
            },
            {
                "title": "Shared preference",
                "content": "Prefer concise verification output.",
                "knowledge_type": "preference",
                "scope": "user",
                "search_terms": "style",
            },
        ]
        for record in records:
            response = self.post(
                "/v1/knowledge/publish",
                {**self.context, "evidence_summary": "Captured for filter test.", **record},
            )
            self.assertEqual(response.status_code, 200)

        filtered = self.post(
            "/v1/knowledge/list",
            {
                **self.context,
                "status": "candidate",
                "query": "alpha",
                "knowledge_type": "fact",
                "scope": "project",
                "updated_from": "2000-01-01",
                "updated_to": "2999-12-31",
                "limit": 10,
            },
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["count"], 1)
        self.assertEqual(filtered.json()["results"][0]["title"], "Alpha deployment fact")

        user_scope = self.post(
            "/v1/knowledge/list",
            {**self.context, "scope": "user", "source_agent": "claude-code", "limit": 10},
        )
        self.assertEqual(user_scope.status_code, 200)
        self.assertEqual([item["title"] for item in user_scope.json()["results"]], ["Shared preference"])

    def test_learning_and_recall_cross_the_runtime_boundary(self) -> None:
        write_verified_turn(self.transcript)
        learned = self.post(
            "/v1/learning/turn",
            {
                **self.context,
                "session_id": "runtime-learn",
                "transcript_path": str(self.transcript),
                "last_assistant_message": "",
            },
        )
        self.assertEqual(learned.status_code, 200)
        self.assertEqual(learned.json()["promoted"], 0)
        self.assertEqual(
            learned.json()["compiler"]["compiler_version"],
            COMPILER_VERSION,
        )

        records = self.post(
            "/v1/knowledge/list", {**self.context, "status": "all", "limit": 10}
        ).json()["results"]
        detail = self.post(
            "/v1/knowledge/get",
            {**self.context, "knowledge_id": records[0]["id"]},
        ).json()
        self.assertEqual(
            detail["compilation"]["compilations"][0]["compiler_version"],
            COMPILER_VERSION,
        )
        self.assertEqual(len(detail["compilation"]["source_events"]), 1)

        approved = self.post('/v1/knowledge/feedback', {**self.context,
            'knowledge_id':records[0]['id'], 'outcome':'verified', 'evidence_kind':'user_approval',
            'evidence_ref':'review-fixture', 'evidence_summary':'Reviewed the generated procedure'})
        self.assertEqual(approved.status_code, 200)

        recalled = self.post(
            "/v1/learning/recall",
            {
                **self.context,
                "session_id": "runtime-apply",
                "prompt": "Build another runtime widget with the same procedure.",
            },
        )
        self.assertEqual(recalled.status_code, 200)
        context = recalled.json()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Runtime widget procedure", context)

    def test_instance_status_relays_configured_file_without_local_paths(self) -> None:
        # Unset is the default for the framework: no vertical instance attached.
        with patch.dict("os.environ", {"MW_INSTANCE_STATUS_PATH": ""}):
            self.assertEqual(
                self.client.get("/v1/instance/status", headers=self.headers).json(),
                {"state": "not_configured"},
            )

        status = self.root / "daily-pipeline.json"
        status.write_text(
            json.dumps({
                "state": "running",
                "stage": "static-validation",
                "input_count": 30,
                "artifact": r"C:\Users\someone\idps\delta\rules.xml",
                "log": "/home/someone/.dpi-runs/run-17/pipeline.log",
            }),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"MW_INSTANCE_STATUS_PATH": str(status)}):
            payload = self.client.get("/v1/instance/status", headers=self.headers).json()
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["input_count"], 30)
        # Relative labels survive; host-local locations do not.
        self.assertEqual(payload["artifact"], "rules.xml")
        self.assertEqual(payload["log"], "pipeline.log")
        self.assertNotIn("someone", json.dumps(payload))

        missing = self.root / "absent.json"
        with patch.dict("os.environ", {"MW_INSTANCE_STATUS_PATH": str(missing)}):
            payload = self.client.get("/v1/instance/status", headers=self.headers).json()
        self.assertEqual(payload, {"state": "not_started"})

    def test_knowledge_management_dashboard_and_overview_are_exposed(self) -> None:
        dashboard = self.client.get("/knowledge")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("MemWeave 知识管理", dashboard.text)
        self.assertIn("检测本机 Agent", dashboard.text)
        self.assertIn("agentCandidate", dashboard.text)
        self.assertIn("/v1/agents/register", dashboard.text)
        self.assertIn("filter(item => item.installed)", dashboard.text)
        self.assertIn("已加入 MemWeave", dashboard.text)
        self.assertIn('id="knowledgeType"', dashboard.text)
        self.assertIn('id="scope"', dashboard.text)
        self.assertIn('id="sourceAgent"', dashboard.text)
        self.assertIn('id="updatedFrom"', dashboard.text)
        self.assertIn('id="updatedTo"', dashboard.text)
        self.assertIn("待审核至", dashboard.text)
        self.assertIn('class="knowledge-filters"', dashboard.text)
        self.assertIn('id="pendingHeading">待采纳清单', dashboard.text)
        self.assertIn('id="adoptedModule" class="knowledge-module adopted-module"', dashboard.text)
        self.assertIn('id="pendingRows"', dashboard.text)
        self.assertIn('id="adoptedRows"', dashboard.text)
        self.assertNotIn('id="rows"', dashboard.text)
        self.assertIn('id="pendingModule"', dashboard.text)
        self.assertIn('class="summary-chevron"', dashboard.text)
        self.assertIn('全局 Hook 缺失、受限或被禁用', dashboard.text)
        self.assertIn('默认共享池', dashboard.text)
        self.assertIn('data-repair-agent', dashboard.text)
        self.assertIn('最近学习完成', dashboard.text)
        self.assertIn('data-time-sort aria-sort="descending"', dashboard.text)
        self.assertNotIn('id="sortDirection"', dashboard.text)

        overview = self.post(
            "/v1/knowledge/overview",
            {
                "project_key": "runtime-test",
                "agent_ids": ["claude-code", "codex"],
                "limit": 100,
            },
        )
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["agent_ids"], ["claude-code", "codex"])

    def test_governance_endpoints_expose_lifecycle_state(self) -> None:
        published = self.post(
            "/v1/knowledge/publish",
            {
                **self.context,
                "title": "ledger-svc retry policy",
                "content": "Retry ledger-svc with exponential backoff.",
                "knowledge_type": "procedure",
                "evidence_summary": "backoff test passed",
            },
        ).json()
        knowledge_id = published["knowledge"]["id"]
        self.post(
            "/v1/knowledge/feedback",
            {
                **self.context,
                "knowledge_id": knowledge_id,
                "outcome": "verified",
                "evidence_summary": "verifier passed",
                "evidence_kind": "test",
                "evidence_ref": "tests/ledger.json",
            },
        )

        report = self.post("/v1/governance/report", self.context)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["status_counts"].get("active"), 1)
        self.assertIn("lfhv", report.json())

        # A sweep is dry-run unless the caller opts out, and a fresh record with
        # confirming evidence is never a retirement candidate.
        sweep = self.post("/v1/governance/sweep", self.context).json()
        self.assertTrue(sweep["dry_run"])
        self.assertEqual(sweep["demoted"], 0)
        self.assertEqual(sweep["archived"], 0)

        lfhv = self.post("/v1/governance/lfhv", self.context).json()
        self.assertEqual(lfhv["retired_total"], 0)
        self.assertIsNone(lfhv["miss_rate"])

        resurrect = self.post("/v1/governance/resurrect", self.context).json()
        self.assertEqual(resurrect["restored"], 0)
        self.assertEqual(
            self.post(
                "/v1/knowledge/get",
                {**self.context, "knowledge_id": knowledge_id},
            ).json()["knowledge"]["status"],
            "active",
        )


if __name__ == "__main__":
    unittest.main()

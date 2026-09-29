import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.evaluation import retrieval_metrics, paired_metrics
from agent_knowledge_bridge.reuse import ReuseStore, digest, observed_source_labels


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "memory.db"
        self.store = ReuseStore(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, number=1, artifact="out.json", value=2):
        return {"id": f"kn_{number}", "source_agent": "claude-code", "title": "Widget",
                "content": json.dumps({"reuse_check": {"kind": "json_equals",
                    "artifact": artifact, "equals": {"version": value}}})}

    def start(self, records=None, **kwargs):
        args = dict(agent_id="codex", project_key="p", session_id="s", prompt="widget",
                    records=records or [self.record()], retrieval_ms=2,
                    workspace=str(self.root), turn_id="turn1")
        args.update(kwargs)
        return self.store.start(**args)

    def finish(self, trace, citations="kn_1", **kwargs):
        args = dict(agent_id="codex", project_key="p", session_id="s",
                    turn=TranscriptTurn("widget", trace + " " + citations, ()))
        args.update(kwargs)
        return self.store.complete(**args)

    def items(self):
        return self.store.list("p")[0]["items"]

    def test_only_relevant_constraint_passes_not_all_recalled_items(self):
        trace, _, _ = self.start([self.record(), self.record(2, value=3)])
        (self.root / "out.json").write_text('{"version":2}')
        self.finish(trace, "kn_1 kn_2")
        self.assertEqual([i["check_status"] for i in self.items()], ["constraint_pass", "constraint_fail"])
        metrics = self.store.metrics("p")
        self.assertEqual(metrics["cross_agent_constraint_pass_items"], 1)
        self.assertEqual(metrics["checked_items"], 2)
        self.assertIsNone(metrics["ttft_ms"])
        self.assertIsNone(self.finish(trace))
        self.assertEqual(self.store.metrics("p"), metrics)

    def test_stale_artifact_and_bool_not_integer(self):
        (self.root / "out.json").write_text('{"version":2}')
        trace, _, _ = self.start()
        self.finish(trace)
        self.assertEqual(self.items()[0]["check_status"], "unchanged_artifact")
        trace, _, _ = self.start([self.record(value=1)], turn_id="turn2")
        (self.root / "out.json").write_text('{"version":true}')
        self.finish(trace)
        self.assertEqual(self.items()[0]["check_status"], "constraint_fail")

    def test_cross_agent_metrics_deduplicate_requests_and_evidence_types(self):
        self.start([self.record(), self.record(2)])
        self.finish("", turn_id="turn1", turn=TranscriptTurn(
            "widget", "kn_1 kn_2\n`来源：Claude Code`", ()))
        self.start([{**self.record(), "source_agent": "codex"}],
                   agent_id="claude-code", turn_id="reverse")
        self.finish("", agent_id="claude-code", turn_id="reverse",
                    turn=TranscriptTurn("widget", "完成。\n`来源：Codex`", ()))
        # Still-running requests count as output, but not completion evidence.
        self.start(turn_id="in-progress")
        metrics = self.store.metrics("p")
        self.assertEqual(metrics["cross_agent_emitted_turns"], 3)
        self.assertEqual(metrics["cross_agent_cited_turns"], 1)
        self.assertEqual(metrics["cross_agent_source_attributed_turns"], 2)
        self.assertEqual(metrics["cross_agent_evidence_turns"], 2)
        self.assertEqual(metrics["cross_agent_checked_items"], 0)
        self.assertEqual(metrics["cross_agent_constraint_pass_items"], 0)

    def test_cross_agent_metrics_exclude_self_unemitted_empty_and_other_project(self):
        self.start([{**self.record(), "source_agent": "codex"}])
        self.finish("", turn_id="turn1", turn=TranscriptTurn(
            "widget", "kn_1\n`来源：Codex`", ()))
        self.start([{**self.record(2), "content": "x" * 5000}], turn_id="unemitted")
        self.finish("", turn_id="unemitted", turn=TranscriptTurn(
            "widget", "kn_2\n`来源：Claude Code`", ()))
        self.store.start(agent_id="codex", project_key="p", session_id="s",
                         prompt="empty", records=[], retrieval_ms=1, turn_id="empty")
        self.start(project_key="elsewhere")
        metrics = self.store.metrics("p")
        self.assertEqual(metrics["traces"], 3)
        self.assertEqual(metrics["source_attributed_turns"], 1)
        for key in ("cross_agent_emitted_turns", "cross_agent_cited_turns",
                    "cross_agent_source_attributed_turns", "cross_agent_evidence_turns"):
            self.assertEqual(metrics[key], 0, key)

    def test_legacy_id_citation_is_evidence_without_inventing_source_badge(self):
        trace, _, _ = self.start()
        self.finish(trace)
        with self.store.knowledge._connect() as db:
            items = json.loads(db.execute(
                "SELECT items_json FROM reuse_traces WHERE id=?", (trace,)).fetchone()[0])
            items[0].pop("source_attribution_observed")
            db.execute("UPDATE reuse_traces SET items_json=? WHERE id=?", (json.dumps(items), trace))
        metrics = self.store.metrics("p")
        self.assertEqual(metrics["cross_agent_evidence_turns"], 1)
        self.assertEqual(metrics["cross_agent_cited_turns"], 1)
        self.assertEqual(metrics["cross_agent_source_attributed_turns"], 0)
        self.assertEqual(metrics["source_attributed_turns"], 0)
        self.assertEqual(metrics["cross_agent_constraint_pass_items"], 0)

    def test_missing_citation_no_credit_even_with_good_artifact(self):
        trace, _, _ = self.start()
        (self.root / "out.json").write_text('{"version":2}')
        self.finish(trace, "")
        self.assertEqual(self.items()[0]["check_status"], "not_cited")

    def test_chat_instructions_use_source_badges_and_keep_ids_internal(self):
        records = [self.record(), {**self.record(2), "source_agent": "codex"}]
        trace, context, emitted = self.start(records)
        self.assertEqual(emitted, ["kn_1", "kn_2"])
        self.assertIn("source_label=Claude Code", context)
        self.assertIn("source_label=Codex", context)
        self.assertIn("`来源：Claude Code`", context)
        self.assertIn("Never print knowledge IDs, trace IDs, session IDs", context)
        self.assertNotIn("cite its [kn_id]", context)
        # Internal IDs still provide database traceability, without requesting
        # their appearance in the user's conversation.
        self.assertIn(trace, context)
        self.assertIn("[kn_1]", context)

    def test_source_badge_links_turn_without_claiming_specific_item_use(self):
        self.start([self.record(), self.record(2)])
        (self.root / "out.json").write_text('{"version":2}')
        self.finish("", turn_id="turn1",
                    turn=TranscriptTurn("widget", "已按策略完成。\n\n`来源：Claude Code`", ()))
        trace = self.store.list("p")[0]
        self.assertEqual(trace["association"], "turn_id")
        self.assertTrue(all(i["source_attribution_observed"] for i in trace["items"]))
        self.assertFalse(any(i["citation_observed"] for i in trace["items"]))
        self.assertTrue(all(i["check_status"] == "not_cited" for i in trace["items"]))
        metrics = self.store.metrics("p")
        self.assertEqual(metrics["source_attributed_turns"], 1)
        self.assertEqual(metrics["cited_items"], 0)
        self.assertEqual(metrics["cross_agent_constraint_pass_items"], 0)

    def test_source_attribution_requires_matching_emitted_source(self):
        self.start([self.record(), {**self.record(2), "source_agent": "codex"}])
        self.finish("", turn_id="turn1",
                    turn=TranscriptTurn("widget", "已完成。\n`来源：Codex`", ()))
        self.assertEqual([i["source_attribution_observed"] for i in self.items()], [False, True])

        self.start(turn_id="unrelated")
        self.finish("", turn_id="unrelated",
                    turn=TranscriptTurn("widget", "已完成。\n`来源：Cursor`", ()))
        self.assertFalse(self.items()[0]["source_attribution_observed"])
        self.assertEqual(self.store.metrics("p")["source_attributed_turns"], 1)

    def test_badge_for_unemitted_record_does_not_count(self):
        large = {**self.record(), "content": "x" * 5000}
        self.start([large])
        self.finish("", turn_id="turn1",
                    turn=TranscriptTurn("widget", "已完成。\n`来源：Claude Code`", ()))
        self.assertEqual(self.store.metrics("p")["source_attributed_turns"], 0)

    def test_source_badges_ignore_examples_and_tool_echoes(self):
        for text in (
            "例如 `来源：Claude Code` 这样的标签。",
            "> `来源：Claude Code`",
            "```markdown\n`来源：Claude Code`\n```",
            "~~~\n`来源：Claude Code`\n~~~",
            "<!--\n`来源：Claude Code`\n-->",
        ):
            with self.subTest(text=text):
                self.assertEqual(observed_source_labels(text), set())
        self.assertEqual(observed_source_labels(
            "已完成。\n`来源：Claude Code`\n`来源：Codex`\n`来源：Claude Code`"
        ), {"Claude Code", "Codex"})
        self.start()
        tool = ToolEvent("t", "Bash", "echo `来源：Claude Code`", "`来源：Claude Code`", True, None)
        self.finish("", turn_id="turn1", turn=TranscriptTurn("widget", "done", (tool,)))
        self.assertEqual(self.store.metrics("p")["source_attributed_turns"], 0)

    def test_source_only_claude_turn_keeps_weak_association_explicit(self):
        self.start(agent_id="claude-code", turn_id=None,
                   records=[{**self.record(), "source_agent": "codex"}])
        (self.root / "out.json").write_text('{"version":2}')
        self.finish("", agent_id="claude-code",
                    turn=TranscriptTurn("widget", "已完成。\n`来源：Codex`", ()))
        trace = self.store.list("p")[0]
        self.assertEqual(trace["association"], "unique_prompt_hash")
        self.assertTrue(trace["items"][0]["source_attribution_observed"])
        self.assertEqual(trace["items"][0]["check_status"], "weak_turn_association")

    def test_historical_traces_are_not_backfilled_as_source_attributions(self):
        trace, _, _ = self.start()
        with self.store.knowledge._connect() as db:
            items = json.loads(db.execute("SELECT items_json FROM reuse_traces WHERE id=?", (trace,)).fetchone()[0])
            for item in items:
                item.pop("source_label")
                item.pop("source_attribution_observed")
            db.execute("UPDATE reuse_traces SET items_json=? WHERE id=?", (json.dumps(items), trace))
        self.assertEqual(self.items()[0]["source_label"], "Claude Code")
        self.assertFalse(self.items()[0]["source_attribution_observed"])
        self.assertEqual(self.store.metrics("p")["source_attributed_turns"], 0)

    def test_path_escape_and_absolute_path_rejected(self):
        for path in ("../outside.json", str(self.root / "out.json")):
            trace, _, _ = self.start([self.record(artifact=path)], turn_id=path)
            self.finish(trace)
            self.assertEqual(self.items()[0]["check_status"], "unsafe_path")

    def test_budget_preserves_whole_blocks_and_exact_emitted_ids(self):
        large = self.record()
        large["content"] = "x" * 5000
        trace, context, emitted = self.start([large, self.record(2)])
        self.assertEqual(emitted, ["kn_2"])
        self.assertNotIn("[kn_1]", context)
        self.assertTrue(context.endswith("</memweave_context>"))
        self.assertLessEqual(len(context), 4000)
        self.assertFalse(self.items()[0]["emitted"])

    def test_agent_project_session_and_turn_isolation(self):
        trace, _, _ = self.start()
        for overrides in ({"agent_id":"claude-code"}, {"project_key":"other"},
                          {"session_id":"other"}, {"turn_id":"wrong"}):
            self.assertIsNone(self.finish(trace, **overrides))
        self.assertIsNone(self.store.list("p")[0]["completed_at"])

    def test_ambiguous_prompt_and_weak_association_do_not_verify(self):
        self.start(turn_id="a")
        self.start(turn_id="b")
        self.assertIsNone(self.finish(""))
        self.assertEqual(self.store.metrics("p")["constraint_pass_items"], 0)
        self.start(session_id="unique")
        (self.root / "out.json").write_text('{"version":2}')
        self.finish("", session_id="unique")
        self.assertEqual(self.items()[0]["check_status"], "weak_turn_association")

    def test_tool_output_echo_is_not_a_citation(self):
        trace, _, _ = self.start()
        tool = ToolEvent("t", "Bash", "pytest unrelated", trace + " kn_1", True, "test")
        self.finish("", turn_id="turn1", turn=TranscriptTurn("widget", "done", (tool,)))
        self.assertFalse(self.items()[0]["citation_observed"])
        self.assertEqual(self.items()[0]["check_status"], "not_cited")

    def test_legacy_feedback_does_not_become_reuse(self):
        k = self.store.knowledge.publish(source_agent="claude-code", project_key="p",
            title="widget", content="widget procedure", knowledge_type="procedure",
            evidence_summary="legacy", scope="project")["knowledge"]["id"]
        self.store.knowledge.feedback(agent_id="codex", knowledge_id=k, outcome="verified",
            evidence_kind="test", evidence_ref="old-test", evidence_summary="legacy pass")
        self.assertEqual(ReuseStore(self.db).metrics("p")["constraint_pass_items"], 0)
        self.assertEqual(self.store.knowledge.get(requester_agent="codex", knowledge_id=k)["knowledge"]["verified_count"], 1)

    def test_retry_identical_context_and_long_prompt(self):
        adapter = ClaudeLearningAdapter(database_path=self.db, agent_id="codex", project_key="p",
                                        reviewer=lambda _: {"proposals": []})
        k = adapter.store.knowledge.publish(source_agent="claude-code", project_key="p",
            title="widget", content="widget procedure", knowledge_type="procedure",
            evidence_summary="fixture", scope="project")["knowledge"]["id"]
        adapter.store.knowledge.feedback(agent_id="human", knowledge_id=k, outcome="verified",
            evidence_kind="test", evidence_ref="fixture", evidence_summary="fixture")
        request = {"prompt":"widget " * 200, "session_id":"s", "turn_id":"retry"}
        first = adapter.recall(request)
        self.assertEqual(first, adapter.recall(request))
        self.assertEqual(self.store.metrics("p")["traces"], 1)
        with self.assertRaises(ValueError):
            adapter.recall({**request, "prompt":"different"})

    def test_authenticated_scoped_api_hides_snapshot_content(self):
        self.start()
        with TestClient(create_app(database_path=self.db, api_token="test")) as client:
            self.assertEqual(client.post('/v1/reuse/traces', json={"project_key":"p"}).status_code,401)
            def get(project):
                return client.post('/v1/reuse/traces', json={"project_key":project},
                    headers={"Authorization":"Bearer test"}).json()
            self.assertEqual(get("other")["results"], [])
            row = get("p")["results"][0]
            self.assertNotIn("context_text", row)
            self.assertNotIn("check_spec", row["items"][0])
            self.assertEqual(client.get('/knowledge').status_code, 200)

    def test_runtime_round_trip_preserves_turn_workspace_and_evidence(self):
        k = self.store.knowledge.publish(source_agent="codex", project_key="p",
            title="widget", content=self.record()["content"], knowledge_type="procedure",
            evidence_summary="fixture", scope="project")["knowledge"]["id"]
        self.store.knowledge.feedback(agent_id="human", knowledge_id=k, outcome="verified",
            evidence_kind="test", evidence_ref="fixture", evidence_summary="fixture")
        headers = {"Authorization":"Bearer test"}
        base = {"agent_id":"claude-code", "project_key":"p", "session_id":"api", "turn_id":"api-turn"}
        with TestClient(create_app(database_path=self.db, api_token="test",
                                  reviewer=lambda _: {"proposals": []})) as client:
            response = client.post('/v1/learning/recall', headers=headers,
                json={**base, "cwd":str(self.root), "prompt":"widget"})
            self.assertEqual(response.status_code, 200)
            context = response.json()["hookSpecificOutput"]["additionalContext"]
            self.assertIn(k, context)
            trace = self.store.list("p")[0]
            (self.root / "out.json").write_text('{"version":2}')
            transcript = self.root / "api.jsonl"
            transcript.write_text("\n".join(json.dumps(entry) for entry in [
                {"type":"user", "message":{"role":"user", "content":"widget"}},
                {"type":"assistant", "message":{"role":"assistant", "content":trace["id"] + " " + k}}
            ]), encoding="utf-8")
            response = client.post('/v1/learning/turn', headers=headers,
                json={**base, "cwd":str(self.root), "transcript_path":str(transcript)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.items()[0]["check_status"], "constraint_pass")
            self.assertEqual(self.store.metrics("p")["cross_agent_constraint_pass_items"], 1)
            with self.store.knowledge._connect() as db:
                query = db.execute("SELECT query FROM recall_events").fetchone()[0]
            self.assertEqual(query, "sha256:" + digest("widget"))


class EvaluationTests(unittest.TestCase):
    def test_gold_metrics_denominators_and_negatives(self):
        cases = [dict(expected_ids=["a","b"], retrieved_ids=["a","x"], emitted_ids=["a","x"],
                      retrieval_ms=1, adapter_ms=2),
                 dict(expected_ids=[], retrieved_ids=["x"], emitted_ids=["x"], retrieval_ms=3, adapter_ms=4)]
        metrics = retrieval_metrics(cases, k=2)
        self.assertEqual(metrics["recall_at_k"], .5)
        self.assertEqual(metrics["precision_at_k"], .5)
        self.assertEqual(metrics["negative_injection_rate"], 1)
        self.assertIsNone(retrieval_metrics([])["recall_at_k"])

    def test_paired_scorer_missing_results_and_mixed_models(self):
        base = dict(task_id="a", repeat=1, model="deepseek-flash", snapshot="v1", dataset_version="v1",
                    evidence_ref="test:fixture")
        rows = [{**base, "mode":mode, "success":mode != "baseline"} for mode in ("baseline","auto","oracle")]
        self.assertEqual(paired_metrics(rows)["auto_minus_baseline"], 1)
        self.assertIsNone(paired_metrics(rows[:2])["auto_minus_baseline"])
        self.assertIsNone(paired_metrics(rows)["performance"]["auto"]["ttft_ms"]["p50"])
        with self.assertRaises(ValueError):
            paired_metrics([*rows[:2], {**rows[2], "model":"other"}])
        with self.assertRaises(ValueError):
            paired_metrics([*rows, rows[0]])


if __name__ == "__main__":
    unittest.main()

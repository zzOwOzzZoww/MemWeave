"""Experience learning is evidence-bound and separate from causal success."""
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from agent_knowledge_bridge.experiences import (
    validate_contract, applicable, observe_contract, metrics, filter_rows, encode_contract)
from agent_knowledge_bridge.daemon import create_app

COMMAND = "python -m pytest tests/test_widget.py -q"
CONTRACT = {
    "version": 1, "applies_when": ["widget", "Windows"], "exclude_when": ["Linux"],
    "steps": ["Read the widget schema before editing.", "Preserve field order and run the widget check."],
    "avoid": ["Do not write an unordered widget."],
    "reason": "The widget check failed for unordered fields and passed after correction.",
    "verifier": {"kind": "observed_command", "command": COMMAND},
}


def tool(success, i=0, command=COMMAND):
    return ToolEvent(f"tool-{i}", "exec_command", json.dumps({"cmd": command}),
                     "PASS" if success else "FAIL: field order", success, "test")


def reviewer(text):
    ids = re.findall(r"EVENT_ID: (ae_[a-f0-9]+)", text)
    return {"proposals": [{
        "title": "Windows widget field order", "content": "Use the proven field order.",
        "knowledge_type": "procedure", "scope": "project", "evidence_event_ids": ids,
        "experience": copy.deepcopy(CONTRACT)}]}


def make_adapter(tmp_path, agent="claude-code", turn=None, review=reviewer):
    adapter_cls = ClaudeLearningAdapter if agent == "claude-code" else CodexLearningAdapter
    adapter = adapter_cls(database_path=tmp_path / "db.sqlite", agent_id=agent,
                          project_key="p", reviewer=review)
    if turn is not None:
        adapter.transcript_parser = lambda *a, **k: turn
    return adapter


def seed(tmp_path, outcomes=(False, True), review=reviewer, agent="claude-code"):
    turn = TranscriptTurn("Build widget for Windows, not Linux", "Corrected field order.",
                          tuple(tool(status, i) for i, status in enumerate(outcomes)))
    adapter = make_adapter(tmp_path, agent=agent, turn=turn, review=review)
    result = adapter.learn({"session_id": "source", "transcript_path": "fake"})
    with adapter.store.knowledge._connect() as db:
        rows = db.execute("SELECT id,status,content FROM knowledge_records ORDER BY created_at").fetchall()
    return adapter, result, rows


@pytest.mark.parametrize("source,target", [("claude-code", "codex"), ("codex", "claude-code")])
def test_bidirectional_experience_transfer_and_real_verifier_observation(tmp_path, source, target):
    original, learned, records = seed(tmp_path, agent=source)
    assert (learned["proposals"], learned["promoted"]) == (1, 1)
    consumer = make_adapter(tmp_path, agent=target)
    recalled = consumer.recall({"session_id": "consume", "turn_id": "t", "prompt": "Build widget for Windows"})
    assert "执行检查清单" in recalled["hookSpecificOutput"]["additionalContext"]
    assert COMMAND in recalled["hookSpecificOutput"]["additionalContext"]
    # No technical ID in the answer is needed; exact verifier observation is
    # counted separately and does not claim citation or actual adoption.
    consumer.reuse.complete(agent_id=target, project_key="p", session_id="consume", turn_id="t",
                            turn=TranscriptTurn("Build widget for Windows", "Done", (tool(True),)))
    observed = metrics(consumer.store.knowledge, "p")
    assert observed["cross_agent_passed"] == 1
    assert observed["passed"] == 1
    assert consumer.reuse.metrics("p")["cited_items"] == 0


@pytest.mark.parametrize("outcomes", [(False,), (True,), (True, False), (False, True, False)])
def test_unresolved_or_success_only_stays_candidate(tmp_path, outcomes):
    adapter, learned, rows = seed(tmp_path, outcomes)
    assert learned["proposals"] == 1 and learned["promoted"] == 0
    assert rows[0]["status"] == "candidate"
    assert adapter.store.knowledge.search(requester_agent="codex", project_key="p",
        query="Build widget for Windows", limit=3)["count"] == 0


def test_reviewer_cannot_hide_later_failure(tmp_path):
    def misleading(text):
        value = reviewer(text)
        value["proposals"][0]["evidence_event_ids"] = value["proposals"][0]["evidence_event_ids"][:2]
        return value
    _, learned, rows = seed(tmp_path, (False, True, False), misleading)
    assert learned["promoted"] == 0 and rows[0]["status"] == "candidate"


@pytest.mark.parametrize("edit", ["fake_verifier", "unknown_condition", "credential", "invalid", "invented_exclusion"])
def test_unsupported_contract_rejected(tmp_path, edit):
    def malformed(text):
        value = reviewer(text)
        c = value["proposals"][0]["experience"]
        if edit == "fake_verifier":
            c["verifier"]["command"] = "python never_observed.py"
        elif edit == "unknown_condition":
            c["applies_when"] = ["nonexistent-framework"]
        elif edit == "invented_exclusion":
            c["exclude_when"] = ["nonexistent-exclusion"]
        elif edit == "credential":
            c["reason"] = "password=do-not-store-this-secret"
        else:
            c["steps"] = []
        return value
    _, learned, rows = seed(tmp_path, review=malformed)
    assert learned["proposals"] == 0 and not rows


@pytest.mark.parametrize("query,expected", [
    ("Build widget for Windows", True), ("Build widget", False),
    ("Build widget for Windows Linux", False), ("widget Windowsish", False),
    ("weather Windows", False), ("WIDGET WINDOWS", True),
])
def test_conditions_are_all_required_and_exclusion_wins(query, expected):
    assert applicable(validate_contract(CONTRACT), query) == expected
    row = {"content": encode_contract(CONTRACT)}
    assert bool(filter_rows([row], query)) == expected


def test_scope_gate_applies_to_search_and_direct_emission(tmp_path):
    adapter, _, rows = seed(tmp_path)
    key = rows[0]["id"]
    for query in ("widget Windows Linux", "widget", "weather"):
        assert adapter.store.knowledge.search(requester_agent="codex", project_key="p",
                                              query=query, limit=3)["count"] == 0
    assert adapter.store.knowledge.search(requester_agent="codex", project_key="other",
                                          query="widget Windows", limit=3)["count"] == 0
    record = adapter.store.knowledge.get(requester_agent="codex", knowledge_id=key)["knowledge"]
    _, content, emitted = adapter.reuse.start(agent_id="codex", project_key="p",
        session_id="outside", turn_id="x", prompt="widget Windows Linux", records=[record], retrieval_ms=1)
    assert content == "" and emitted == []
    item = json.loads(adapter.reuse.existing("codex", "p", "outside", "x")["items_json"])[0]
    assert item["omitted_reason"] == "not_applicable"


def complete(adapter, statuses=(True,), turn_id="target"):
    adapter.recall({"session_id": "consumer", "turn_id": turn_id, "prompt": "widget Windows"})
    return adapter.reuse.complete(agent_id=adapter.agent_id, project_key="p",
        session_id="consumer", turn_id=turn_id,
        turn=TranscriptTurn("widget Windows", "Done", tuple(tool(v, i) for i, v in enumerate(statuses))))


def test_counterexample_suspends_even_after_many_passes(tmp_path):
    source, _, rows = seed(tmp_path)
    consumer = make_adapter(tmp_path, "codex")
    for i in range(3):
        complete(consumer, turn_id=f"pass-{i}")
    complete(consumer, (False,), "fail")
    report = metrics(source.store.knowledge, "p")
    assert report["passed"] == 3 and report["failed"] == 1 and report["suspended"] == 1
    assert consumer.recall({"session_id": "next", "turn_id": "next", "prompt": "widget Windows"}) == {}
    # A duplicate successful lesson must not silently reactivate a suspension.
    source.transcript_parser = lambda *a, **k: TranscriptTurn(
        "Build widget for Windows, not Linux", "Later rerun", (tool(False, 9), tool(True, 10)))
    source.learn({"session_id": "new-source", "transcript_path": "fake"})
    assert metrics(source.store.knowledge, "p")["suspended"] == 1


def test_failure_then_recovery_recorded_without_suspension(tmp_path):
    source, _, _ = seed(tmp_path)
    complete(make_adapter(tmp_path, "codex"), (False, True))
    report = metrics(source.store.knowledge, "p")
    assert report["recovered"] == 1 and report["failure_events"] == 1 and report["suspended"] == 0


def test_missing_check_and_weak_association_never_claim_success(tmp_path):
    source, _, _ = seed(tmp_path)
    consumer = make_adapter(tmp_path, "codex")
    complete(consumer, (), "none")
    consumer.recall({"session_id": "weak", "prompt": "widget Windows"})
    consumer.reuse.complete(agent_id="codex", project_key="p", session_id="weak",
                           turn=TranscriptTurn("widget Windows", "Done", (tool(True),)))
    report = metrics(source.store.knowledge, "p")
    assert report["passed"] == 0 and report["unverified"] == 2


def test_unrelated_test_cannot_verify_experience():
    result = observe_contract(CONTRACT, [tool(True, command="python -m pytest unrelated.py")], "turn_id")
    assert result["status"] == "not_checked"
    uncertain = ToolEvent('unknown', 'exec_command', json.dumps({'cmd': COMMAND}), '', None, 'test')
    assert observe_contract(CONTRACT, [tool(True), uncertain], 'turn_id')['status'] == 'not_checked'


def test_concurrent_completion_is_idempotent(tmp_path):
    source, _, _ = seed(tmp_path)
    consumer = make_adapter(tmp_path, "codex")
    consumer.recall({"session_id": "concurrent", "turn_id": "same", "prompt": "widget Windows"})
    args = dict(agent_id="codex", project_key="p", session_id="concurrent", turn_id="same",
                turn=TranscriptTurn("widget Windows", "", (tool(True),)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consumer.reuse.complete(**args), range(2)))
    assert sum(result is not None for result in results) == 1
    assert metrics(source.store.knowledge, "p")["passed"] == 1


def test_small_context_budget_cannot_gain_verification_credit(tmp_path):
    source, _, rows = seed(tmp_path)
    record = source.store.knowledge.get(requester_agent="codex", knowledge_id=rows[0]["id"])["knowledge"]
    source.reuse.start(agent_id="codex", project_key="p", session_id="tiny", turn_id="t",
        prompt="widget Windows", records=[record], retrieval_ms=1, budget=10)
    source.reuse.complete(agent_id="codex", project_key="p", session_id="tiny", turn_id="t",
        turn=TranscriptTurn("widget Windows", "", (tool(True),)))
    assert metrics(source.store.knowledge, "p")["passed"] == 0


def test_metrics_endpoint_reports_real_experience_counters(tmp_path):
    seed(tmp_path)
    complete(make_adapter(tmp_path, "codex"))
    app = create_app(database_path=tmp_path / "db.sqlite", api_token="test-only", reviewer=reviewer)
    with TestClient(app) as client:
        response = client.post("/v1/metrics", headers={"Authorization": "Bearer test-only"},
                               json={"project_key": "p"})
    assert response.status_code == 200
    assert response.json()["experience"]["cross_agent_passed"] == 1


def test_verified_transfer_reranks_only_same_condition_group(tmp_path):
    from agent_knowledge_bridge.experiences import rank_equivalent
    source, _, first = seed(tmp_path)
    def alternative(text):
        response = reviewer(text)
        response['proposals'][0]['title'] = 'Alternative widget order'
        response['proposals'][0]['experience']['steps'][0] = 'Inspect the widget schema and preserve its ordering.'
        return response
    _, _, records = seed(tmp_path, review=alternative, agent='codex')
    first_id = first[0]['id']
    other_id = next(r['id'] for r in records if r['id'] != first_id)
    with source.store.knowledge._connect() as db:
        db.execute('UPDATE experience_outcomes SET passed=3,cross_agent_passed=2 WHERE knowledge_id=?', (other_id,))
        first_row = db.execute('SELECT * FROM knowledge_records WHERE id=?', (first_id,)).fetchone()
        other_row = db.execute('SELECT * FROM knowledge_records WHERE id=?', (other_id,)).fetchone()
        unrelated = {**dict(first_row), 'id':'plain', 'content':'Ordinary unrelated fact'}
        ordered, changes = rank_equivalent(db, [first_row, unrelated, other_row])
        assert [r['id'] for r in ordered] == [other_id, 'plain', first_id]
        assert changes == 2
        ordered, changes = rank_equivalent(db, [first_row, other_row],
                                           origins={first_id:'direct', other_id:'sibling'})
        assert [r['id'] for r in ordered] == [first_id, other_id] and changes == 0
        # Same task words but different project must not compete.
        different_project = {**dict(other_row), 'project_key':'elsewhere'}
        ordered, changes = rank_equivalent(db, [first_row, different_project])
        assert [r['id'] for r in ordered] == [first_id, other_id] and changes == 0


def test_contract_cannot_bypass_admission_through_plain_content(tmp_path):
    def disguised(text):
        value = reviewer(text)
        proposal = value['proposals'][0]
        proposal['content'] = encode_contract(proposal.pop('experience'))
        return value
    _, learned, records = seed(tmp_path, (True,), disguised)
    assert learned['proposals'] == 0 and not records


def test_invalid_reserved_contract_cannot_be_recalled():
    assert filter_rows([{'content':'{"experience":{"steps":[]}}'}], 'widget Windows') == []


def test_empty_outcome_counters_are_zero_without_invented_samples(tmp_path):
    adapter = make_adapter(tmp_path)
    assert all(value == 0 for value in metrics(adapter.store.knowledge, 'p').values())

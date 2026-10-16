"""End-to-end benchmark safety contracts; local fake HTTP, no paid APIs."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("evaluate_longmemeval_e2e", SCRIPTS / "evaluate_longmemeval_e2e.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def row():
    return {"question_id": "q", "question_type": "single-session-user",
        "question": "What is the ledger-svc retry policy?", "question_date": "2023/05/21 (Sun) 03:00",
        "answer": "SECRET_GOLD", "answer_session_ids": ["answer_leaky"],
        "haystack_session_ids": ["answer_leaky", "unrelated"],
        "haystack_dates": ["2023/05/20 (Sat) 02:00", "2023/05/20 (Sat) 03:00"],
        "haystack_sessions": [[{"role": "user", "content": "ledger-svc retry policy uses exponential backoff.", "has_answer": True}],
                              [{"role": "user", "content": "I like gardening."}]]}


def test_real_core_prepares_all_arms_without_gold_in_answer_input():
    entries = M.prepare_question((row(), 64000, 5))
    assert len(entries) == 7
    for entry in entries:
        serialized = json.dumps(entry["messages"])
        assert "SECRET_GOLD" not in serialized
        assert "answer_leaky" not in serialized
        assert "has_answer" not in serialized
        assert "2023/05/21" in serialized
        assert "kn_" not in serialized
        if entry["arm"] == "no_memory":
            assert entry["context_chars"] == 0
    groups = {(e["scenario"], e["arm"]): e for e in entries}
    assert groups[("native", "memweave")]["emitted_chunks"] > 0
    assert groups[("retired", "memweave")]["restored_chunks"] > 0
    assert groups[("retired", "no_lfhv")]["restored_chunks"] == 0
    assert groups[("retired", "fts5")]["emitted_chunks"] == 0


def test_gold_changes_do_not_change_retrieval_or_messages():
    original = M.prepare_question((row(), 64000, 5))
    changed = row()
    changed.update(answer="OTHER", answer_session_ids=["other"], haystack_session_ids=["other", "else"])
    changed["haystack_sessions"][0][0]["has_answer"] = False
    after = M.prepare_question((changed, 64000, 5))
    assert [e["messages"] for e in original] == [e["messages"] for e in after]


def test_balanced_sampling_is_deterministic_and_full_selection_keeps_all():
    rows = [{"question_id": f"q{i}" + ("_abs" if i % 2 else ""),
             "question_type": "multi-session" if i < 10 else "single-session-user"} for i in range(20)]
    first = M.select_rows(rows, 8, 42)
    assert first == M.select_rows(rows, 8, 42)
    assert len({r["question_id"] for r in first}) == 8
    assert sum(r["question_id"].endswith("_abs") for r in first) == 4
    assert M.select_rows(rows, 20, 42) == rows


@pytest.mark.parametrize("text,expected", [("yes", True), (" No. ", False), ("YES\n", True)])
def test_judge_parse(text, expected):
    assert M.parse_judgment(text) is expected


@pytest.mark.parametrize("text", ["yes or no", "not yes", "yesterday", "", "yes, because..."])
def test_ambiguous_judgment_fails(text):
    with pytest.raises(ValueError):
        M.parse_judgment(text)


def test_same_wrapper_whole_block_budget():
    records = [{"id": str(i), "date": "today", "content": "x" * 20} for i in range(3)]
    memory, kept = M.format_memory(records, ["0", "1", "2"], len(M.block(records[0])))
    assert kept == ["0"]
    assert memory == M.block(records[0])


def fixture_entries():
    return [{"question_id": "q", "scenario": scenario, "arm": arm,
             "question_type": "multi-session", "abstention": False, "retired_evidence": True,
             "retrieval_ms": 1, "context_utf8_bytes": 12} for scenario, arms in M.ARMS.items() for arm in arms]


def fixture_results(entries):
    return [{**e, "status": "ok", "correct": True, "answer": {"prompt_tokens": 5, "ms": 2}} for e in entries]


def test_failures_do_not_shrink_score_denominator():
    entries = fixture_entries()
    results = fixture_results(entries)
    results[2].update(status="judge_error", correct=None)
    report = M.aggregate(entries, results)
    assert report["groups"]["native/memweave"]["accuracy"] is None
    assert report["groups"]["native/memweave"]["correct_over_all_expected_lower_bound"] == 0
    assert report["paired"]["native/memweave_vs_fts5/all"]["accuracy_delta"] is None


def test_missing_usage_is_not_fabricated():
    entries = fixture_entries()
    results = fixture_results(entries)
    results[0]["answer"]["prompt_tokens"] = None
    assert M.aggregate(entries, results)["groups"]["native/no_memory"]["mean_answer_input_tokens"] is None


def test_prepared_coverage_rejects_duplicates_or_missing():
    manifest = {"sample_ids": ["q"], "protocol": M.PROTOCOL, "status": "prepared_not_run"}
    entries = fixture_entries()
    M.validate_prepared(manifest, entries)
    with pytest.raises(ValueError):
        M.validate_prepared(manifest, entries[:-1])
    with pytest.raises(ValueError):
        M.validate_prepared(manifest, entries + [entries[0]])


def test_live_requires_explicit_consent_before_reading_or_connecting():
    from argparse import Namespace
    with pytest.raises(ValueError, match="consent"):
        M.run(Namespace(consent_public_data=False))


def test_judge_is_blind_and_answer_reference_is_not_sent_to_answer_client():
    entry = {**fixture_entries()[0], "messages": M.answer_messages("QUESTION", "DATE", "HISTORY"),
             "question": "QUESTION", "reference": "SECRET_GOLD"}
    class Fake:
        def __init__(self, text):
            self.text, self.calls = text, []
        def complete(self, messages):
            self.calls.append(messages)
            return {"text": self.text, "prompt_tokens": 10, "ms": 1}
    answer, judge = Fake("RESPONSE"), Fake("yes")
    result = M.run_entry(entry, answer, judge)
    assert result["status"] == "ok"
    assert "SECRET_GOLD" not in json.dumps(answer.calls)
    prompt = judge.calls[0][0]["content"]
    assert "SECRET_GOLD" in prompt and "RESPONSE" in prompt
    assert "HISTORY" not in prompt and "no_memory" not in prompt and "native" not in prompt


def test_numeric_reference_is_supported_by_official_prompt():
    prompt = M.get_anscheck_prompt("temporal-reasoning", "How many days?", 18, "19 days")
    assert "Correct Answer: 18" in prompt
    assert "off-by-one" in prompt


def test_run_writes_complete_artifacts_using_only_local_fake_api(tmp_path, monkeypatch):
    from argparse import Namespace
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    entries = [{**e, "messages": M.answer_messages("Q", "DATE", "HISTORY"),
                "question": "Q", "reference": 18} for e in fixture_entries()]
    path = prepared / "prepared.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    M.write_json(prepared / "manifest.json", {"protocol": M.PROTOCOL, "status": "prepared_not_run",
        "sample_ids": ["q"], "sample_size": 1, "sample_seed": 42, "full_dataset": False,
        "source_sha256": M.source_hashes(), "prepared_sha256": M.digest(path)})
    calls = []
    budgets = []
    class Fake:
        def __init__(self, *args):
            budgets.append(args[-1])
        def complete(self, messages):
            calls.append(messages)
            return {"text": "yes", "model": "fake", "prompt_tokens": 5, "completion_tokens": 1, "ms": 2}
    monkeypatch.setattr(M, "ChatClient", Fake)
    monkeypatch.setenv("LME_TEST_KEY", "fake")
    output = tmp_path / "live"
    M.run(Namespace(consent_public_data=True, prepared=prepared, output=output, max_api_calls=14,
        answer_key_env="LME_TEST_KEY", judge_key_env="LME_TEST_KEY", answer_base_url="http://localhost/v1",
        judge_base_url="http://localhost/v1", answer_model="fake", judge_model="fake",
        timeout=5, answer_max_tokens=2048, judge_max_tokens=2048, workers=2))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "completed" and report["api_calls"] == 14
    assert report["pilot_not_final_score"]
    assert len(calls) == 14
    assert budgets == [2048, 2048]
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["generation"]["judge_max_tokens"] == 2048
    assert manifest["generation"]["answer_max_tokens"] == 2048
    assert (output / "REPORT.md").is_file()
    assert "fake" in (output / "manifest.json").read_text(encoding="utf-8")


def test_local_http_client_usage_and_truncation_without_retries():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = {"choices": [{"finish_reason": "stop" if len(requests) == 1 else "length",
                                 "message": {"content": "yes"}}],
                    "model": "fake", "usage": {"prompt_tokens": 12, "completion_tokens": 1}}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = M.ChatClient(f"http://127.0.0.1:{server.server_port}/v1", "fake", "fake-key", 5, 2048)
        result = client.complete([{"role": "user", "content": "hello"}])
        assert result["prompt_tokens"] == 12
        with pytest.raises(M.ModelOutputError, match="truncated") as error:
            client.complete([{"role": "user", "content": "hello"}])
        assert error.value.diagnostics["finish_reason"] == "length"
        assert error.value.diagnostics["completion_tokens"] == 1
        assert all(request["max_tokens"] == 2048 for request in requests)
        assert len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("url", ["https://user:secret@example.com/v1", "https://example.com/v1?key=secret", "http://example.com/v1"])
def test_unsafe_url_rejected(url):
    with pytest.raises(ValueError):
        M.ChatClient(url, "model", "key", 5, 10)


@pytest.mark.parametrize("answer_budget,judge_budget", [(512, 2048), (2048, 10), (2047, 2048)])
def test_small_output_budgets_rejected_before_api_calls(answer_budget, judge_budget):
    from argparse import Namespace
    with pytest.raises(ValueError, match="2048"):
        M.run(Namespace(consent_public_data=True, answer_max_tokens=answer_budget,
                        judge_max_tokens=judge_budget))


@pytest.mark.parametrize("phase", ["answer", "judge"])
def test_failed_output_diagnostics_are_retained_without_response_body(phase):
    entry = {**fixture_entries()[0], "messages": [], "question": "Q", "reference": "A"}
    details = M.output_diagnostics(
        {"usage": {"prompt_tokens": 12, "completion_tokens": 2048,
                   "completion_tokens_details": {"reasoning_tokens": 2048}}},
        {"finish_reason": "length", "message": {"content": None, "reasoning_content": "SECRET"}})
    class Fake:
        def __init__(self, fail):
            self.fail = fail
        def complete(self, messages):
            if self.fail:
                raise M.ModelOutputError("missing_or_truncated_output", details)
            return {"text": "yes", "ms": 1}
    result = M.run_entry(entry, Fake(phase == "answer"), Fake(phase == "judge"))
    assert result["status"] == phase + "_error"
    assert result["api_calls"] == (1 if phase == "answer" else 2)
    assert result["output_diagnostics"]["reasoning_tokens"] == 2048
    assert result["error_reason"] == "missing_or_truncated_output"
    assert "SECRET" not in json.dumps(result)


def test_diagnostics_drop_untrusted_strings_and_invalid_token_counts():
    result = M.output_diagnostics({"usage": {"prompt_tokens": "SECRET", "completion_tokens": True,
                                           "total_tokens": -1}}, {"finish_reason": "SECRET"})
    assert result["finish_reason"] == "missing_or_unknown"
    assert result["prompt_tokens"] is result["completion_tokens"] is result["total_tokens"] is None
    assert "SECRET" not in json.dumps(result)

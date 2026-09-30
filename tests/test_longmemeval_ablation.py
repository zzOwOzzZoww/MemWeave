"""Public-data import must not leak evidence labels or bias retirement."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_longmemeval_ablation.py"
SPEC = importlib.util.spec_from_file_location("evaluate_longmemeval_ablation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row():
    return {"question_id": "q", "question_type": "single-session-user", "question": "What did I choose?",
        "answer": "Raven", "haystack_session_ids": ["answer_leaky", "other"],
        "haystack_dates": ["2023/05/20 (Sat) 03:00", "2023/05/20 (Sat) 02:00"],
        "haystack_sessions": [[{"role": "user", "content": "I chose Raven.", "has_answer": True}],
                              [{"role": "user", "content": "I like Cedar."}]],
        "answer_session_ids": ["answer_leaky"]}


def prediction(aliases, emitted=True):
    return {"items": [{"evidence_id": a, "emitted": emitted} for a in aliases]}


def test_gold_and_raw_id_hints_cannot_change_index_or_retirement():
    source = row()
    baseline = MODULE.memory_records(source)
    indices = MODULE.retirement_indices(source)
    source.update(answer="different", question="different question", answer_session_ids=["renamed"],
                  haystack_session_ids=["renamed", "different"])
    source["haystack_sessions"][0][0]["has_answer"] = False
    assert MODULE.memory_records(source) == baseline
    assert MODULE.retirement_indices(source) == indices == {1}
    assert "answer_leaky" not in str(baseline)


def test_chunking_preserves_full_transcript_without_gold_alignment():
    source = row()
    source["haystack_sessions"] = [[{"role": "user", "content": "a" * 20003}]]
    source["haystack_dates"] = source["haystack_dates"][:1]
    records = MODULE.memory_records(source)
    assert len(records) == 3
    assert all(len(r["content"]) <= 8000 for r in records)
    assert "".join(r["content"] for r in records) == "user: " + "a" * 20003


def test_repeated_chunks_consume_rank_slots_but_do_not_inflate_session_recall():
    source = row()
    source["answer_session_ids"] = source["haystack_session_ids"]
    result = prediction(["s0000:c0000", "s0000:c0001", "s0001:c0000"])
    assert MODULE.score(source, result, k=2)["recall"] == .5
    assert MODULE.score(source, result, k=3)["complete"]


def test_abstention_is_excluded_from_positive_metrics():
    source = row()
    source["question_id"] += "_abs"
    result = MODULE.metrics([source], [prediction(["s0000:c0000"])])
    assert result["positive_questions"] == 0
    assert result["mean_evidence_recall"] is None
    assert result["abstention_empty_rate"] == 0


def test_no_emission_is_not_a_retrieval_hit():
    source = row()
    result = prediction(["s0000:c0000"], emitted=False)
    assert MODULE.score(source, result, emitted=False)["hit"]
    assert not MODULE.score(source, result)["hit"]


@pytest.mark.parametrize("change", [{"answer_session_ids": ["missing"]}, {"question": "x" * 501},
                                   {"haystack_dates": []}])
def test_invalid_public_rows_fail_loudly(change):
    source = row()
    source.update(change)
    with pytest.raises(ValueError):
        MODULE.validate_rows([source])


def test_duplicate_questions_fail_loudly():
    with pytest.raises(ValueError):
        MODULE.validate_rows([row(), row()])


def test_official_duplicate_session_ids_preserve_both_history_positions():
    source = row()
    source["haystack_session_ids"] = ["answer_leaky", "answer_leaky"]
    MODULE.validate_rows([source])
    assert len(MODULE.memory_records(source)) == 2
    assert MODULE.score(source, prediction(["s0001:c0000"]))["recall"] == 1


def test_lifecycle_replay_uses_real_store_and_isolated_snapshots(tmp_path):
    source = row()
    records = MODULE.memory_records(source)
    seed, archived = tmp_path / "seed.db", tmp_path / "archived.db"
    aliases = MODULE.seed_history(records, seed, "lme-test")
    MODULE.clone_database(seed, archived)
    MODULE.archive_history(records, aliases, MODULE.retirement_indices(source), archived)
    with MODULE.KnowledgeStore(seed)._connect() as db:
        assert {r[0] for r in db.execute("SELECT status FROM knowledge_records")} == {"active"}
    with MODULE.KnowledgeStore(archived)._connect() as db:
        status = {r["id"]: r["status"] for r in db.execute("SELECT id,status FROM knowledge_records")}
        assert status[aliases["s0001:c0000"]] == "archived"
        assert status[aliases["s0000:c0000"]] == "active"


def recovery_module():
    spec = importlib.util.spec_from_file_location("summarize_longmemeval_ablation", SCRIPT.with_name("summarize_longmemeval_ablation.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recovery_predictions():
    return [{"case_id": "q", "variant": arm, "repeat": repeat,
        "question_type": row()["question_type"], "gold_session_ids": row()["answer_session_ids"],
        "emitted_ids": ["s0000:c0000"], "restored_ids": [], "items": [],
        "context_chars": 100, "context_utf8_bytes": 100, "recall_ms": repeat + 1.0}
        for arm in MODULE.ARMS for repeat in range(2)]


def test_recovery_checks_complete_coverage_and_semantics_not_timing(tmp_path):
    path = tmp_path / "predictions.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recovery_predictions()), encoding="utf-8")
    first, timings, signature = recovery_module().load_predictions([row()], path)
    assert set(first) == set(MODULE.ARMS)
    assert all(t == [1.0, 2.0] for t in timings.values())
    assert len(signature) == 64


@pytest.mark.parametrize("damage", ["missing", "duplicate", "gold", "semantic"])
def test_recovery_rejects_incomplete_or_inconsistent_runs(tmp_path, damage):
    results = recovery_predictions()
    if damage == "missing":
        results.pop()
    elif damage == "duplicate":
        results.append(results[0])
    elif damage == "gold":
        results[0]["gold_session_ids"] = ["wrong"]
    else:
        results[0]["context_chars"] += 1
    path = tmp_path / "predictions.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in results), encoding="utf-8")
    with pytest.raises(ValueError):
        recovery_module().load_predictions([row()], path)


@pytest.mark.parametrize("workers", [0, -1, 33])
def test_invalid_workers_fail_before_running(workers):
    with pytest.raises(ValueError, match="workers"):
        list(MODULE.evaluate_questions([row()], repeats=2, workers=workers))


@pytest.mark.parametrize("workers", [1, 2])
def test_expired_timeout_does_not_start_questions(workers):
    with pytest.raises(TimeoutError):
        list(MODULE.evaluate_questions([row()], repeats=2, workers=workers, timeout=0))


def test_spawned_workers_preserve_serial_semantics_and_case_order(tmp_path):
    first = row()
    first["question"] = "What is the ledger-svc retry policy?"
    first["haystack_sessions"][1][0]["content"] = "ledger-svc retry policy uses exponential backoff."
    second = row()
    second["question_id"] = "q2"
    second["question"] = "What is the vault-svc retry policy?"
    second["haystack_sessions"][1][0]["content"] = "vault-svc retry policy uses exponential backoff."
    fixture = tmp_path / "rows.json"
    fixture.write_text(json.dumps([first, second]), encoding="utf-8")
    code = """
import json
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from evaluate_longmemeval_ablation import evaluate_questions, semantic_result
if __name__ == '__main__':
    rows = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    serial = list(evaluate_questions(rows, repeats=2, workers=1))
    parallel = list(evaluate_questions(rows, repeats=2, workers=2))
    for expected, actual in zip(serial, parallel):
        assert expected['index'] == actual['index']
        assert expected['imported_chunks'] == actual['imported_chunks']
        assert expected['retired_evidence_positive'] == actual['retired_evidence_positive']
        assert len(actual['predictions']) == 12
        assert [semantic_result(p) for p in expected['predictions']] == [semantic_result(p) for p in actual['predictions']]
        assert any(p['emitted_ids'] for p in actual['predictions'])
        assert any(p['restored_ids'] for p in actual['predictions'])
    assert [r['index'] for r in parallel] == [0, 1]
"""
    result = subprocess.run([sys.executable, "-c", code, str(SCRIPT.parent), str(fixture)],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr

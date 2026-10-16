"""Evaluate the new harness contracts without inspecting held-out engine scores."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "evaluation" / "memweave-cross-agent-v2-holdout"
sys.path.insert(0, str(HERE))


def load(name):
    spec = importlib.util.spec_from_file_location("holdout_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("build_dataset")
EVAL = load("evaluate")


def fixture(positive=True):
    return {"case_id": "unit-fixture", "kernel_id": "unit-topic", "category": "direct_fact" if positive else "insufficient_detail",
            "target_agent": "codex", "project_context": "unit-project",
            "query": "What is the unit-service deployment region?",
            "memory_records": [{"id": "fact", "source_agent": "claude-code", "scope": "project",
                                "project_key": "unit-project", "status": "active", "source_session": "unit-source",
                                "content": "unit-service deployment region is eu-west-1."},
                               {"id": "noise", "source_agent": "claude-code", "scope": "project",
                                "project_key": "unit-project", "status": "active", "source_session": "unit-noise",
                                "content": "unit-other deployment region is ap-east-1."}],
            "expected": {"decision": "inject" if positive else "abstain", "evidence_ids": ["fact"] if positive else [],
                         "forbidden_ids": ["noise"] if positive else ["fact", "noise"],
                         "topical_ids": ["fact"], "shadow_probe": "none"},
            "forbidden_error_kind": "wrong_subject" if positive else "missing_relation"}


def prediction(emitted=(), candidates=(), returned=(), restored=()):
    return {"case_id": "unit-fixture", "emitted_ids": list(emitted), "candidate_ids": list(candidates),
            "returned_ids": list(returned), "restored_ids": list(restored)}


def test_build_is_deterministic_and_topic_grouped():
    rows = BUILD.build_cases()
    assert BUILD.canonical(rows) == BUILD.canonical(BUILD.build_cases())
    assert BUILD.validate(rows)["splits"] == {"dev": 30, "test": 90}
    split_groups = {split: {r["kernel_id"] for r in rows if r["split"] == split} for split in ("dev", "test")}
    assert len(split_groups["dev"]) == 5 and len(split_groups["test"]) == 15
    assert not split_groups["dev"] & split_groups["test"]
    card = json.loads((HERE / "dataset-card.json").read_text(encoding="utf-8"))
    assert EVAL.digest(HERE / "cases.jsonl") == card["dataset_sha256"]


@pytest.mark.parametrize("mutation", ["split", "duplicate_id", "unknown_gold", "contradictory_fact", "past_validity"])
def test_invalid_dataset_is_rejected(mutation):
    rows = BUILD.build_cases()
    if mutation == "split":
        rows[0]["split"], rows[6]["split"] = rows[6]["split"], rows[0]["split"]
    elif mutation == "duplicate_id":
        rows[1]["case_id"] = rows[0]["case_id"]
    elif mutation == "unknown_gold":
        rows[0]["expected"]["evidence_ids"] = ["missing"]
    elif mutation == "contradictory_fact":
        rows[0]["memory_records"][0]["fact"]["subject"] = "wrong-subject"
    else:
        rows[0]["request"]["as_of"] = "2026-08-01"
    with pytest.raises(ValueError):
        BUILD.validate(rows)


def test_prior_exact_content_overlap_is_rejected():
    rows = BUILD.build_cases()
    with pytest.raises(ValueError, match="overlap"):
        BUILD.validate(rows, {rows[0]["memory_records"][0]["content"].casefold()})


def test_no_known_fixture_topic_or_content_is_reused():
    rows = BUILD.build_cases()
    previous = [json.loads(line) for line in (HERE.parent / "memweave-cross-agent-v1" / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    prior = {r["content"].casefold() for c in previous for r in c["memory_records"]}
    assert BUILD.validate(rows, prior)["valid"]


def test_related_candidate_does_not_count_as_injection_or_support():
    scored = EVAL.score_case(fixture(False), prediction(candidates=["fact"]))
    assert scored["decision_correct"] and scored["complete_clean"]
    assert scored["insufficient_related_candidate"] and not scored["forbidden_injected"]
    assert scored["evidence_recall"] is None
    failed = EVAL.score_case(fixture(False), prediction(emitted=["fact"], candidates=["fact"], returned=["fact"]))
    assert not failed["decision_correct"] and failed["forbidden_injected"]
    assert failed["error_kind"] == "missing_relation"


def test_nonempty_output_is_not_complete_clean_when_wrong_evidence_is_emitted():
    row = EVAL.score_case(fixture(), prediction(emitted=["noise"], candidates=["noise"], returned=["noise"]))
    assert row["decision_correct"] and row["evidence_recall"] == 0
    assert not row["complete_clean"] and row["forbidden_injected"]


@pytest.mark.parametrize("field,values", [("emitted_ids", ["unknown"]), ("emitted_ids", ["fact", "fact"]),
                                        ("candidate_ids", ["unknown"]), ("returned_ids", ["unknown"]),
                                        ("restored_ids", ["fact"])])
def test_invalid_prediction_is_rejected(field, values):
    pred = prediction()
    pred[field] = values
    with pytest.raises(ValueError):
        EVAL.score_case(fixture(), pred)


def test_missing_and_duplicate_predictions_are_not_successful_abstentions():
    case = fixture(False)
    with pytest.raises(ValueError, match="Missing"):
        EVAL.summarize([case], [])
    with pytest.raises(ValueError, match="Duplicate"):
        EVAL.summarize([case], [prediction(), prediction()])


def test_semantic_repeat_check_ignores_nested_timing_only():
    a = {**prediction(), "recall_ms": 1.0, "stages": [{"stage": "bridge", "elapsed_ms": 1.0, "added_count": 0}], "repeat": 0}
    b = deepcopy(a)
    b["recall_ms"] = 23.0
    b["repeat"] = 1
    b["stages"][0]["elapsed_ms"] = 7.0
    assert EVAL.semantic(a) == EVAL.semantic(b)
    b["stages"][0]["added_count"] = 1
    assert EVAL.semantic(a) != EVAL.semantic(b)


def test_freeze_detects_source_and_environment_drift(monkeypatch):
    monkeypatch.setattr(EVAL, "fingerprints", lambda: {"file": "fixed"})
    monkeypatch.setattr(EVAL, "environment", lambda: {"python": "fixed"})
    EVAL.check_freeze({"hashes": {"file": "fixed"}, "environment": {"python": "fixed"}})
    with pytest.raises(ValueError, match="changed"):
        EVAL.check_freeze({"hashes": {"file": "changed"}, "environment": {"python": "fixed"}})


def test_observation_wrapper_preserves_adapter_semantics(tmp_path):
    from evaluate_retrieval_ablation import clone_database, run_arm, seed_case, semantic_result
    case = fixture()
    seed = tmp_path / "seed.db"
    project, aliases = seed_case(case, seed)
    plain = tmp_path / "plain.db"
    observed = tmp_path / "observed.db"
    clone_database(seed, plain)
    clone_database(seed, observed)
    before = run_arm(case, plain, project, aliases, "full")
    after = EVAL.run_case(case, observed, project, aliases, {"variant": "full", "limit": 3, "budget_chars": 4000})
    assert semantic_result(before) == semantic_result(after)
    assert "fact" in after["candidate_ids"]
    assert after["diagnostics"]

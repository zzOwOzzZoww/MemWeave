"""Ablation intervention and fixed-label scoring contracts."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_retrieval_ablation.py"
SPEC = importlib.util.spec_from_file_location("evaluate_retrieval_ablation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def case(category="lfhv_shadow_recovery", status="archived"):
    return {"case_id": "test-demand", "category": category, "project_context": "p",
        "target_agent": "codex", "query": "What is the ledger-svc retry policy?",
        "memory_records": [{"id": "memory", "source_agent": "claude-code", "scope": "project",
            "project_key": "p", "status": status, "source_session": "fixture",
            "title": "ledger-svc retry policy", "content": "ledger-svc retry policy uses exponential backoff."}],
        "expected": {"decision": "abstain", "evidence_ids": [], "forbidden_ids": ["memory"],
                     "shadow_probe": "shadow_candidate"}}


def prediction(emitted=(), restored=()):
    return {"emitted_ids": list(emitted), "restored_ids": list(restored)}


def test_lfhv_off_does_not_change_gold_to_abstention():
    row = MODULE.score_case(case(), prediction())
    assert row["positive"] and not row["decision_correct"]
    assert row["evidence_recall"] == 0 and row["recovery_complete"] is False
    assert MODULE.score_case(case(), prediction(["memory"], ["memory"]))["complete_clean"]


@pytest.mark.parametrize("output", [prediction(["unknown"]), prediction([], ["memory"]),
                                    prediction(["memory", "memory"])])
def test_invalid_predictions_are_not_scored(output):
    with pytest.raises(ValueError):
        MODULE.score_case(case(), output)


def test_restoration_requires_actual_emission_under_budget(tmp_path):
    data = case()
    seed = tmp_path / "seed.db"
    project, aliases = MODULE.seed_case(data, seed)
    results = {}
    for variant, budget in (("full", 4000), ("no_lfhv", 4000), ("tiny", 1)):
        database = tmp_path / (variant + ".db")
        MODULE.clone_database(seed, database)
        results[variant] = MODULE.run_arm(data, database, project, aliases,
            "full" if variant == "tiny" else variant, budget=budget)
    assert results["full"]["emitted_ids"] == results["full"]["restored_ids"] == ["memory"]
    assert results["no_lfhv"]["emitted_ids"] == results["no_lfhv"]["restored_ids"] == []
    assert results["tiny"]["emitted_ids"] == results["tiny"]["restored_ids"] == []
    with MODULE.closing(MODULE.sqlite3.connect(seed)) as db:
        assert db.execute("SELECT status FROM knowledge_records").fetchone()[0] == "archived"


@pytest.mark.parametrize("variant,disabled", [("no_bridge", "bridge"), ("no_sibling", "sibling"),
                                             ("no_anchor", "anchor")])
def test_disabled_stage_is_not_executed_and_environment_is_restored(tmp_path, monkeypatch, variant, disabled):
    monkeypatch.setenv("MW_RECALL_LIMIT", "19")
    data = case(category="control", status="active")
    project, aliases = MODULE.seed_case(data, tmp_path / "db.sqlite")
    result = MODULE.run_arm(data, tmp_path / "db.sqlite", project, aliases, variant)
    assert result["emitted_ids"] == ["memory"]
    assert next(r for r in result["stages"] if r["stage"] == disabled)["skipped_reason"] == "disabled"
    assert MODULE.os.environ["MW_RECALL_LIMIT"] == "19"


def test_diagnostic_labels_are_valid_and_frozen():
    rows = MODULE.read_cases(MODULE.DIAGNOSTICS)
    assert len(rows) == 12
    assert {"bridge", "sibling", "anchor", "lfhv_shadow_recovery"} <= {r["category"] for r in rows}


def test_paired_comparison_counts_recall_loss_not_merely_decision():
    data = case()
    full = prediction(["memory"], ["memory"])
    removed = prediction()
    assert MODULE.paired_changes([data], [full], [removed])["recall_harmed_cases"] == 1


def test_allowed_supporting_records_do_not_replace_required_evidence():
    data = MODULE.read_cases(MODULE.DIAGNOSTICS)[0]
    assert MODULE.score_case(data, prediction(["mapping-a", "mapping-b"]))["evidence_recall"] == 0
    row = MODULE.score_case(data, prediction(["mapping-a", "mapping-b", "target"]))
    assert row["complete_clean"] and row["unexpected_records"] == 0


@pytest.mark.parametrize("case_id,variant", [
    ("ABD-bridge-gap", "no_bridge"), ("ABD-sibling-gap", "no_sibling"),
    ("ABD-anchor-page", "no_anchor"), ("ABD-lfhv-demand", "no_lfhv"),
])
def test_real_fixture_intervention_changes_required_evidence(tmp_path, case_id, variant):
    data = next(c for c in MODULE.read_cases(MODULE.DIAGNOSTICS) if c["case_id"] == case_id)
    seed = tmp_path / "seed.db"
    project, aliases = MODULE.seed_case(data, seed)
    scored = {}
    for arm in ("full", variant):
        database = tmp_path / (arm + ".db")
        MODULE.clone_database(seed, database)
        result = MODULE.run_arm(data, database, project, aliases, arm)
        scored[arm] = MODULE.score_case(data, result)
    assert scored["full"]["evidence_recall"] == 1
    assert scored[variant]["evidence_recall"] < 1

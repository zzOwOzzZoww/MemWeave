"""Validate the released JSONL benchmark and print its digest and balance."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = ROOT / "cases.jsonl"
EXPECTED_CATEGORIES = {
    "cross_agent_recall", "cross_language_recall", "distractor_ranking",
    "project_scope_isolation", "knowledge_update", "same_session_noise_filter",
    "answerability_abstention", "near_miss_abstention", "lfhv_shadow_recovery",
    "lifecycle_exclusion",
}


def main() -> None:
    raw = PATH.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    assert len(rows) == 500, f"expected 500 cases, found {len(rows)}"
    ids = [row["case_id"] for row in rows]
    assert len(ids) == len(set(ids)), "duplicate case_id"
    queries = [row["query"] for row in rows]
    assert len(queries) == len(set(queries)), "duplicate query"
    assert all(row.get("synthetic") is True for row in rows), "non-synthetic row"
    assert {row["category"] for row in rows} == EXPECTED_CATEGORIES
    assert Counter(row["category"] for row in rows) == Counter({name: 50 for name in EXPECTED_CATEGORIES})
    assert Counter(row["split"] for row in rows) == {"calibration": 100, "dev": 100, "test": 300}
    kernels: dict[str, set[str]] = defaultdict(set)
    record_ids: set[str] = set()
    for row in rows:
        assert row["source_agent"] != row["target_agent"]
        kernels[row["kernel_id"]].add(row["split"])
        current_ids = {record["id"] for record in row["memory_records"]}
        assert not (current_ids & record_ids), "memory record IDs must be globally unique"
        record_ids.update(current_ids)
        expected = row["expected"]
        assert set(expected["evidence_ids"]).issubset(current_ids)
        assert set(expected["forbidden_ids"]).issubset(current_ids)
        assert not (set(expected["evidence_ids"]) & set(expected["forbidden_ids"]))
        if expected["decision"] == "inject":
            assert expected["evidence_ids"]
        else:
            assert not expected["evidence_ids"]
        if row["category"] == "lfhv_shadow_recovery":
            assert expected["decision"] == "abstain" and expected["shadow_probe"] == "shadow_candidate"
        if row["category"] == "lifecycle_exclusion":
            assert expected["shadow_probe"] == "must_not_recover"
    assert len(kernels) == 50
    assert all(len(splits) == 1 for splits in kernels.values()), "kernel leakage across splits"
    category_split = {split: dict(Counter(r["category"] for r in rows if r["split"] == split)) for split in ("calibration", "dev", "test")}
    print(json.dumps({"valid": True, "cases": len(rows), "kernels": len(kernels),
                      "split_counts": dict(Counter(r["split"] for r in rows)),
                      "category_counts": dict(Counter(r["category"] for r in rows)),
                      "category_split_counts": category_split,
                      "sha256": hashlib.sha256(raw).hexdigest()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Run the local answer-sufficiency contract set without model calls."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.store import KnowledgeStore


def seed(store: KnowledgeStore, records: list[dict]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for record in records:
        result = store.publish(
            source_agent="answerability-evaluator",
            project_key="answerability-fixture",
            title=record["title"],
            content=record["content"],
            knowledge_type="fact",
            scope="project",
            evidence_summary="Hand-labelled answerability fixture",
            source_session=record.get("source_session"),
            search_terms=record.get("search_terms"),
            subject_terms=record.get("subject_terms"),
        )
        knowledge_id = result["knowledge"]["id"]
        store.feedback(
            agent_id="answerability-evaluator",
            knowledge_id=knowledge_id,
            outcome="verified",
            evidence_kind="test",
            evidence_ref=f"answerability:{record['alias']}",
            evidence_summary="Fixture is intentionally labelled for this evaluator",
        )
        ids[record["alias"]] = knowledge_id
    return ids


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(len(values) * fraction + 0.999) - 1))
    return round(values[index], 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=ROOT / "evaluation" / "answerability_cases.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memweave-answerability-") as folder:
        store = KnowledgeStore(Path(folder) / "answerability.db")
        ids = seed(store, fixture["knowledge"])
        aliases_by_id = {value: key for key, value in ids.items()}
        rows: list[dict] = []
        for case in fixture["cases"]:
            started = time.perf_counter()
            result = store.search(
                requester_agent="answerability-evaluator",
                project_key="answerability-fixture",
                query=case["query"],
                limit=5,
                expand_siblings=False,
            )
            elapsed = (time.perf_counter() - started) * 1000
            returned = [aliases_by_id[item["id"]] for item in result["results"] if item["id"] in aliases_by_id]
            expected = set(case["expected"])
            omitted = result.get("retrieval_diagnostics", {}).get("omitted", [])
            rows.append({
                **case,
                "returned": returned,
                "expected_found": sorted(expected.intersection(returned)),
                "extra_returned": sorted(set(returned) - expected),
                "answerable_hit": bool(expected.intersection(returned)),
                "clean_abstention": not returned if case["decision"] == "abstain" else None,
                "diagnostic_reasons": sorted({item.get("reason") for item in omitted if item.get("reason")}),
                "retrieval_ms": round(elapsed, 3),
            })

    answerable = [row for row in rows if row["decision"] == "answerable"]
    abstain = [row for row in rows if row["decision"] == "abstain"]
    unresolved = [row for row in rows if row["decision"] == "unresolved"]
    report = {
        "benchmark": fixture["version"],
        "cases": len(rows),
        "answerable_cases": len(answerable),
        "abstention_cases": len(abstain),
        "unresolved_cases": len(unresolved),
        "answerable_hit_rate": round(sum(row["answerable_hit"] for row in answerable) / len(answerable), 4) if answerable else None,
        "answerable_noise_case_rate": round(sum(bool(row["extra_returned"]) for row in answerable) / len(answerable), 4) if answerable else None,
        "answerable_mean_extra_records": round(sum(len(row["extra_returned"]) for row in answerable) / len(answerable), 3) if answerable else None,
        "abstention_clean_rate": round(sum(row["clean_abstention"] for row in abstain) / len(abstain), 4) if abstain else None,
        "unresolved_nonempty_rate": round(sum(bool(row["returned"]) for row in unresolved) / len(unresolved), 4) if unresolved else None,
        "p50_ms": percentile([row["retrieval_ms"] for row in rows], 0.50),
        "p95_ms": percentile([row["retrieval_ms"] for row in rows], 0.95),
        "cases": rows,
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the benchmark through the local MemWeave retrieval adapter (no model calls)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "src"))

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from evaluation_profile import PROFILES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["calibration", "dev", "test", "all"], default="test")
    parser.add_argument('--profile', choices=PROFILES, default='shadow-v1',
                        help='Freeze v1 shadow semantics or measure same-turn restoration separately.')
    args = parser.parse_args()
    os.environ['MW_LFHV_PROBE'] = '1'
    os.environ['MW_LFHV_RECOVERY'] = '1' if args.profile == 'on-demand-v2' else '0'
    args.output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.split != "all":
        rows = [row for row in rows if row["split"] == args.split]
    predictions: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="memweave-benchmark-") as temp:
        aliases: dict[str, dict[str, str]] = {}
        for case in rows:
            # User-scope fixtures must not leak between otherwise isolated cases.
            database = Path(temp) / (case['case_id'] + '.db')
            case_project = "bench-" + case["case_id"].lower().replace("_", "-")
            aliases[case["case_id"]] = {}
            writer = ClaudeLearningAdapter(database_path=database, agent_id=case["target_agent"],
                                           project_key=case_project, reviewer=lambda _: {"proposals": []})
            superseded: list[tuple[str, str]] = []
            for record in case["memory_records"]:
                record_project = case_project if record["project_key"] == case["project_context"] else case_project + "-other"
                result = writer.store.knowledge.publish(
                    source_agent=record["source_agent"], project_key=record_project,
                    title=record.get('title') or record['content'].splitlines()[0][:160], content=record["content"],
                    knowledge_type="procedure", scope=record["scope"],
                    evidence_summary="Synthetic benchmark fixture; not production evidence",
                    source_session=record["source_session"],
                )
                knowledge_id = result["knowledge"]["id"]
                aliases[case["case_id"]][record["id"]] = knowledge_id
                if record["status"] in {"active", "stale", "archived"}:
                    writer.store.knowledge.feedback(
                        agent_id="benchmark-fixture", knowledge_id=knowledge_id,
                        outcome="verified", evidence_kind="test", evidence_ref="fixture:benchmark-setup",
                        evidence_summary="Explicit synthetic fixture activation", project_key=record_project,
                    )
                if record["status"] in {'archived', 'stale'}:
                    writer.store.knowledge.transit(
                        knowledge_id, to_status=record['status'], reason="Synthetic benchmark lifecycle state",
                        actor="benchmark-fixture", expected_status="active",
                    )
                elif record["status"] == "quarantined":
                    writer.store.knowledge.feedback(
                        agent_id="benchmark-fixture", knowledge_id=knowledge_id,
                        outcome="rejected", evidence_kind="test", evidence_ref="fixture:benchmark-quarantine",
                        evidence_summary="Synthetic benchmark quarantine state", project_key=record_project,
                    )
                if record.get("superseded_by"):
                    superseded.append((knowledge_id, record["superseded_by"]))
            for old_id, newer_alias in superseded:
                # A missing newer alias represents an opaque historical
                # tombstone: the replacement may since have been removed.
                new_id = aliases[case["case_id"]].get(newer_alias, "tombstone:" + newer_alias)
                # Fixture setup records the resulting state after an explicit
                # version replacement; it never mutates a user database.
                with writer.store.knowledge._connect() as db:
                    db.execute("UPDATE knowledge_records SET status='archived', superseded_by=? WHERE id=?", (new_id, old_id))

            adapter_type = CodexLearningAdapter if case['target_agent'] == 'codex' else ClaudeLearningAdapter
            adapter = adapter_type(database_path=database, agent_id=case["target_agent"],
                                            project_key=case_project, reviewer=lambda _: {"proposals": []})
            started = time.perf_counter()
            adapter.recall({"session_id": "benchmark", "turn_id": case["case_id"],
                            "prompt": case["query"], "cwd": "synthetic://" + case_project})
            adapter_ms = (time.perf_counter() - started) * 1000
            trace = adapter.reuse.list(case_project, limit=1)[0]
            emitted_actual = {item["knowledge_id"] for item in trace["items"] if item["emitted"]}
            emitted_aliases = [alias for alias, kid in aliases[case["case_id"]].items() if kid in emitted_actual]
            shadow_aliases: list[str] = []
            if adapter._governor is not None:
                shadow_ids = {item["knowledge_id"] for item in adapter._governor.lfhv_report(project_key=case_project)["false_kills"]}
                shadow_aliases = [alias for alias, kid in aliases[case["case_id"]].items() if kid in shadow_ids]
            restored_actual = {item['knowledge_id'] for item in trace['items']
                               if item.get('lfhv_recovery_outcome') == 'restored' and item['emitted']}
            predictions.append({
                "case_id": case["case_id"],
                'profile': args.profile,
                "emitted_evidence_ids": emitted_aliases,
                "shadow_hit_ids": shadow_aliases,
                'restored_evidence_ids': [alias for alias, kid in aliases[case['case_id']].items() if kid in restored_actual],
                'omitted': [{'evidence_id': alias, 'reason': item['omitted_reason']}
                            for item in trace['items'] if not item['emitted']
                            for alias, kid in aliases[case['case_id']].items() if kid == item['knowledge_id']],
                "adapter_ms": round(adapter_ms, 4),
            })
    prediction_path = args.output / "predictions.jsonl"
    prediction_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), encoding="utf-8")
    # Convert system-local IDs back to stable dataset aliases for the scorer.
    report = {"dataset": "mw-cross-agent-v1.0.0-candidate", "split": args.split,
              'profile': args.profile, 'runner_version': 'isolated-fixtures-v2',
              "cases": len(rows), "predictions": str(prediction_path),
              "note": "Retrieval-only local adapter run; no LLM calls. No downstream answer correctness or task success is measured."}
    (args.output / "run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

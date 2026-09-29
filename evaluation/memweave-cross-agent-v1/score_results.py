"""Score evidence-ID predictions without model calls or subjective LLM grading."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from math import ceil
from pathlib import Path
from evaluation_profile import PROFILES, expected_for

ROOT = Path(__file__).resolve().parent


def ratio(n: int, d: int):
    return None if not d else round(n / d, 6)


def percentile(values: list[float], p: float):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, ceil(p * len(ordered)) - 1)], 4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["calibration", "dev", "test", "all"], default="all")
    parser.add_argument('--profile', choices=PROFILES, default='shadow-v1')
    args = parser.parse_args()
    gold = {row["case_id"]: row for row in (json.loads(line) for line in (ROOT / "cases.jsonl").read_text(encoding="utf-8").splitlines())}
    predictions = {}
    for line in args.predictions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row["case_id"]
        if case_id not in gold:
            raise ValueError(f"unknown case_id: {case_id}")
        if case_id in predictions:
            raise ValueError(f"duplicate prediction: {case_id}")
        if row.get('profile', 'shadow-v1') != args.profile:
            raise ValueError('prediction and scoring profiles differ')
        known = {record['id'] for record in gold[case_id]['memory_records']}
        for field in ('emitted_evidence_ids', 'shadow_hit_ids', 'restored_evidence_ids'):
            ids = row.get(field, [])
            if (not isinstance(ids, list) or any(not isinstance(key, str) for key in ids)
                    or not set(ids) <= known or len(set(ids)) != len(ids)):
                raise ValueError(f'invalid or unknown evidence IDs in {case_id}: {field}')
        if not set(row.get('restored_evidence_ids', [])) <= set(row.get('emitted_evidence_ids', [])):
            raise ValueError('restoration credit requires same-turn emission')
        predictions[case_id] = row
    if args.split != "all":
        selected = {case_id for case_id, case in gold.items() if case["split"] == args.split}
        outside = set(predictions) - selected
        if outside:
            raise ValueError(f"predictions contain cases outside split {args.split}: {sorted(outside)[:3]}")
        gold = {case_id: case for case_id, case in gold.items() if case["split"] == args.split}
    grouped = defaultdict(list)
    for case_id, case in gold.items():
        pred = predictions.get(case_id, {})
        emitted = set(pred.get("emitted_evidence_ids", []))
        shadow = set(pred.get("shadow_hit_ids", []))
        expected = expected_for(case, args.profile)
        positive = expected["decision"] == "inject"
        # A missing system output is not a successful abstention.
        correct_decision = case_id in predictions and bool(emitted) == positive
        evidence_recall = (len(emitted & set(expected["evidence_ids"])) / len(expected["evidence_ids"])) if positive else None
        detail = {
            "case_id": case_id,
            "decision_correct": correct_decision,
            "evidence_recall": evidence_recall,
            "forbidden_injected": bool(emitted & set(expected["forbidden_ids"])),
            "shadow_candidate_found": bool(shadow & set(expected["forbidden_ids"])) if expected["shadow_probe"] == "shadow_candidate" else None,
            'same_turn_recovery': bool(set(expected.get('restored_ids', [])) <=
                (set(pred.get('restored_evidence_ids', [])) & emitted)) if expected.get('restored_ids') else None,
            # This field must be supplied by deterministic checks or a blinded
            # human grader; the benchmark does not ask the evaluated model to grade itself.
            "answer_correct": pred.get("answer_correct") if isinstance(pred.get("answer_correct"), bool) else None,
            "adapter_ms": pred.get("adapter_ms"),
        }
        grouped[case["category"]].append(detail)
    def summarize(items):
        positives = [i for i in items if i["evidence_recall"] is not None]
        negatives = [i for i in items if i["evidence_recall"] is None]
        shadow_cases = [i for i in items if i["shadow_candidate_found"] is not None]
        recovery_cases = [i for i in items if i['same_turn_recovery'] is not None]
        graded_answers = [i for i in items if i["answer_correct"] is not None]
        return {
            "cases": len(items),
            "decision_accuracy": ratio(sum(i["decision_correct"] for i in items), len(items)),
            "positive_evidence_recall": ratio(sum(i["evidence_recall"] or 0 for i in positives), len(positives)),
            "abstention_clean_rate": ratio(sum(i["decision_correct"] for i in negatives), len(negatives)),
            "forbidden_injection_rate": ratio(sum(i["forbidden_injected"] for i in items), len(items)),
            "lfhv_shadow_candidate_recall": ratio(sum(bool(i["shadow_candidate_found"]) for i in shadow_cases), len(shadow_cases)),
            'lfhv_same_turn_recovery_rate': ratio(sum(i['same_turn_recovery'] for i in recovery_cases), len(recovery_cases)),
            "answer_accuracy_blind_or_deterministic": ratio(sum(i["answer_correct"] for i in graded_answers), len(graded_answers)),
            "answer_grade_coverage": ratio(len(graded_answers), len(items)),
            "adapter_p50_ms": percentile([float(i["adapter_ms"]) for i in items if i["adapter_ms"] is not None], 0.50),
            "adapter_p95_ms": percentile([float(i["adapter_ms"]) for i in items if i["adapter_ms"] is not None], 0.95),
        }
    report = {"dataset_version": "mw-cross-agent-v1.0.0-candidate", "split": args.split,
              'profile': args.profile,
              "predictions_received": len(predictions),
              "missing_predictions": len(gold) - len(predictions), "overall": summarize([d for group in grouped.values() for d in group]),
              "by_category": {name: summarize(items) for name, items in sorted(grouped.items())}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

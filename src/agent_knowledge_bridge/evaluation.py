"""Gold-labelled retrieval metrics and paired task-outcome scoring.

These functions score supplied observations; they never manufacture LLM outcomes.
"""
from collections import defaultdict
from agent_knowledge_bridge.reuse import percentile


def retrieval_metrics(cases, k=3):
    if k < 1:
        raise ValueError("k must be positive")
    positive, negative = [], []
    for case in cases:
        expected = set(case["expected_ids"])
        retrieved = list(dict.fromkeys(case["retrieved_ids"]))[:k]
        emitted = set(case["emitted_ids"])
        if expected:
            correct = len(expected.intersection(retrieved))
            positive.append({"precision": correct / k, "recall": correct / len(expected),
                             "mrr": next((1 / rank for rank, item in enumerate(retrieved, 1)
                                          if item in expected), 0)})
        else:
            negative.append(bool(emitted))
    mean = lambda values: sum(values) / len(values) if values else None
    return {"k": k, "cases": len(cases), "positive_cases": len(positive),
            "negative_cases": len(negative),
            "precision_at_k": mean([p["precision"] for p in positive]),
            "recall_at_k": mean([p["recall"] for p in positive]),
            "mrr_at_k": mean([p["mrr"] for p in positive]),
            "negative_injection_rate": mean(negative),
            "retrieval_p50_ms": percentile([c["retrieval_ms"] for c in cases], .5),
            "retrieval_p95_ms": percentile([c["retrieval_ms"] for c in cases], .95),
            "adapter_p50_ms": percentile([c["adapter_ms"] for c in cases], .5),
            "adapter_p95_ms": percentile([c["adapter_ms"] for c in cases], .95),
            "ttft_ms": None}


def paired_metrics(rows):
    """Only complete, comparable triples enter success delta denominators.

Rows require fixed model, dataset/knowledge snapshot, task and repetition.
Timing and token counters remain nullable. An evidence reference is mandatory
for reported success/failure, but this scorer does not audit external files.
"""
    groups = defaultdict(dict)
    signatures = set()
    for row in rows:
        if row["mode"] not in {"baseline", "auto", "oracle"}:
            raise ValueError("unknown evaluation mode")
        signatures.add((row["model"], row["snapshot"], row["dataset_version"]))
        key = (row["task_id"], row["repeat"])
        if row["mode"] in groups[key]:
            raise ValueError("duplicate task/mode/repeat")
        if row.get("success") is not None:
            if type(row["success"]) is not bool or not row.get("evidence_ref"):
                raise ValueError("task outcomes require boolean success and evidence_ref")
        for metric in ("ttft_ms", "total_ms", "input_tokens", "output_tokens"):
            if row.get(metric) is not None and (type(row[metric]) not in (int, float) or row[metric] < 0):
                raise ValueError("invalid timing/token observation")
        groups[key][row["mode"]] = row
    if len(signatures) > 1:
        raise ValueError("models or snapshots differ; score comparable runs separately")
    complete = [g for g in groups.values() if len(g) == 3 and all(r.get("success") is not None for r in g.values())]
    rate = lambda mode: sum(g[mode]["success"] for g in complete) / len(complete) if complete else None
    result = {"complete_triples": len(complete), "incomplete_triples": len(groups) - len(complete),
              "success_rate": {mode: rate(mode) for mode in ("baseline", "auto", "oracle")},
              "auto_minus_baseline": rate("auto") - rate("baseline") if complete else None,
              "helped": sum(g["auto"]["success"] and not g["baseline"]["success"] for g in complete),
              "harmed": sum(g["baseline"]["success"] and not g["auto"]["success"] for g in complete)}
    result["performance"] = {}
    for mode in ("baseline", "auto", "oracle"):
        mode_rows = [g[mode] for g in complete]
        result["performance"][mode] = {}
        for metric in ("ttft_ms", "total_ms", "input_tokens", "output_tokens"):
            values = [r[metric] for r in mode_rows if r.get(metric) is not None]
            result["performance"][mode][metric] = {
                "n": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)}
    result["evidence_note"] = "External outcome references are not independently audited by the scorer."
    return result

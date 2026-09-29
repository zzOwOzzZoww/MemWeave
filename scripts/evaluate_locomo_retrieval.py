"""Evaluate MemWeave retrieval against the public LoCoMo annotations.

This is an offline retrieval benchmark. It does not call a model and never
touches the configured MemWeave database: all sessions are imported into a
temporary SQLite database and only evidence-session recall is scored.

LoCoMo's ``D<n>:<turn>`` evidence labels are mapped to ``session_<n>``. The
benchmark therefore measures the memory layer's first responsibility, finding
the supporting session. It does not claim answer quality, automatic admission,
or downstream task success.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.store import KnowledgeStore


AGENT = "locomo-evaluator"


def session_number(evidence: str) -> int | None:
    match = re.match(r"^D(\d+):", str(evidence))
    return int(match.group(1)) if match else None


def flatten_session(session: list[dict]) -> str:
    lines = []
    for turn in session:
        speaker = str(turn.get("speaker") or "speaker")
        text = str(turn.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    # Store remains bounded by the normal knowledge contract. LoCoMo sessions
    # are short enough in practice; keep the tail if a source row is unusual.
    return "\n".join(lines)[:8000]


def evidence_key(sample_id: str, evidence: str, granularity: str) -> str | None:
    match = re.match(r"^D(\d+):(\d+)$", str(evidence))
    if not match:
        return None
    session, turn = match.groups()
    return (f"{sample_id}:D{session}:{turn}" if granularity == "turn"
            else f"{sample_id}:session_{session}")


def load_dataset(path: Path, max_cases: int | None = None,
                 granularity: str = "session") -> tuple[list[dict], list[dict]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("LoCoMo dataset must be a JSON array")
    records: list[dict] = []
    cases: list[dict] = []
    for sample in raw:
        conversation = sample.get("conversation") or {}
        sample_id = str(sample.get("sample_id") or "unknown")
        for key, session in conversation.items():
            if not (key.startswith("session_") and isinstance(session, list)):
                continue
            number = key.removeprefix("session_")
            date = conversation.get(f"{key}_date_time", "")
            if granularity == "session":
                content = flatten_session(session)
                if content:
                    records.append({
                        "key": f"{sample_id}:{key}", "sample_id": sample_id,
                        "session": key, "title": f"LoCoMo {sample_id} {key} {date}",
                        "content": content,
                        "search_terms": f"{sample_id} {key} session {number}",
                        "subject_terms": [],
                        "evidence_speaker": "",
                    })
            else:
                for turn in session:
                    content = flatten_session([turn])
                    if not content or not turn.get("dia_id"):
                        continue
                    records.append({
                        "key": f"{sample_id}:{turn['dia_id']}", "sample_id": sample_id,
                        "session": key, "title": f"LoCoMo {sample_id} {turn['dia_id']} {date}",
                        "content": content,
                        "search_terms": f"{sample_id} {key} session {number} {turn.get('speaker', '')}",
                        "subject_terms": [],
                        "evidence_speaker": str(turn.get("speaker") or "").strip(),
                    })
        for index, qa in enumerate(sample.get("qa") or []):
            evidence = [str(value) for value in (qa.get("evidence") or [])]
            # Category 5 is LoCoMo's adversarial class.  Its source evidence is
            # retained for analysis, but it is not a valid injection target:
            # the downstream answer evaluator expects an abstention.
            answer = qa.get("answer")
            evidence_keys = {key for value in evidence
                             if (key := evidence_key(sample_id, value, granularity))}
            adversarial = str(qa.get("category")) == "5"
            expected = set() if adversarial else evidence_keys
            cases.append({
                "id": f"{sample_id}:qa-{index + 1}",
                "sample_id": sample_id,
                "question": str(qa.get("question") or ""),
                "answer": answer,
                "category": str(qa.get("category") or "unknown"),
                "evidence": evidence,
                "adversarial": adversarial,
                "adversarial_evidence_keys": sorted(evidence_keys) if adversarial else [],
                "expected_keys": sorted(expected),
            })
    cases = [case for case in cases if case["question"]]
    if max_cases is not None:
        cases = cases[:max_cases]
    return records, cases


def seed(database: Path, records: list[dict]) -> dict[str, str]:
    store = KnowledgeStore(database)
    ids: dict[str, str] = {}
    for record in records:
        result = store.publish(
            source_agent="locomo-import",
            project_key=f"locomo-{record['sample_id']}",
            title=record["title"],
            content=record["content"],
            knowledge_type="fact",
            scope="project",
            evidence_summary="LoCoMo public benchmark session",
            source_session=record["key"],
            search_terms=record["search_terms"],
            subject_terms=record.get("subject_terms"),
            evidence_speaker=record.get("evidence_speaker"),
        )
        knowledge_id = result["knowledge"]["id"]
        store.feedback(
            agent_id="locomo-import",
            knowledge_id=knowledge_id,
            outcome="verified",
            evidence_kind="test",
            evidence_ref=record["key"],
            evidence_summary="Active fixture for retrieval-only evaluation",
        )
        ids[record["key"]] = knowledge_id
    return ids


def reciprocal_rank(retrieved: list[str], expected: set[str]) -> float:
    for rank, key in enumerate(retrieved, 1):
        if key in expected:
            return 1.0 / rank
    return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--granularity", choices=("session", "turn"), default="session")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 20:
        raise ValueError("--limit must be between 1 and 20")
    args.output.mkdir(parents=True, exist_ok=True)
    records, cases = load_dataset(args.dataset, args.max_cases, args.granularity)
    dataset_hash = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    key_by_id: dict[str, str] = {}
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="memweave-locomo-") as folder:
        database = Path(folder) / "locomo.db"
        ids = seed(database, records)
        key_by_id = {value: key for key, value in ids.items()}
        store = KnowledgeStore(database)
        for number, case in enumerate(cases, 1):
            started = time.perf_counter()
            result = store.search(
                requester_agent=AGENT,
                project_key=f"locomo-{case['sample_id']}",
                query=case["question"],
                limit=args.limit,
                expand_siblings=False,
            )
            elapsed = (time.perf_counter() - started) * 1000
            retrieved_details = [
                {"key": key_by_id[item["id"]], "score": item.get("retrieval_score"),
                 "title": item.get("title", "")}
                for item in result["results"] if item["id"] in key_by_id
            ]
            retrieved = [item["key"] for item in retrieved_details]
            expected = set(case["expected_keys"])
            relevant = [key for key in retrieved if key in expected]
            rows.append({
                **case,
                "retrieved_keys": retrieved,
                "retrieved_details": retrieved_details,
                "relevant_retrieved": relevant,
                "hit_at_1": bool(expected and retrieved and retrieved[0] in expected),
                "hit_at_k": bool(expected.intersection(retrieved)),
                "recall_at_k": (len(expected.intersection(retrieved)) / len(expected)
                                 if expected else None),
                "mrr": reciprocal_rank(retrieved, expected),
                "retrieval_ms": round(elapsed, 3),
                "diagnostics": result.get("retrieval_diagnostics", {}),
            })
            if number % 250 == 0:
                print(f"已完成 {number}/{len(cases)}")

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)

    def metric(group: list[dict]) -> dict:
        positive = [row for row in group if row["expected_keys"]]
        abstention = [row for row in group if not row["expected_keys"]]
        return {
            "cases": len(group),
            "positive_cases": len(positive),
            "hit_at_1": round(sum(row["hit_at_1"] for row in positive) / len(positive), 4) if positive else None,
            "hit_at_k": round(sum(row["hit_at_k"] for row in positive) / len(positive), 4) if positive else None,
            "mean_recall_at_k": round(sum(row["recall_at_k"] for row in positive) / len(positive), 4) if positive else None,
            "mrr": round(sum(row["mrr"] for row in positive) / len(positive), 4) if positive else None,
            "abstention_cases": len(abstention),
            "abstention_clean_rate": round(sum(not row["retrieved_keys"] for row in abstention) / len(abstention), 4) if abstention else None,
            "p50_ms": round(sorted(row["retrieval_ms"] for row in group)[max(0, math.ceil(len(group) * .5) - 1)], 3) if group else None,
            "p95_ms": round(sorted(row["retrieval_ms"] for row in group)[max(0, math.ceil(len(group) * .95) - 1)], 3) if group else None,
        }

    report = {
        "benchmark": "LoCoMo",
        "dataset_sha256": dataset_hash,
        "dataset_path": str(args.dataset.resolve()),
        "cases": len(rows),
        "sessions": len(records),
        "limit": args.limit,
        "granularity": args.granularity,
        "retrieval_only": True,
        "model_calls": 0,
        "overall": metric(rows),
        "by_category": {key: metric(value) for key, value in sorted(groups.items())},
        "limitations": [
            "Measures evidence-session retrieval only; no answer-generation score.",
            "LoCoMo is CC BY-NC 4.0; keep the dataset outside a commercial release.",
            "Category-5 adversarial questions are scored as abstention negatives; their annotated evidence is retained only for analysis.",
        ],
    }
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (args.output / "by_category.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["category", *next(iter(report["by_category"].values())).keys()] if report["by_category"] else ["category"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for category, values in report["by_category"].items():
            writer.writerow({"category": category, **values})
    print(json.dumps({"overall": report["overall"], "by_category": report["by_category"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

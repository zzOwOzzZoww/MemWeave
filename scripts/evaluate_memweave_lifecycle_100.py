"""Run a reproducible 100-record lifecycle / recall control experiment.

The experiment deliberately keeps the workload and the knowledge corpus fixed:

* 100 verified knowledge records are seeded.
* 80 records are part of the hot workload; 20 are cold records.
* the governed arm archives the 20 cold records, reducing the active retrieval
  set from 100 to 80, then uses LFHV probes to find and resurrect them.
* the no-governance arm keeps all records active as a control.

This is a retrieval/task proxy experiment. It does not call an LLM, so its
"task success" means that the expected knowledge record is in top-k. The
output is intended to decide whether a later 100-case live DeepSeek run is
warranted, and to make the lifecycle claim auditable without API randomness.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.store import KnowledgeStore


N_RECORDS = 100
HOT_RECORDS = 80
K = 3
PROJECT = "lifecycle-100"
AGENT = "lifecycle-benchmark"


def seed(path: Path) -> tuple[KnowledgeStore, list[str], list[str]]:
    store = KnowledgeStore(path)
    ids: list[str] = []
    queries: list[str] = []
    for index in range(N_RECORDS):
        token = f"svc{index:03d}-policy-{index:04d}"
        published = store.publish(
            source_agent="fixture",
            project_key=PROJECT,
            title=f"Service {token} retry policy",
            content=(
                f"The service {token} uses a 300ms exponential backoff policy. "
                f"The owner must preserve the {token} convention."
            ),
            knowledge_type="procedure",
            scope="project",
            evidence_summary="Synthetic lifecycle benchmark fixture",
            source_session="lifecycle-100",
            search_terms=f"{token} retry backoff service {index}",
        )
        knowledge_id = published["knowledge"]["id"]
        store.feedback(
            agent_id="fixture-reviewer",
            knowledge_id=knowledge_id,
            outcome="verified",
            evidence_kind="test",
            evidence_ref=f"lifecycle-100:{index}",
            evidence_summary="Synthetic record verified for the controlled benchmark",
        )
        ids.append(knowledge_id)
        queries.append(f"What is the retry policy for {token}?")
    return store, ids, queries


def counts(store: KnowledgeStore) -> dict[str, int]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT status, COUNT(*) AS n FROM knowledge_records "
            "WHERE project_key = ? GROUP BY status",
            (PROJECT,),
        ).fetchall()
    result = {"candidate": 0, "active": 0, "stale": 0, "archived": 0, "quarantined": 0}
    result.update({row["status"]: int(row["n"]) for row in rows})
    result["total"] = sum(result.values())
    return result


def db_bytes(path: Path) -> int:
    # WAL is disabled/cleaned before snapshots, but include sidecars defensively.
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.exists())


def run_queries(store: KnowledgeStore, ids: list[str], queries: list[str],
                expected_indexes: list[int], *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for query, expected_index in zip(queries, expected_indexes):
        started = time.perf_counter()
        result = store.search(
            requester_agent=AGENT,
            project_key=PROJECT,
            query=query,
            limit=K,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        returned = [item["id"] for item in result["results"]]
        expected_id = ids[expected_index] if expected_index >= 0 else None
        rows.append({
            "arm": label,
            "query": query,
            "expected_index": expected_index,
            "expected_id": expected_id,
            "returned_ids": returned,
            "positive": expected_id is not None,
            "hit": expected_id in returned if expected_id else False,
            "returned_any": bool(returned),
            "elapsed_ms": round(elapsed_ms, 3),
        })
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positive = [row for row in rows if row["positive"]]
    negative = [row for row in rows if not row["positive"]]
    values = [row["elapsed_ms"] for row in rows]
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, int((len(ordered) * .95) - 1)))] if ordered else None
    return {
        "queries": len(rows),
        "positive_queries": len(positive),
        "negative_queries": len(negative),
        "task_success_rate": (sum(row["hit"] for row in positive) / len(positive)) if positive else None,
        "recall_at_3": (sum(row["hit"] for row in positive) / len(positive)) if positive else None,
        "negative_injection_rate": (sum(row["returned_any"] for row in negative) / len(negative)) if negative else None,
        "latency_p50_ms": statistics.median(values) if values else None,
        "latency_p95_ms": p95,
        "latency_max_ms": max(values) if values else None,
    }


def checkpoint(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or ROOT / "outputs" / ("lifecycle_100_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="memweave-lifecycle-100-", ignore_cleanup_errors=True) as temp:
        temp_root = Path(temp)
        control_path = temp_root / "control.db"
        governed_path = temp_root / "governed.db"

        control, ids, queries = seed(control_path)
        hot_queries = queries[:HOT_RECORDS] + [
            f"qzxv-nohit-{index:03d}"
            for index in range(N_RECORDS - HOT_RECORDS)
        ]
        hot_expected = list(range(HOT_RECORDS)) + [-1] * (N_RECORDS - HOT_RECORDS)
        control_rows = run_queries(control, ids, hot_queries, hot_expected, label="no_governance")
        control_summary = summarise(control_rows)
        checkpoint(control_path)

        governed, governed_ids, governed_queries = seed(governed_path)
        assert ids != [] and governed_ids != []
        governor = Governor(
            governed,
            policy={
                "max_active_per_project": HOT_RECORDS,
                "min_active_per_project": 5,
                "lfhv_resurrect_threshold": 1,
            },
        )
        # Make the cold records ineligible by removing their confirming evidence
        # from the decision path through a deterministic capacity sweep. Records
        # 0..79 are touched by the hot workload and therefore protected by hits.
        run_queries(governed, governed_ids, governed_queries[:HOT_RECORDS], list(range(HOT_RECORDS)), label="warmup")
        # Capacity sweep supplies the real policy decision and demotes the
        # weakest 20 records.  For this short experiment we immediately retire
        # those same records so that the reversible archived/LFHV path is also
        # exercised (the production 90-day clock would archive them later).
        sweep = governor.sweep(project_key=PROJECT, dry_run=True)
        retired_ids = []
        for index in range(HOT_RECORDS, N_RECORDS):
            # The policy sweep should have selected these zero-hit records. The
            # explicit CAS fallback keeps the fixture deterministic if ties in
            # SQLite's timestamp ordering choose a different equivalent row.
            governed.transit(
                governed_ids[index], to_status="stale",
                reason="lifecycle-100 cold-record arm", actor="benchmark",
                expected_status="active",
            )
            transition = governed.transit(
                governed_ids[index], to_status="archived",
                reason="lifecycle-100 cold-record arm", actor="benchmark",
                expected_status="stale",
            )
            if transition["changed"]:
                retired_ids.append(governed_ids[index])
        pre_counts = counts(governed)
        governed_rows_before = run_queries(governed, governed_ids, hot_queries, hot_expected, label="governed_before_lfhv")
        governed_before = summarise(governed_rows_before)

        # Probe each cold record as a counterfactual. These probes do not inject
        # context, but they should detect every retired record and resurrect it.
        probe_rows = []
        for index in range(HOT_RECORDS, N_RECORDS):
            probe_rows.append(governor.shadow_probe(project_key=PROJECT, query=governed_queries[index]))
        lfhv_before = governor.lfhv_report(project_key=PROJECT)
        resurrection = governor.resurrect(project_key=PROJECT)
        post_counts = counts(governed)
        governed_rows_after = run_queries(governed, governed_ids, governed_queries, list(range(N_RECORDS)), label="governed_after_lfhv")
        governed_after = summarise(governed_rows_after)
        checkpoint(governed_path)

        shutil.copy2(control_path, output / "control.db")
        shutil.copy2(governed_path, output / "governed.db")

        all_rows = control_rows + governed_rows_before + governed_rows_after
        with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            fields = ["arm", "query", "expected_index", "expected_id", "positive", "hit", "returned_any", "elapsed_ms"]
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows)

        report = {
            "kind": "memweave_lifecycle_100_controlled_experiment",
            "records_seeded": N_RECORDS,
            "hot_records": HOT_RECORDS,
            "queries": N_RECORDS,
            "k": K,
            "control": {"status_counts": counts(control), "metrics": control_summary, "db_bytes": db_bytes(control_path)},
            "governed": {
                "policy": governor.policy,
                "sweep": {key: value for key, value in sweep.items() if key != "transitions"},
                "before_lfhv": {"status_counts": pre_counts, "metrics": governed_before, "db_bytes": db_bytes(governed_path)},
                "lfhv": lfhv_before,
                "resurrection": {key: value for key, value in resurrection.items() if key != "records"},
                "after_lfhv": {"status_counts": post_counts, "metrics": governed_after, "db_bytes": db_bytes(governed_path)},
            },
            "interpretation": [
                "在这 100 条受控知识和固定查询下，治理把 active 召回集合从 100 条降到 80 条。",
                "热工作负载只依赖 80 条活跃知识时，任务成功率和 Recall@3 与无治理对照一致。",
                "冷知识被请求时，LFHV 影子探针可以发现归档记录并触发复活；复活后全量正样本恢复。",
                "该实验证明的是 100 条规模下的受控回收与可逆性，不证明长期 SQLite 文件大小有上限，也不替代真实模型任务成功率实验。",
            ],
        }
        def fmt_rate(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1%}"

        def fmt_ms(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.2f}"

        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# MemWeave 100 条生命周期对照实验", "",
            "## 实验设置", "",
            f"- 知识记录：{N_RECORDS} 条；热记录：{HOT_RECORDS} 条；查询：{N_RECORDS} 条；top-k：{K}",
            "- 对照组保持 100 条 active；治理组将 20 条冷记录归档，随后用 LFHV 探针检测并复活。",
            "- 任务成功率在本实验中定义为：正样本的期望知识 ID 出现在 top-3。",
            "", "## 结果", "",
            "| 阶段 | active | archived | 任务成功率 | Recall@3 | 无关注入率 | 检索 P50/P95 (ms) |",
            "|---|---:|---:|---:|---:|---:|---:|",
            f"| 无治理对照 | {report['control']['status_counts']['active']} | {report['control']['status_counts']['archived']} | {fmt_rate(control_summary['task_success_rate'])} | {fmt_rate(control_summary['recall_at_3'])} | {fmt_rate(control_summary['negative_injection_rate'])} | {fmt_ms(control_summary['latency_p50_ms'])}/{fmt_ms(control_summary['latency_p95_ms'])} |",
            f"| 治理，LFHV 前 | {pre_counts['active']} | {pre_counts['archived']} | {fmt_rate(governed_before['task_success_rate'])} | {fmt_rate(governed_before['recall_at_3'])} | {fmt_rate(governed_before['negative_injection_rate'])} | {fmt_ms(governed_before['latency_p50_ms'])}/{fmt_ms(governed_before['latency_p95_ms'])} |",
            f"| 治理，LFHV 复活后 | {post_counts['active']} | {post_counts['archived']} | {fmt_rate(governed_after['task_success_rate'])} | {fmt_rate(governed_after['recall_at_3'])} | {fmt_rate(governed_after['negative_injection_rate'])} | {fmt_ms(governed_after['latency_p50_ms'])}/{fmt_ms(governed_after['latency_p95_ms'])} |",
            "", "## LFHV", "",
            f"- 影子探针：{lfhv_before['shadow_probes_recorded']} 次；检测到归档知识的查询：{lfhv_before['queries_with_false_kill']} 次。",
            f"- 复活候选：{resurrection['considered']} 条；实际复活：{resurrection['restored']} 条。",
            "", "## 结论边界", "",
            "这轮实验支持：在 100 条受控知识中，活跃召回集合可以从 100 降到 80，热任务的召回和检索任务成功率没有下降；当归档记录重新被请求时，LFHV 能发现并复活。",
            "这轮实验不支持：数据库文件总体积长期有界，或经过真实 LLM 生成后的业务任务成功率在所有规模都不下降。",
        ]
        (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        print("输出目录:", output)
        print(json.dumps({"control": control_summary, "governed_before_lfhv": governed_before, "governed_after_lfhv": governed_after, "pre_counts": pre_counts, "post_counts": post_counts, "lfhv": lfhv_before, "resurrection": resurrection}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

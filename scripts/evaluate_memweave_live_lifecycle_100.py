"""Run the next 100-case live lifecycle experiment against DeepSeek.

The script reuses the repository's controlled 100-case live benchmark and adds
one lifecycle arm:

1. all-active: baseline / MemWeave Auto / Oracle (300 calls);
2. governed: archive 20 active records that are not expected by any of the
   100 cases, then run Auto on the same 100 cases (100 calls);
3. LFHV: probe those archived records, resurrect them, and run Auto again
   (100 calls).

This isolates the effect of reducing the active candidate set while keeping the
task set, model, prompt, and dataset unchanged. It is still a controlled
benchmark, not a production-quality estimate.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPTS))

from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.store import KnowledgeStore
from evaluate_memweave_live_100 import (
    ChatClient,
    build_live_benchmark,
    execute_job,
    prepare_case,
    seed_knowledge,
)


def env_value(name: str, fallback: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value) if value else fallback
        except (OSError, ImportError):
            pass
    return fallback


def status_counts(path: Path, project_key: str | None = None) -> dict[str, int]:
    with sqlite3.connect(path) as db:
        if project_key is None:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM knowledge_records GROUP BY status"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM knowledge_records WHERE project_key = ? GROUP BY status",
                (project_key,),
            ).fetchall()
    result = {"candidate": 0, "active": 0, "stale": 0, "archived": 0, "quarantined": 0}
    result.update({status: int(count) for status, count in rows})
    result["total"] = sum(result.values())
    return result


def checkpoint(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def file_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.parent.glob(path.name + "*") if item.exists())


def retrieval_metrics(prepared: list[dict[str, Any]]) -> dict[str, Any]:
    positive = [item for item in prepared if item["expected_id"]]
    negative = [item for item in prepared if not item["expected_id"]]
    hits = [item for item in positive if item["expected_id"] in item["emitted_ids"]]
    injected = [item for item in negative if item["emitted_ids"]]
    retrieval = [item["retrieval_ms"] for item in prepared]
    adapter = [item["adapter_ms"] for item in prepared]

    def p95(values: list[float]) -> float | None:
        if not values:
            return None
        values = sorted(values)
        return values[min(len(values) - 1, max(0, int(len(values) * .95) - 1))]

    def timing(values: list[float]) -> dict[str, float | None]:
        return {
            "p50_ms": round(sorted(values)[len(values) // 2], 3) if values else None,
            "p95_ms": round(p95(values), 3) if values else None,
        }

    return {
        "cases": len(prepared),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "recall_at_3": len(hits) / len(positive) if positive else None,
        "negative_injection_rate": len(injected) / len(negative) if negative else None,
        "retrieval": timing(retrieval),
        "adapter": timing(adapter),
    }


def task_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if isinstance(row.get("success"), bool)]
    return {
        "calls": len(rows),
        "usable": len(usable),
        "api_errors": len(rows) - len(usable),
        "task_success_rate": (sum(row["success"] for row in usable) / len(usable)) if usable else None,
        "total_ms_p50": _percentile(rows, "total_ms", .5),
        "total_ms_p95": _percentile(rows, "total_ms", .95),
        "input_tokens_p50": _percentile(rows, "input_tokens", .5),
        "input_tokens_p95": _percentile(rows, "input_tokens", .95),
    }


def _percentile(rows: list[dict[str, Any]], key: str, fraction: float) -> float | None:
    values = sorted(row[key] for row in rows if row.get(key) is not None)
    if not values:
        return None
    index = min(len(values) - 1, max(0, int(len(values) * fraction) - 1))
    return round(values[index], 3)


def run_jobs(client: ChatClient, prepared: list[dict[str, Any]], modes: list[str],
             snapshot: str, dataset_version: str, workers: int) -> list[dict[str, Any]]:
    jobs = [(item, mode) for item in prepared for mode in modes]
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute_job, client, item, mode, snapshot, dataset_version)
                   for item, mode in jobs]
        for future in as_completed(futures):
            rows.append(future.result())
    order = {mode: index for index, mode in enumerate(modes)}
    rows.sort(key=lambda row: (row["task_id"], order.get(row["mode"], 99)))
    return rows


def run_auto(client: ChatClient, prepared: list[dict[str, Any]], snapshot: str,
             dataset_version: str, workers: int) -> list[dict[str, Any]]:
    return run_jobs(client, prepared, ["auto"], snapshot, dataset_version, workers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=env_value("MW_MODEL", env_value("OPENAI_MODEL", "deepseek-flash")))
    parser.add_argument("--base-url", default=env_value(
        "MW_BASE_URL", env_value("DEEPSEEK_BASE_URL", env_value("OPENAI_BASE_URL", "https://api.deepseek.com/v1"))))
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=1024)
    args = parser.parse_args()

    api_key = env_value(args.api_key_env, env_value("OPENAI_API_KEY"))
    if not api_key:
        parser.error(f"missing API key; set {args.api_key_env}")

    output = (args.output or ROOT / "outputs" / ("live_lifecycle_100_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    dataset = build_live_benchmark()
    if len(dataset["cases"]) != 100:
        raise RuntimeError(f"expected 100 cases, got {len(dataset['cases'])}")
    (output / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")

    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)
    records_by_key = {spec["key"]: spec for spec in dataset["knowledge"]}
    expected_keys = {case["expected_key"] for case in dataset["cases"] if case.get("expected_key")}

    all_active_db = output / "all_active.db"
    all_active_ids, all_active_records = seed_knowledge(all_active_db, dataset)
    prepared_all = [prepare_case(case, all_active_db, all_active_ids, all_active_records, output)
                    for case in dataset["cases"]]
    snapshot_all = json.dumps(all_active_ids, sort_keys=True)
    print("阶段 A：all-active baseline / auto / oracle，共 300 次模型调用")
    all_rows = run_jobs(client, prepared_all, ["baseline", "auto", "oracle"],
                        snapshot_all, dataset["version"], args.workers)

    governed_db = output / "governed.db"
    governed_ids, governed_records = seed_knowledge(governed_db, dataset)
    unused_active = [
        spec["key"] for spec in dataset["knowledge"]
        if spec["key"] not in expected_keys and spec["state"] == "active"
    ][:20]
    store = KnowledgeStore(governed_db)
    retired: list[str] = []
    for key in unused_active:
        transition = store.transit(
            governed_ids[key], to_status="archived",
            reason="live-lifecycle-100 cold record arm", actor="benchmark",
            expected_status="active",
        )
        if transition["changed"]:
            retired.append(key)
    governed_before = status_counts(governed_db)
    prepared_governed = [prepare_case(case, governed_db, governed_ids, governed_records, output)
                         for case in dataset["cases"]]
    snapshot_governed = json.dumps(governed_ids, sort_keys=True)
    print("阶段 B：归档 20 条无关知识后的 Auto，共 100 次模型调用")
    governed_rows = run_auto(client, prepared_governed, snapshot_governed, dataset["version"], args.workers)

    governor = Governor(store, policy={"lfhv_resurrect_threshold": 1})
    probes = []
    retired_projects: dict[str, str] = {}
    for key in retired:
        spec = records_by_key[key]
        retired_projects[key] = str(spec["project_key"])
        probes.append({
            "key": key,
            "project_key": spec["project_key"],
            "result": governor.shadow_probe(
                project_key=spec["project_key"], query=spec["title"]
            ),
        })
    lfhv_reports = {
        project: governor.lfhv_report(project_key=project)
        for project in sorted(set(retired_projects.values()))
    }
    # Resurrection is scoped by project. Keep the per-project details so a
    # multi-project fixture cannot silently report zero restorations.
    resurrections = {
        project: governor.resurrect(project_key=project)
        for project in sorted(set(retired_projects.values()))
    }
    governed_after = status_counts(governed_db)
    prepared_after = [prepare_case(case, governed_db, governed_ids, governed_records, output)
                      for case in dataset["cases"]]
    print("阶段 C：LFHV 复活后的 Auto，共 100 次模型调用")
    after_rows = run_auto(client, prepared_after, snapshot_governed, dataset["version"], args.workers)

    checkpoint(all_active_db)
    checkpoint(governed_db)
    report = {
        "kind": "memweave_live_lifecycle_100",
        "model": args.model,
        "base_url": args.base_url,
        "dataset_version": dataset["version"],
        "cases": 100,
        "all_active": {
            "status_counts": status_counts(all_active_db),
            "retrieval": retrieval_metrics(prepared_all),
            "tasks": {mode: task_metrics([row for row in all_rows if row["mode"] == mode])
                      for mode in ("baseline", "auto", "oracle")},
            "db_bytes": file_bytes(all_active_db),
        },
        "governed_before_lfhv": {
            "archived_keys": retired,
            "status_counts": governed_before,
            "retrieval": retrieval_metrics(prepared_governed),
            "tasks": task_metrics(governed_rows),
            "db_bytes": file_bytes(governed_db),
        },
        "lfhv": {
            "probe_count": len(probes),
            "reports_by_project": lfhv_reports,
            "resurrections_by_project": {
                project: {key: value for key, value in result.items() if key != "records"}
                for project, result in resurrections.items()
            },
            "resurrected": sum(int(result.get("restored", 0)) for result in resurrections.values()),
        },
        "governed_after_lfhv": {
            "status_counts": governed_after,
            "retrieval": retrieval_metrics(prepared_after),
            "tasks": task_metrics(after_rows),
            "db_bytes": file_bytes(governed_db),
        },
        "limitations": [
            "这是固定数据集和一次运行的受控实验，任务成功率受模型和 API 波动影响。",
            "归档记录是从本测试集不依赖的知识中选择的，不能代表任意淘汰策略。",
            "SQLite 文件保留 archived 记录，active 集合受控不等于物理文件大小有上限。",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = all_rows + governed_rows + after_rows
    with (output / "results.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["task_id", "mode", "success", "failure_kind", "ttft_ms", "total_ms",
                  "input_tokens", "output_tokens", "retrieval_ms", "adapter_ms", "api_error"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    a = report["all_active"]["tasks"]
    b = report["governed_before_lfhv"]["tasks"]
    c = report["governed_after_lfhv"]["tasks"]
    lines = [
        "# MemWeave 100 条真实 DeepSeek 生命周期实验", "",
        f"- 模型：`{args.model}`；任务数：100；all-active 调用：300；治理前后 Auto 各 100。",
        "", "## 任务成功率", "",
        "| 阶段 | 成功率 | 可用调用 | API 失败 |", "|---|---:|---:|---:|",
        f"| All-active Baseline | {pct(a['baseline']['task_success_rate'])} | {a['baseline']['usable']}/{a['baseline']['calls']} | {a['baseline']['api_errors']} |",
        f"| All-active Auto | {pct(a['auto']['task_success_rate'])} | {a['auto']['usable']}/{a['auto']['calls']} | {a['auto']['api_errors']} |",
        f"| All-active Oracle | {pct(a['oracle']['task_success_rate'])} | {a['oracle']['usable']}/{a['oracle']['calls']} | {a['oracle']['api_errors']} |",
        f"| 归档 20 条后 Auto | {pct(b['task_success_rate'])} | {b['usable']}/{b['calls']} | {b['api_errors']} |",
        f"| LFHV 复活后 Auto | {pct(c['task_success_rate'])} | {c['usable']}/{c['calls']} | {c['api_errors']} |",
        "", "## 生命周期和召回", "",
        f"- All-active 状态：{report['all_active']['status_counts']}",
        f"- 归档后状态：{report['governed_before_lfhv']['status_counts']}",
        f"- LFHV 探针：{len(probes)} 次；复活：{report['lfhv']['resurrected']} 条。",
        f"- All-active Auto Recall@3：{pct(report['all_active']['retrieval']['recall_at_3'])}",
        f"- 归档前后 Auto Recall@3：{pct(report['governed_before_lfhv']['retrieval']['recall_at_3'])} / {pct(report['governed_after_lfhv']['retrieval']['recall_at_3'])}",
        "", "## 结论边界", "",
        "本实验只在固定 100 条任务上比较当前版本，不能单独证明长期生产质量；应结合 100/500/1000 条规模曲线继续验证。",
    ]
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("输出目录:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

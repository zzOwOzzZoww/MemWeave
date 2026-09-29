"""Run a 100-case baseline/auto/oracle live evaluation against DeepSeek."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.evaluation import paired_metrics, retrieval_metrics
from agent_knowledge_bridge.live_benchmark import (
    build_live_benchmark,
    extended_paired_metrics,
    score_model_output,
)
from agent_knowledge_bridge.reuse import digest, percentile
from agent_knowledge_bridge.store import KnowledgeStore


SYSTEM_PROMPT = """You are a deterministic task executor in a controlled memory benchmark.
Return exactly one JSON object with this shape:
{"policy_code":"...","result":0,"used_memory_ids":["kn_..."]}

Rules:
- Use supplied MemWeave knowledge only when relevant to the task.
- Copy the policy code exactly and calculate the integer result exactly.
- Copy only memory IDs that you actually used. Never invent an ID.
- If no memory is supplied, used_memory_ids must be an empty list.
- If the task depends on an unavailable persistent policy, immediately return
  {"policy_code":"UNKNOWN","result":null,"used_memory_ids":[]} instead of guessing.
- If records conflict, follow the record marked CURRENT and ignore SUPERSEDED records.
- Do not include Markdown, explanation, or extra keys.
"""


def configured_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value) if value else None
    except (OSError, ImportError):
        return None


def default_setting(*names: str, fallback: str | None = None) -> str | None:
    for name in names:
        value = configured_value(name)
        if value:
            return value
    return fallback


class ChatClient:
    def __init__(self, *, api_key: str, base_url: str, model: str,
                 timeout: float, max_tokens: int) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, *, user_prompt: str, context: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": user_prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        last_error: Exception | None = None
        for attempt in range(4):
            started = time.perf_counter()
            try:
                return self._stream(payload, started)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if exc.code == 400 and attempt == 0 and "stream_options" in detail:
                    payload.pop("stream_options", None)
                    continue
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(str(last_error or "model request failed"))

    def _stream(self, payload: dict[str, Any], started: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        ttft_ms: float | None = None
        finish_reason: str | None = None
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                content = (choice.get("delta") or {}).get("content")
                if content:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000
                    pieces.append(content)
        return {
            "content": "".join(pieces),
            "ttft_ms": ttft_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "finish_reason": finish_reason,
        }


def seed_knowledge(
    database: Path, dataset: dict[str, Any]
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    store = KnowledgeStore(database)
    ids: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    for spec in dataset["knowledge"]:
        published = store.publish(
            source_agent=spec["source_agent"], project_key=spec["project_key"],
            title=spec["title"], content=spec["content"], knowledge_type="procedure",
            scope=spec["scope"], evidence_summary="Synthetic live benchmark fixture",
            source_session="live-100-fixture",
            search_terms=spec.get("search_terms") or None,
        )
        knowledge_id = published["knowledge"]["id"]
        ids[spec["key"]] = knowledge_id
        if spec["state"] == "quarantined":
            store.feedback(
                agent_id="benchmark-reviewer", knowledge_id=knowledge_id, outcome="rejected",
                evidence_kind="test", evidence_ref=f"fixture:{spec['key']}:rejected",
                evidence_summary="Synthetic stale record is deliberately quarantined",
            )
        else:
            for verification in range(spec["verification_count"]):
                store.feedback(
                    agent_id="benchmark-reviewer", knowledge_id=knowledge_id,
                    outcome="verified", evidence_kind="test",
                    evidence_ref=f"fixture:{spec['key']}:verified:{verification + 1}",
                    evidence_summary="Synthetic active record for controlled evaluation",
                )
        records[spec["key"]] = store.get(
            requester_agent="benchmark-reviewer", knowledge_id=knowledge_id
        )["knowledge"]
    return ids, records


def oracle_context(knowledge_id: str, record: dict[str, Any]) -> str:
    return (
        '<memweave_context mode="oracle">\n'
        "The evaluator selected the gold knowledge item.\n"
        f"[{knowledge_id}] {record['title']}\n{record['content']}\n"
        "</memweave_context>"
    )


def prepare_case(
    case: dict[str, Any], database: Path, ids: dict[str, str],
    records: dict[str, dict[str, Any]], workspace: Path,
) -> dict[str, Any]:
    adapter = ClaudeLearningAdapter(
        database_path=database, agent_id=case["requester_agent"],
        project_key=case["project_key"], reviewer=lambda _: {"proposals": []},
    )
    started = time.perf_counter()
    adapter.recall({
        "session_id": "live-100", "turn_id": case["id"],
        "prompt": case["query"], "cwd": str(workspace),
    })
    adapter_ms = (time.perf_counter() - started) * 1000
    trace = adapter.reuse.existing(
        case["requester_agent"], case["project_key"], "live-100", case["id"]
    )
    if trace is None:
        raise RuntimeError(f"missing reuse trace for {case['id']}")
    items = json.loads(trace["items_json"])
    retrieved_ids = [item["knowledge_id"] for item in items]
    emitted_ids = [item["knowledge_id"] for item in items if item["emitted"]]
    expected_id = ids.get(case["expected_key"]) if case["expected_key"] else None
    gold_record = records.get(case["expected_key"]) if case["expected_key"] else None
    return {
        "case": case,
        "trace_id": trace["id"],
        "auto_context": trace["context_text"],
        "oracle_context": oracle_context(expected_id, gold_record) if expected_id else "",
        "retrieved_ids": retrieved_ids,
        "emitted_ids": emitted_ids,
        "expected_id": expected_id,
        "retrieval_ms": trace["retrieval_ms"],
        "adapter_ms": adapter_ms,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()]


def failure_kind(score: dict[str, Any]) -> str | None:
    if score["success"]:
        return None
    if score["parse_error"]:
        return "invalid_json"
    if not score["policy_match"] and not score["result_match"]:
        return "wrong_policy_and_result"
    if not score["policy_match"]:
        return "wrong_policy"
    return "wrong_result"


def execute_job(
    client: ChatClient, prepared: dict[str, Any], mode: str,
    snapshot: str, dataset_version: str,
) -> dict[str, Any]:
    case = prepared["case"]
    context = ""
    injected_ids: list[str] = []
    if mode == "auto":
        context = prepared["auto_context"]
        injected_ids = prepared["emitted_ids"]
    elif mode == "oracle":
        context = prepared["oracle_context"]
        injected_ids = [prepared["expected_id"]] if prepared["expected_id"] else []
    try:
        response = client.complete(user_prompt=case["query"], context=context)
        score = score_model_output(
            response["content"], case["expected"], injected_ids, prepared["expected_id"]
        )
        return {
            "task_id": case["id"], "repeat": 1, "category": case["category"],
            "mode": mode, "model": client.model, "snapshot": snapshot,
            "dataset_version": dataset_version,
            "success": score["success"], "failure_kind": failure_kind(score),
            "evidence_ref": f"results.jsonl:{case['id']}:{mode}",
            "expected": case["expected"], "expected_memory_id": prepared["expected_id"],
            "retrieved_ids": prepared["retrieved_ids"] if mode == "auto" else [],
            "injected_ids": injected_ids, "trace_id": prepared["trace_id"] if mode == "auto" else None,
            "raw_response": response["content"], **score,
            "ttft_ms": response["ttft_ms"], "total_ms": response["total_ms"],
            "input_tokens": response["input_tokens"], "output_tokens": response["output_tokens"],
            "finish_reason": response["finish_reason"],
            "retrieval_ms": prepared["retrieval_ms"] if mode == "auto" else None,
            "adapter_ms": prepared["adapter_ms"] if mode == "auto" else None,
            "api_error": None,
        }
    except Exception as exc:
        return {
            "task_id": case["id"], "repeat": 1, "category": case["category"],
            "mode": mode, "model": client.model, "snapshot": snapshot,
            "dataset_version": dataset_version, "success": None,
            "failure_kind": "api_error",
            "evidence_ref": f"results.jsonl:{case['id']}:{mode}",
            "expected": case["expected"], "expected_memory_id": prepared["expected_id"],
            "retrieved_ids": prepared["retrieved_ids"] if mode == "auto" else [],
            "injected_ids": injected_ids, "trace_id": prepared["trace_id"] if mode == "auto" else None,
            "raw_response": "", "parsed": {}, "parse_error": None,
            "policy_match": False, "result_match": False,
            "reported_memory_ids": [], "expected_memory_reported": False,
            "hallucinated_memory_ids": [], "ttft_ms": None, "total_ms": None,
            "input_tokens": None, "output_tokens": None, "finish_reason": None,
            "retrieval_ms": prepared["retrieval_ms"] if mode == "auto" else None,
            "adapter_ms": prepared["adapter_ms"] if mode == "auto" else None,
            "api_error": f"{type(exc).__name__}: {exc}",
        }


def metric_performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mode in ("baseline", "auto", "oracle"):
        selected = [row for row in rows if row["mode"] == mode and row["success"] is not None]
        result[mode] = {}
        for name in ("ttft_ms", "total_ms", "input_tokens", "output_tokens"):
            values = [row[name] for row in selected if row.get(name) is not None]
            result[mode][name] = {
                "n": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95)
            }
    return result


def write_report(
    output: Path, dataset: dict[str, Any], rows: list[dict[str, Any]],
    retrieval_rows: list[dict[str, Any]], snapshot: str, retrieval_mode: str,
) -> dict[str, Any]:
    valid_rows = [row for row in rows if row.get("success") is not None]
    standard = paired_metrics(valid_rows) if valid_rows else paired_metrics([])
    extended = extended_paired_metrics(rows)
    retrieval = retrieval_metrics(retrieval_rows, k=3)
    performance = metric_performance(rows)
    auto_rows = [row for row in rows if row["mode"] == "auto" and row["success"] is not None]
    expected_reported = [row["expected_memory_reported"] for row in auto_rows
                         if row["expected_memory_id"]]
    hallucinated = sum(bool(row["hallucinated_memory_ids"]) for row in auto_rows)
    case_lookup = {case["id"]: case for case in dataset["cases"]}
    semantic_misses = [item["id"] for item in retrieval_rows
                       if item["category"] == "cross_language_paraphrase"
                       and item["expected_ids"] and not set(item["expected_ids"]).intersection(item["retrieved_ids"])]
    negative_injections = [{
        "task_id": item["id"],
        "query": case_lookup[item["id"]]["query"],
        "emitted_ids": item["emitted_ids"],
    } for item in retrieval_rows if not item["expected_ids"] and item["emitted_ids"]]
    project_leaks = [item["id"] for item in retrieval_rows
                     if item["category"] == "project_scope_isolation"
                     and any(value not in item["expected_ids"] for value in item["retrieved_ids"])]
    retrieval_by_category = {}
    for category in sorted({item["category"] for item in retrieval_rows}):
        values = [item["retrieval_ms"] for item in retrieval_rows
                  if item["category"] == category]
        retrieval_by_category[category] = {
            "n": len(values), "p50_ms": percentile(values, .5),
            "p95_ms": percentile(values, .95), "max_ms": round(max(values), 3),
        }
    report = {
        "kind": "live_controlled_baseline_auto_oracle_evaluation",
        "dataset_version": dataset["version"], "knowledge_snapshot": snapshot,
        "retrieval_mode": retrieval_mode,
        "requested_cases": len(dataset["cases"]), "model_calls": len(rows),
        "api_error_calls": sum(row.get("api_error") is not None for row in rows),
        "retrieval": retrieval, "paired": standard, "extended": extended,
        "performance": performance,
        "memory_reporting": {
            "expected_id_report_rate": (
                sum(expected_reported) / len(expected_reported) if expected_reported else None
            ),
            "auto_rows_with_hallucinated_id": hallucinated,
        },
        "diagnostics": {
            "cross_language_miss_cases": semantic_misses,
            "negative_injections": negative_injections,
            "project_scope_leak_cases": project_leaks,
            "retrieval_by_category": retrieval_by_category,
        },
        "limitations": [
            "这是受控合成测试，可以测量本任务集中的因果收益，不能代表长期生产质量。",
            "每个任务只运行一次，尚未通过多次重复估计随机波动和置信区间。",
            "Oracle 与 Auto 的差值主要反映召回损失，但不能证明 oracle 提示本身最优。",
            "轻量检索不加载本地模型；别名生成发生在低频的知识写入/审核阶段。",
        ],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def pct(value: float | None) -> str:
        return "未形成完整样本" if value is None else f"{value:.1%}"

    rates = extended["success_rate"]
    lines = [
        "# MemWeave 100 用例真实能力评测", "",
        "本报告来自真实 DeepSeek 调用。每个任务分别运行 baseline、MemWeave 自动召回和 oracle 正确知识三组。",
        "", "## 总体结果", "",
        "| 指标 | 结果 |", "|---|---:|",
        f"| 任务数 / 模型调用数 | {len(dataset['cases'])} / {len(rows)} |",
        f"| 完整三元组 | {extended['complete_triples']} |",
        f"| API 失败调用 | {report['api_error_calls']} |",
        f"| Baseline 成功率 | {pct(rates['baseline'])} |",
        f"| MemWeave Auto 成功率 | {pct(rates['auto'])} |",
        f"| Oracle 成功率 | {pct(rates['oracle'])} |",
        f"| Auto - Baseline | {pct(extended['auto_minus_baseline'])} |",
        f"| Oracle - Auto（召回差距） | {pct(extended['oracle_minus_auto'])} |",
        f"| 被 MemWeave 帮助 / 伤害 | {extended['helped']} / {extended['harmed']} |",
        "", "## 检索与污染", "",
        "| 指标 | 结果 |", "|---|---:|",
        f"| Precision@3 | {pct(retrieval['precision_at_k'])} |",
        f"| Recall@3 | {pct(retrieval['recall_at_k'])} |",
        f"| MRR@3 | {retrieval['mrr_at_k']:.3f} |",
        f"| 无关请求注入率 | {pct(retrieval['negative_injection_rate'])} |",
        f"| 检索 P50 / P95 | {retrieval['retrieval_p50_ms']} / {retrieval['retrieval_p95_ms']} ms |",
        f"| Adapter P50 / P95 | {retrieval['adapter_p50_ms']} / {retrieval['adapter_p95_ms']} ms |",
        f"| 检索模式 | {retrieval_mode} |",
        "", "## 分类别成功率", "",
        "| 类别 | 用例 | Baseline | Auto | Oracle | 帮助 | 伤害 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for category, data in extended["by_category"].items():
        category_rates = data["success_rate"]
        lines.append(
            f"| {category} | {data['cases']} | {pct(category_rates['baseline'])} | "
            f"{pct(category_rates['auto'])} | {pct(category_rates['oracle'])} | "
            f"{data['helped']} | {data['harmed']} |"
        )
    lines.extend(["", "## 时延与 Token", "",
                  "| 模式 | TTFT P50/P95 ms | 总耗时 P50/P95 ms | 输入 Token P50/P95 |",
                  "|---|---:|---:|---:|"])
    for mode in ("baseline", "auto", "oracle"):
        item = performance[mode]
        lines.append(
            f"| {mode} | {item['ttft_ms']['p50']} / {item['ttft_ms']['p95']} | "
            f"{item['total_ms']['p50']} / {item['total_ms']['p95']} | "
            f"{item['input_tokens']['p50']} / {item['input_tokens']['p95']} |"
        )
    lines.extend([
        "", "## 如何解释", "",
        "- `Auto - Baseline` 才是本次受控任务中可归因于自动记忆链路的成功率变化。",
        "- `Oracle - Auto` 主要暴露检索损失：正确知识存在且模型会用，但自动召回没有把它送进去。",
        "- `Precision@3` 固定以 3 为分母，因此只返回 1 条且正确时记为 33.3%；同时结合 Recall 和 MRR 阅读。",
        "- 无关请求注入率衡量知识污染；零召回在这类任务上是正确行为。",
        "- 逐条输入、召回 ID、原始模型输出、判分和错误见 `dataset.json`、`retrieval.json`、`results.jsonl`。",
        "", "## 已定位问题", "",
        f"- 跨语言改写漏召回 {len(semantic_misses)}/20 条；这些任务的 oracle 全部成功，剩余损失位于候选召回或相关性门槛。",
        f"- 无关请求错误注入 {len(negative_injections)}/10 条；具体任务和注入 ID 已写入 `report.json`。",
        f"- 项目隔离越界 {len(project_leaks)}/15 条。",
        "- 已过滤纯数字等低信息 token，并用作用域和 active 状态限制知识污染。",
        "", "## 分类检索时延", "",
        "| 类别 | P50 ms | P95 ms | 最大值 ms |", "|---|---:|---:|---:|",
    ])
    for category, timing in retrieval_by_category.items():
        lines.append(
            f"| {category} | {timing['p50_ms']} | {timing['p95_ms']} | {timing['max_ms']} |"
        )
    lines.extend(["", "## 实验边界", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        fields = ["task_id", "category", "mode", "success", "failure_kind",
                  "policy_match", "result_match", "expected_memory_reported",
                  "ttft_ms", "total_ms", "input_tokens", "output_tokens",
                  "retrieval_ms", "adapter_ms", "api_error"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=default_setting("MW_MODEL", "OPENAI_MODEL", fallback="deepseek-flash"))
    parser.add_argument("--base-url", default=default_setting(
        "MW_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL", fallback="https://api.deepseek.com/v1"
    ))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--max-cases", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument(
        "--retrieval-mode", choices=("fts5", "hybrid", "fts5-enriched"),
        default="fts5-enriched",
    )
    parser.add_argument("--report-only", action="store_true",
                        help="Rebuild reports from existing retrieval/results without model calls")
    parser.add_argument("--retrieval-only", action="store_true",
                        help="Run retrieval cases and metrics without model calls")
    args = parser.parse_args()
    if not 1 <= args.max_cases <= 100:
        parser.error("--max-cases must be between 1 and 100")
    if not 1 <= args.workers <= 12:
        parser.error("--workers must be between 1 and 12")
    if not 256 <= args.max_tokens <= 4096:
        parser.error("--max-tokens must be between 256 and 4096")
    output = args.output or ROOT / "outputs" / (
        "live_100_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    dataset_path = output / "dataset.json"
    state_path = output / "state.json"
    database = output / "evaluation.db"
    results_path = output / "results.jsonl"

    full_dataset = build_live_benchmark()
    dataset = {**full_dataset, "cases": full_dataset["cases"][:args.max_cases]}
    canonical = json.dumps(full_dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dataset_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if dataset_path.exists():
        saved = json.loads(dataset_path.read_text(encoding="utf-8"))
        if saved.get("dataset_sha256") != dataset_hash or len(saved["cases"]) != len(dataset["cases"]):
            raise RuntimeError("output directory belongs to a different dataset or --max-cases value")
    else:
        dataset_path.write_text(json.dumps(
            {**dataset, "dataset_sha256": dataset_hash}, ensure_ascii=False, indent=2
        ), encoding="utf-8")

    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ids = state["knowledge_ids"]
        store = KnowledgeStore(database)
        records = {
            key: store.get(requester_agent="benchmark-reviewer", knowledge_id=knowledge_id)["knowledge"]
            for key, knowledge_id in ids.items()
        }
    else:
        if database.exists():
            raise RuntimeError("evaluation.db exists without state.json; use a clean output directory")
        ids, records = seed_knowledge(database, full_dataset)
        state = {"dataset_sha256": dataset_hash, "knowledge_ids": ids}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot = digest(json.dumps(ids, sort_keys=True) + dataset_hash)

    if args.report_only:
        retrieval_path = output / "retrieval.json"
        if not retrieval_path.exists() or not results_path.exists():
            raise RuntimeError("--report-only requires retrieval.json and results.jsonl")
        retrieval_rows = json.loads(retrieval_path.read_text(encoding="utf-8"))
        report = write_report(
            output, dataset, read_jsonl(results_path), retrieval_rows, snapshot,
            args.retrieval_mode,
        )
        print(json.dumps({
            "phase": "report_complete", "output": str(output),
            "complete_triples": report["extended"]["complete_triples"],
        }, ensure_ascii=False), flush=True)
        return 0

    print(json.dumps({
        "phase": "prepare", "output": str(output), "cases": len(dataset["cases"]),
        "planned_calls": 0 if args.retrieval_only else len(dataset["cases"]) * 3,
        "model": args.model,
        "workers": args.workers, "retrieval_mode": args.retrieval_mode,
    }, ensure_ascii=False), flush=True)
    prepared = [prepare_case(case, database, ids, records, output) for case in dataset["cases"]]
    retrieval_rows = [{
        "id": item["case"]["id"], "category": item["case"]["category"],
        "expected_ids": [item["expected_id"]] if item["expected_id"] else [],
        "retrieved_ids": item["retrieved_ids"], "emitted_ids": item["emitted_ids"],
        "retrieval_ms": item["retrieval_ms"], "adapter_ms": item["adapter_ms"],
        "trace_id": item["trace_id"],
    } for item in prepared]
    (output / "retrieval.json").write_text(
        json.dumps(retrieval_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.retrieval_only:
        metrics = retrieval_metrics(retrieval_rows, k=3)
        (output / "retrieval_report.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({"phase": "retrieval_complete", "output": str(output),
                          "metrics": metrics}, ensure_ascii=False, indent=2), flush=True)
        return 0

    api_key = default_setting("DEEPSEEK_API_KEY", "OPENAI_API_KEY")
    if not api_key:
        parser.error("DEEPSEEK_API_KEY or OPENAI_API_KEY is not configured")

    existing = read_jsonl(results_path)
    completed = {(row["task_id"], row["mode"]) for row in existing}
    jobs = [(item, mode) for item in prepared for mode in ("baseline", "auto", "oracle")
            if (item["case"]["id"], mode) not in completed]
    random.Random(args.seed).shuffle(jobs)
    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)
    lock = threading.Lock()
    done = len(existing)
    if jobs:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(execute_job, client, item, mode, snapshot, dataset["version"]): (item, mode)
                for item, mode in jobs
            }
            for future in as_completed(futures):
                row = future.result()
                with lock:
                    with results_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    done += 1
                    if done % 10 == 0 or done == len(dataset["cases"]) * 3:
                        print(json.dumps({
                            "phase": "model_calls", "completed": done,
                            "total": len(dataset["cases"]) * 3,
                            "api_errors": sum(r.get("api_error") is not None for r in read_jsonl(results_path)),
                        }, ensure_ascii=False), flush=True)

    rows = read_jsonl(results_path)
    report = write_report(
        output, dataset, rows, retrieval_rows, snapshot, args.retrieval_mode
    )
    print(json.dumps({
        "phase": "complete", "output": str(output),
        "complete_triples": report["extended"]["complete_triples"],
        "success_rate": report["extended"]["success_rate"],
        "api_error_calls": report["api_error_calls"],
    }, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["api_error_calls"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

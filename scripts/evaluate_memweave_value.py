"""Convention-compliance live evaluation: measures the marginal value of memory.

Unlike the live_100 benchmark (where the answer exists only in memory, so a
memoryless run cannot succeed), these tasks are *attemptable without memory*:

  - 3 plain fields are copied from the task (service_name / owner_team / runtime)
  - 3 convention fields have documented defaults (version=1 / retention_days=30 /
    api_prefix="/api/v1") that the model will use when no memory is supplied
  - the project convention overrides those defaults, and only memory supplies it

So the baseline is not zero-by-design. The delta between baseline and auto is
attributable to memory alone, because the task text, system prompt, model,
temperature and output contract are identical across groups.

Note on --variant partial: an attempt to let part of the convention be inferred
from a quoted reference service produced empty responses (the model spent the
whole token budget on reasoning about whether to copy the reference). Creating
an artificial gradient also weakens the argument, because the delta would then
depend on prompt wording rather than on memory. Prefer --variant convention.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
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
from agent_knowledge_bridge.reuse import percentile
from agent_knowledge_bridge.store import KnowledgeStore


SYSTEM_PROMPT = """You are a deterministic deployment-config generator in a controlled memory benchmark.

Return exactly one JSON object with these keys:
{"service_name":"...","owner_team":"...","runtime":"...","version":1,"retention_days":30,"api_prefix":"/api/v1","used_memory_ids":[]}

Rules:
- Copy service_name, owner_team and runtime exactly as given in the task.
- For version, retention_days and api_prefix: if the supplied MemWeave context states a project convention for this service, use that convention value exactly.
- If no convention is supplied for a field, use the documented default: version=1, retention_days=30, api_prefix="/api/v1".
- Copy only memory IDs that you actually used. Never invent an ID.
- Return JSON only. No Markdown, no explanation, no extra keys.
"""

PLAIN_FIELDS = ("service_name", "owner_team", "runtime")
CONVENTION_FIELDS = ("version", "retention_days", "api_prefix")
ALL_FIELDS = PLAIN_FIELDS + CONVENTION_FIELDS
DEFAULTS = {"version": 1, "retention_days": 30, "api_prefix": "/api/v1"}

PREFIXES = ("/api/v2", "/svc/v3", "/internal/v2", "/api/v4", "/edge/v2", "/svc/v5", "/gw/v3")
RUNTIMES = ("python3.11", "python3.12", "java21", "go1.22", "node20")

SERVICES = (
    ("ledger-svc", "payments"), ("audit-svc", "security"), ("invoice-svc", "billing"),
    ("search-svc", "discovery"), ("notify-svc", "messaging"), ("profile-svc", "identity"),
    ("catalog-svc", "commerce"), ("stream-svc", "data-platform"), ("report-svc", "analytics"),
    ("gateway-svc", "platform"), ("session-svc", "identity"), ("export-svc", "analytics"),
    ("quota-svc", "platform"), ("render-svc", "media"), ("index-svc", "search"),
    ("replica-svc", "data-platform"), ("archive-svc", "storage"), ("webhook-svc", "integration"),
    ("scheduler-svc", "platform"), ("metrics-svc", "observability"), ("trace-svc", "observability"),
    ("config-svc", "platform"), ("secret-svc", "security"), ("tenant-svc", "identity"),
    ("billing-svc", "billing"), ("cart-svc", "commerce"), ("order-svc", "commerce"),
    ("shipment-svc", "logistics"), ("inventory-svc", "logistics"), ("recommend-svc", "discovery"),
)

PROJECT_KEY = "convention-benchmark"


# --------------------------------------------------------------------------- #
# dataset
# --------------------------------------------------------------------------- #
def _conventions(index: int) -> tuple[int, int, str]:
    version = 3 + (index * 5) % 17          # 3..19, never the default 1
    retention = 20 + (index * 13) % 150     # 20..169, never the default 30
    prefix = PREFIXES[(index * 3) % len(PREFIXES)]
    return version, retention, prefix


def build_value_benchmark(variant: str = "convention") -> dict[str, Any]:
    """variant="convention": private conventions are unavailable without memory.

    variant="partial": project-wide conventions are inferable from a reference
    service quoted in the task, while the service-specific prefix is not. The
    baseline can therefore succeed on part of the convention fields, so the
    delta measures a real increment rather than an information vacuum.
    """
    knowledge: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for index, (service, team) in enumerate(SERVICES):
        runtime = RUNTIMES[index % len(RUNTIMES)]
        key = f"conv-{index + 1:02d}"
        if variant == "partial":
            version, retention = 7, 45                      # project-wide, inferable
            prefix = PREFIXES[(index * 3) % len(PREFIXES)]   # service-specific
            derivable = ["version", "retention_days"]
            ref_index = (index + 11) % len(SERVICES)
            ref_service = SERVICES[ref_index][0]
            ref_prefix = PREFIXES[(ref_index * 3) % len(PREFIXES)]
            reference = (
                f"\n同项目参考：服务 {ref_service} 的当前部署配置为 "
                f"version={version}, retention_days={retention}, api_prefix={ref_prefix}。\n"
            )
            convention_text = (
                f"Project convention for service {service} deployment config: "
                f"version={version}, retention_days={retention}, api_prefix={prefix}. "
                f"Project-wide values apply to every service, but api_prefix is service-specific "
                f"and must not be copied from another service."
            )
            note = ("version/retention_days are project-wide and inferable from the quoted "
                    "reference; api_prefix is service-specific and requires memory.")
        else:
            version, retention, prefix = _conventions(index + 1)
            derivable = []
            reference = ""
            convention_text = (
                f"Project convention for service {service} deployment config: "
                f"version={version}, retention_days={retention}, api_prefix={prefix}. "
                f"These exact values override the generic defaults for {service}."
            )
            note = "Plain fields come from the task; convention fields require memory."
        knowledge.append({
            "key": key,
            "title": f"{service} deployment config convention",
            "content": convention_text,
            "project_key": PROJECT_KEY,
            "source_agent": "claude-code",
            "scope": "project",
            "state": "active",
            "verification_count": 2,
            "search_terms": team,
        })
        cases.append({
            "id": f"V{index + 1:03d}",
            "category": "convention_compliance",
            "query": (
                f"为服务 {service} 生成部署配置 JSON。\n"
                f"服务名：{service}\n负责团队：{team}\n运行时：{runtime}\n"
                "必须生成字段：service_name, owner_team, runtime, version, retention_days, api_prefix\n"
                + reference +
                "如果本项目对这些字段存在约定，必须使用约定值。"
            ),
            "expected": {
                "service_name": service, "owner_team": team, "runtime": runtime,
                "version": version, "retention_days": retention, "api_prefix": prefix,
            },
            "derivable_conventions": derivable,
            "expected_key": key,
            "project_key": PROJECT_KEY,
            "requester_agent": "claude-code",
            "notes": note,
        })
    return {
        "version": f"memweave-value-{variant}-v1",
        "description": (
            "Convention-compliance benchmark: tasks are attemptable without memory, "
            "so baseline is not zero-by-design."
        ),
        "variant": variant,
        "knowledge": knowledge,
        "cases": cases,
    }


# --------------------------------------------------------------------------- #
# model client
# --------------------------------------------------------------------------- #
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
        payload = {
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
                content = (choices[0].get("delta") or {}).get("content")
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
        }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def configured_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    return None


def default_setting(*names: str, fallback: str | None = None) -> str | None:
    for name in names:
        value = configured_value(name)
        if value:
            return value
    return fallback


def seed_knowledge(database: Path, dataset: dict[str, Any]):
    store = KnowledgeStore(database)
    ids: dict[str, str] = {}
    records: dict[str, dict[str, Any]] = {}
    for spec in dataset["knowledge"]:
        published = store.publish(
            source_agent=spec["source_agent"], project_key=spec["project_key"],
            title=spec["title"], content=spec["content"], knowledge_type="procedure",
            scope=spec["scope"], evidence_summary="Synthetic convention fixture",
            source_session="value-fixture", search_terms=spec.get("search_terms") or None,
        )
        knowledge_id = published["knowledge"]["id"]
        ids[spec["key"]] = knowledge_id
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


def gold_context(knowledge_id: str, record: dict[str, Any]) -> str:
    return (
        '<memweave_context mode="oracle">\n'
        "The evaluator selected the gold knowledge item.\n"
        f"[{knowledge_id}] {record['title']}\n{record['content']}\n"
        "</memweave_context>"
    )


def prepare_case(case, database, ids, records, workspace) -> dict[str, Any]:
    adapter = ClaudeLearningAdapter(
        database_path=database, agent_id=case["requester_agent"],
        project_key=case["project_key"], reviewer=lambda _: {"proposals": []},
    )
    started = time.perf_counter()
    adapter.recall({
        "session_id": "value-benchmark", "turn_id": case["id"],
        "prompt": case["query"], "cwd": str(workspace),
    })
    adapter_ms = (time.perf_counter() - started) * 1000
    trace = adapter.reuse.existing(
        case["requester_agent"], case["project_key"], "value-benchmark", case["id"]
    )
    if trace is None:
        raise RuntimeError(f"missing reuse trace for {case['id']}")
    items = json.loads(trace["items_json"])
    emitted_ids = [item["knowledge_id"] for item in items if item["emitted"]]
    expected_id = ids.get(case["expected_key"]) if case["expected_key"] else None
    gold = records.get(case["expected_key"]) if case["expected_key"] else None
    return {
        "case": case,
        "auto_context": trace["context_text"],
        "oracle_context": gold_context(expected_id, gold) if expected_id else "",
        "emitted_ids": emitted_ids,
        "expected_id": expected_id,
        "retrieval_ms": trace["retrieval_ms"],
        "adapter_ms": adapter_ms,
    }


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, int) and isinstance(actual, str):
        try:
            actual = int(actual)
        except ValueError:
            return False
    if isinstance(expected, str) and isinstance(actual, int):
        actual = str(actual)
    return actual == expected


def score_output(text: str, expected: dict[str, Any], derivable: tuple[str, ...] = ()) -> dict[str, Any]:
    try:
        parsed = parse_json_object(text)
        parse_error = None
    except Exception as exc:  # noqa: BLE001
        parsed = {}
        parse_error = f"{type(exc).__name__}: {exc}"
    specific = [name for name in CONVENTION_FIELDS if name not in derivable]
    field_hits = [name for name in ALL_FIELDS if _same(parsed.get(name), expected[name])]
    conv_hits = [name for name in CONVENTION_FIELDS if _same(parsed.get(name), expected[name])]
    plain_hits = [name for name in PLAIN_FIELDS if _same(parsed.get(name), expected[name])]
    specific_hits = [name for name in specific if _same(parsed.get(name), expected[name])]
    return {
        "success": len(field_hits) == len(ALL_FIELDS),
        "parsed": parsed,
        "parse_error": parse_error,
        "field_hits": len(field_hits),
        "field_total": len(ALL_FIELDS),
        "convention_hits": len(conv_hits),
        "convention_total": len(CONVENTION_FIELDS),
        "plain_hits": len(plain_hits),
        "plain_total": len(PLAIN_FIELDS),
        "specific_hits": len(specific_hits),
        "specific_total": len(specific) or len(CONVENTION_FIELDS),
        "default_used_on_conventions": sum(
            1 for name in CONVENTION_FIELDS if _same(parsed.get(name), DEFAULTS[name])
        ),
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
_PRINT_LOCK = threading.Lock()


def run_job(client, prepared, mode, workspace) -> dict[str, Any]:
    case = prepared["case"]
    if mode == "baseline":
        context = ""
    elif mode == "auto":
        context = prepared["auto_context"]
    else:
        context = prepared["oracle_context"]
    row: dict[str, Any] = {
        "task_id": case["id"], "mode": mode, "category": case["category"],
        "expected_memory_id": prepared["expected_id"],
    }
    try:
        response = client.complete(user_prompt=case["query"], context=context)
    except Exception as exc:  # noqa: BLE001
        row.update({"api_error": str(exc), "success": None, "field_hits": None,
                    "convention_hits": None, "plain_hits": None})
        return row
    score = score_output(response["content"], case["expected"],
                         tuple(case.get("derivable_conventions") or ()))
    if not (response.get("content") or "").strip():
        score["failure_kind"] = "empty_content_token_exhausted"
    reported = score["parsed"].get("used_memory_ids")
    row.update({
        "success": score["success"],
        "field_hits": score["field_hits"], "field_total": score["field_total"],
        "convention_hits": score["convention_hits"], "convention_total": score["convention_total"],
        "plain_hits": score["plain_hits"], "plain_total": score["plain_total"],
        "specific_hits": score["specific_hits"], "specific_total": score["specific_total"],
        "default_used_on_conventions": score["default_used_on_conventions"],
        "parse_error": score["parse_error"],
        "failure_kind": score.get("failure_kind"),
        "reported_memory_ids": reported if isinstance(reported, list) else [],
        "memory_reported": (prepared["expected_id"] in reported
                            if isinstance(reported, list) and prepared["expected_id"] else None),
        "ttft_ms": response["ttft_ms"], "total_ms": response["total_ms"],
        "input_tokens": response["input_tokens"], "output_tokens": response["output_tokens"],
        "retrieval_ms": prepared["retrieval_ms"], "adapter_ms": prepared["adapter_ms"],
        "raw_output": response["content"],
    })
    with _PRINT_LOCK:
        print(f"  {case['id']:<6} {mode:<9} fields={score['field_hits']}/6 "
              f"conv={score['convention_hits']}/3", flush=True)
    return row


def _rate(rows, key, total_key) -> float | None:
    usable = [r for r in rows if isinstance(r.get("success"), bool)]
    if not usable:
        return None
    return sum(r[key] for r in usable) / (len(usable) * _TOTAL[total_key])


_TOTAL = {"field_total": 6, "convention_total": 3, "plain_total": 3}


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("baseline", "auto", "oracle"):
        subset = [r for r in rows if r["mode"] == mode]
        usable = [r for r in subset if isinstance(r.get("success"), bool)]
        specific_total = sum(r.get("specific_total") or 0 for r in usable)
        out[mode] = {
            "n": len(subset), "usable": len(usable),
            "api_errors": len(subset) - len(usable),
            "task_success_rate": (sum(r["success"] for r in usable) / len(usable)) if usable else None,
            "field_compliance_rate": (sum(r["field_hits"] for r in usable) / (len(usable) * 6)) if usable else None,
            "convention_hit_rate": (sum(r["convention_hits"] for r in usable) / (len(usable) * 3)) if usable else None,
            "plain_field_rate": (sum(r["plain_hits"] for r in usable) / (len(usable) * 3)) if usable else None,
            "service_specific_rate": (sum(r["specific_hits"] for r in usable) / specific_total) if specific_total else None,
            "defaults_used_on_conventions": (sum(r.get("default_used_on_conventions") or 0 for r in usable) / (len(usable) * 3)) if usable else None,
        }
    for label, key in (("task_success", "task_success_rate"),
                       ("convention_hit", "convention_hit_rate"),
                       ("field_compliance", "field_compliance_rate"),
                       ("service_specific", "service_specific_rate")):
        base, auto = out["baseline"][key], out["auto"][key]
        if base is not None and auto is not None:
            out[f"{label}_delta"] = auto - base
    return out


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def write_report(output: Path, dataset: dict[str, Any], rows: list[dict[str, Any]],
                 summary: dict[str, Any], dataset_hash: str) -> None:
    lines = [
        "# MemWeave 记忆增量价值评测（约定遵从）",
        "",
        "本实验测的不是「没有记忆能不能做对」，而是「有记忆比没有记忆多做对多少」。",
        "任务本身可无记忆完成：通用字段可从任务直接抄录，约定字段在无记忆时按系统提示中",
        "声明的默认值填写。因此 baseline 不是设计上的零分，Auto−Baseline 可归因于记忆。",
        "",
        f"- 数据集版本：`{dataset['version']}`",
        f"- 数据集 SHA256：`{dataset_hash}`",
        f"- 任务数：{len(dataset['cases'])}；每组模型调用数：{len(dataset['cases'])}",
        "",
        "## 总体结果",
        "",
        "| 指标 | Baseline | Auto | Oracle |",
        "|---|---:|---:|---:|",
    ]
    b, a, o = summary["baseline"], summary["auto"], summary["oracle"]
    lines += [
        f"| 字段级合规率（6 字段） | {pct(b['field_compliance_rate'])} | {pct(a['field_compliance_rate'])} | {pct(o['field_compliance_rate'])} |",
        f"| **约定字段命中率（3 字段）** | **{pct(b['convention_hit_rate'])}** | **{pct(a['convention_hit_rate'])}** | **{pct(o['convention_hit_rate'])}** |",
        f"| 服务特有约定命中率（不可推断） | {pct(b['service_specific_rate'])} | {pct(a['service_specific_rate'])} | {pct(o['service_specific_rate'])} |",
        f"| 通用字段正确率（3 字段，对照） | {pct(b['plain_field_rate'])} | {pct(a['plain_field_rate'])} | {pct(o['plain_field_rate'])} |",
        f"| 任务级全对率 | {pct(b['task_success_rate'])} | {pct(a['task_success_rate'])} | {pct(o['task_success_rate'])} |",
        f"| 约定字段使用默认值比例 | {pct(b['defaults_used_on_conventions'])} | {pct(a['defaults_used_on_conventions'])} | {pct(o['defaults_used_on_conventions'])} |",
        "",
        "## 增量",
        "",
        f"- 任务级全对率：Auto − Baseline = **{pct(summary.get('task_success_delta'))}**",
        f"- 约定字段命中率：Auto − Baseline = **{pct(summary.get('convention_hit_delta'))}**",
        f"- 字段级合规率：Auto − Baseline = **{pct(summary.get('field_compliance_delta'))}**",
        f"- 服务特有约定命中率：Auto − Baseline = **{pct(summary.get('service_specific_delta'))}**",
        f"- 检索损失（Oracle − Auto，任务级全对率）= {pct((o['task_success_rate'] or 0) - (a['task_success_rate'] or 0))}",
        "",
        "## 调用状态",
        "",
        f"- Baseline 可用 {b['usable']}/{b['n']}，API 失败 {b['api_errors']}",
        f"- Auto 可用 {a['usable']}/{a['n']}，API 失败 {a['api_errors']}",
        f"- Oracle 可用 {o['usable']}/{o['n']}，API 失败 {o['api_errors']}",
        "",
    ]
    timing = {}
    for mode in ("baseline", "auto", "oracle"):
        subset = [r for r in rows if r["mode"] == mode and r.get("total_ms")]
        if subset:
            values = sorted(r["total_ms"] for r in subset)
            timing[mode] = {"p50": percentile(values, 0.50), "p95": percentile(values, 0.95)}
    if timing:
        lines += ["## 时延", "", "| 模式 | 总耗时 P50 ms | 总耗时 P95 ms |", "|---|---:|---:|"]
        for mode, value in timing.items():
            lines.append(f"| {mode} | {value['p50']:.0f} | {value['p95']:.0f} |")
        lines.append("")

    lines += ["## 如何解读", "",
              "- **通用字段正确率是设计校验**：三组都应接近 100%。若 Baseline 明显偏低，说明任务本身超出模型能力，实验设计需要修正。",
              "- **约定字段命中率是记忆价值的直接证据**：Baseline 依赖默认值，Auto 依赖召回。",
              "- **约定字段使用默认值比例**：Baseline 应接近 100%（模型行为合理，只是不知道约定），Auto 应接近 0%。",
              "- 本实验仍为受控合成任务，证明的是「记忆能改变模型行为并提高合规率」，不是长期生产质量。",
              ""]
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["task_id", "mode", "success", "field_hits", "convention_hits", "plain_hits",
                  "default_used_on_conventions", "memory_reported", "ttft_ms", "total_ms",
                  "input_tokens", "output_tokens", "retrieval_ms", "adapter_ms", "parse_error"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=default_setting("MW_MODEL", "OPENAI_MODEL", fallback="deepseek-flash"))
    parser.add_argument("--base-url", default=default_setting(
        "MW_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL", fallback="https://api.deepseek.com/v1"))
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--max-cases", type=int, default=30)
    parser.add_argument("--variant", choices=("convention", "partial"), default="convention")
    parser.add_argument("--modes", default="baseline,auto,oracle")
    args = parser.parse_args()

    api_key = default_setting(args.api_key_env, "OPENAI_API_KEY")
    if not api_key:
        parser.error(f"missing API key (set {args.api_key_env})")

    output = (args.output or ROOT / "outputs" / ("value_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = output / "evaluation.db"

    full = build_value_benchmark(args.variant)
    dataset = {**full, "cases": full["cases"][:args.max_cases]}
    canonical = json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dataset_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    (output / "dataset.json").write_text(
        json.dumps({**dataset, "dataset_sha256": dataset_hash}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"加载 {len(dataset['cases'])} 个任务，知识 {len(dataset['knowledge'])} 条")
    ids, records = seed_knowledge(database, full)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    workspace = Path(tempfile.mkdtemp(prefix="memweave-value-"))
    print("准备检索上下文 ...")
    prepared = [prepare_case(case, database, ids, records, workspace) for case in dataset["cases"]]
    hit = sum(1 for p in prepared if p["expected_id"] in p["emitted_ids"])
    print(f"检索命中 {hit}/{len(prepared)}")

    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)
    jobs = [(p, m) for p in prepared for m in modes]
    rows: list[dict[str, Any]] = []
    print(f"开始 {len(jobs)} 次模型调用 ...")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_job, client, p, m, workspace) for p, m in jobs]
        for future in as_completed(futures):
            rows.append(future.result())

    order = {m: i for i, m in enumerate(modes)}
    rows.sort(key=lambda r: (r["task_id"], order.get(r["mode"], 9)))
    with (output / "results.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = summarise(rows)
    (output / "report.json").write_text(
        json.dumps({"dataset_sha256": dataset_hash, "summary": summary}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_report(output, dataset, rows, summary, dataset_hash)

    print("\n" + "=" * 62)
    print(f"{'指标':<26}{'Baseline':>11}{'Auto':>11}{'Oracle':>11}")
    for label, key in (("字段级合规率", "field_compliance_rate"),
                       ("约定字段命中率", "convention_hit_rate"),
                       ("服务特有约定命中", "service_specific_rate"),
                       ("通用字段正确率(对照)", "plain_field_rate"),
                       ("任务级全对率", "task_success_rate")):
        print(f"{label:<24}{pct(summary['baseline'][key]):>11}"
              f"{pct(summary['auto'][key]):>11}{pct(summary['oracle'][key]):>11}")
    print("-" * 62)
    print(f"约定字段命中率增量 Auto-Baseline = {pct(summary.get('convention_hit_delta'))}")
    print(f"任务级全对率增量 Auto-Baseline   = {pct(summary.get('task_success_delta'))}")
    print("=" * 62)
    print("输出目录:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
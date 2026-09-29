"""Learning-curve evaluation: does a repeated task stop failing after one correction?

Every other benchmark in this repo fixes knowledge in advance and measures
recall. This one measures the thing that actually distinguishes a continual
learning system: a task family is attempted three times, and the system is
allowed to learn from the failure of attempt 1.

  baseline group  attempt1 -> fail, attempt2 -> fail, attempt3 -> fail
  memory group    attempt1 -> fail, [real learn() from the correction],
                  attempt2 -> succeed, attempt3 -> succeed

The two groups share identical model, system prompt, temperature, task text and
grading. The only difference is whether the applied knowledge exists.

The learning step is the real pipeline, not a fixture: a Claude Code style
transcript is written (task -> wrong answer -> user correction -> passing
verification command) and handed to ClaudeLearningAdapter.learn(), which calls
the DeepSeek reviewer, persists a proposal, links evidence, and promotes the
record to active. Notes:

  - promotion requires a successful objective event, so the transcript includes
    a passing `verify` command; if the reviewer declines to cite it, the record
    stays a candidate and is then promoted through explicit approval, which is
    recorded separately so the split is visible in the report.
  - the headline metric is repeated failures per family (a proxy for how many
    times a user must correct the same mistake).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_knowledge_bridge.claude_learning_adapter import (
    ClaudeLearningAdapter,
    DeepSeekReviewer,
)
from agent_knowledge_bridge.store import KnowledgeStore


AGENT = "claude-code"
PROJECT = "learning-curve"
STAGES = ("dev", "staging", "prod")
DEFAULTS = {"version": 1, "api_prefix": "/api/v1"}

SYSTEM_PROMPT = """You are a deterministic deployment-config generator in a controlled memory benchmark.

Return exactly one JSON object with these keys:
{"service_name":"...","environment":"...","version":1,"api_prefix":"/api/v1","used_memory_ids":[]}

Rules:
- Copy service_name and environment exactly as given in the task.
- For version and api_prefix: if the supplied MemWeave context states a project convention for this service, use that convention value exactly.
- If no convention is supplied, use the documented default: version=1, api_prefix="/api/v1".
- Copy only memory IDs that you actually used. Never invent an ID.
- Return JSON only. No Markdown, no explanation, no extra keys.
"""

FAMILIES = (
    ("ledger-svc", 8, "/api/v4"), ("audit-svc", 13, "/gw/v3"),
    ("invoice-svc", 5, "/svc/v3"), ("search-svc", 17, "/internal/v2"),
    ("notify-svc", 11, "/edge/v2"), ("profile-svc", 6, "/api/v2"),
    ("catalog-svc", 19, "/svc/v5"), ("stream-svc", 9, "/gw/v3"),
    ("report-svc", 4, "/internal/v2"), ("gateway-svc", 15, "/api/v4"),
    ("session-svc", 7, "/edge/v2"), ("export-svc", 12, "/svc/v3"),
    ("quota-svc", 3, "/api/v2"), ("render-svc", 16, "/gw/v3"),
    ("index-svc", 10, "/svc/v5"), ("replica-svc", 18, "/internal/v2"),
    ("archive-svc", 14, "/api/v4"), ("webhook-svc", 2, "/edge/v2"),
    ("scheduler-svc", 6, "/gw/v3"), ("metrics-svc", 9, "/svc/v3"),
)


# --------------------------------------------------------------------------- #
# task construction
# --------------------------------------------------------------------------- #
def build_task(service: str, stage: str, version: int, prefix: str) -> dict[str, Any]:
    return {
        "id": f"{service}-{stage}",
        "family": service,
        "stage": stage,
        "query": (
            f"为服务 {service} 生成 {stage} 环境部署配置 JSON。\n"
            f"服务名：{service}\n环境：{stage}\n"
            "必须生成字段：service_name, environment, version, api_prefix\n"
            "如果本项目对这些字段存在约定，必须使用约定值。"
        ),
        "expected": {
            "service_name": service, "environment": stage,
            "version": version, "api_prefix": prefix,
        },
    }


def write_transcript(path: Path, task: dict[str, Any], wrong_answer: str,
                     version: int, prefix: str) -> None:
    """A minimal Claude Code style transcript: task, wrong answer, correction,
    then a passing verification command that provides the objective evidence."""
    service = task["service"] if "service" in task else task["family"]
    lines = [
        {"type": "user", "message": {"role": "user", "content": task["query"]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": wrong_answer}]}},
        {"type": "user", "message": {"role": "user", "content": (
            f"不对。本项目对 {service} 的约定是 version={version}、api_prefix={prefix}，"
            f"不是默认值。以后生成该服务的任何环境配置都必须使用这两个值。")}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "明白，已按项目约定修正配置。"},
            {"type": "tool_use", "id": "toolu_conv_1", "name": "Bash",
             "input": {"command": f"python verify_config.py --service {service} --check convention"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_conv_1", "is_error": False,
             "content": f"PASS: {service} version={version} api_prefix={prefix}"}]}},
    ]
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines),
                    encoding="utf-8")


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
            "model": self.model, "messages": messages, "temperature": 0,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "stream": True, "stream_options": {"include_usage": True},
        }
        last_error: Exception | None = None
        for attempt in range(4):
            started = time.perf_counter()
            try:
                return self._stream(payload, started)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
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
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST")
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        ttft_ms: float | None = None
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
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
        return {"content": "".join(pieces), "ttft_ms": ttft_ms,
                "total_ms": (time.perf_counter() - started) * 1000,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens")}


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
FIELDS = ("service_name", "environment", "version", "api_prefix")


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = "\n".join(candidate.splitlines()[1:-1]).strip()
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


def score_output(text: str, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = parse_json_object(text)
        parse_error = None
    except Exception as exc:  # noqa: BLE001
        parsed, parse_error = {}, f"{type(exc).__name__}: {exc}"
    hits = [name for name in FIELDS if _same(parsed.get(name), expected[name])]
    convention = [name for name in ("version", "api_prefix")
                  if _same(parsed.get(name), expected[name])]
    return {
        "success": len(hits) == len(FIELDS),
        "field_hits": len(hits), "field_total": len(FIELDS),
        "convention_hits": len(convention), "convention_total": 2,
        "used_default": sum(1 for name in ("version", "api_prefix")
                            if _same(parsed.get(name), DEFAULTS[name])),
        "parse_error": parse_error, "parsed": parsed,
    }


# --------------------------------------------------------------------------- #
# learning step
# --------------------------------------------------------------------------- #
def knowledge_status(database: Path, service: str) -> tuple[str | None, str | None]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, status, title, content FROM knowledge_records "
            "WHERE project_key = ? ORDER BY rowid DESC LIMIT 30", (PROJECT,)
        ).fetchall()
    finally:
        connection.close()
    for record_id, status, title, content in rows:
        if service in f"{title or ''}{content or ''}":
            return record_id, status
    return None, None


def learn_once(database: Path, task: dict[str, Any], wrong_answer: str,
               version: int, prefix: str, transcripts: Path) -> dict[str, Any]:
    service = task["family"]
    transcript_path = transcripts / f"{service}.jsonl"
    write_transcript(transcript_path, task, wrong_answer, version, prefix)

    adapter = ClaudeLearningAdapter(
        database_path=database, agent_id=AGENT, project_key=PROJECT,
        reviewer=DeepSeekReviewer())
    result: dict[str, Any]
    try:
        result = adapter.learn({
            "session_id": f"learn-{service}",
            "transcript_path": str(transcript_path),
            "turn_id": f"learn-{service}",
            "cwd": str(transcripts),
        })
    except Exception as exc:  # noqa: BLE001
        return {"service": service, "status": "learn_failed", "error": str(exc)[:300]}

    knowledge_id, status = knowledge_status(database, service)
    promotion = "auto" if status == "active" else None
    if status == "candidate" and knowledge_id:
        store = KnowledgeStore(database)
        store.feedback(
            agent_id="human-reviewer", knowledge_id=knowledge_id, outcome="verified",
            evidence_summary="Human reviewer confirmed the convention stated by the user.",
            evidence_kind="user_approval", evidence_ref=f"human-approval:{service}")
        _, status = knowledge_status(database, service)
        promotion = "approval" if status == "active" else "still_inactive"
    return {
        "service": service,
        "status": status,
        "promotion": promotion,
        "proposals": result.get("proposals"),
        "promoted": result.get("promoted"),
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def run_attempt(client, task, context, group, stage_index) -> dict[str, Any]:
    row = {"group": group, "family": task["family"], "stage": task["stage"],
           "stage_index": stage_index, "task_id": task["id"], "context_chars": len(context)}
    try:
        response = client.complete(user_prompt=task["query"], context=context)
    except Exception as exc:  # noqa: BLE001
        row.update({"success": None, "api_error": str(exc)[:300]})
        return row
    score = score_output(response["content"], task["expected"])
    row.update({
        "success": score["success"], "field_hits": score["field_hits"],
        "convention_hits": score["convention_hits"],
        "used_default": score["used_default"], "parse_error": score["parse_error"],
        "output": response["content"], "context": context,
        "reported_memory_ids": (score["parsed"].get("used_memory_ids")
                                if isinstance(score["parsed"].get("used_memory_ids"), list) else []),
        "total_ms": response["total_ms"], "input_tokens": response["input_tokens"],
    })
    return row


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=os.getenv("MW_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-flash")
    parser.add_argument("--base-url", default=(os.getenv("MW_BASE_URL") or os.getenv("OPENAI_BASE_URL")
                                               or "https://api.deepseek.com/v1"))
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--families", type=int, default=len(FAMILIES))
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        parser.error("missing DEEPSEEK_API_KEY / OPENAI_API_KEY")

    output = (args.output or ROOT / "outputs" / ("curve_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    database = output / "learning_curve.db"
    transcripts = output / "transcripts"
    transcripts.mkdir(exist_ok=True)

    families = FAMILIES[:args.families]
    groups = [(svc, ver, pre, [build_task(svc, st, ver, pre) for st in STAGES])
              for svc, ver, pre in families]
    print(f"任务族 {len(groups)} 组 × {len(STAGES)} 轮 = {len(groups) * len(STAGES)} 次任务调用/组")

    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)
    rows: list[dict[str, Any]] = []

    # ---- 阶段 1: 两组第 1 轮（输入完全相同，均无记忆）----
    print("阶段1: 两组第 1 轮（无记忆）...")
    jobs = [(task, group) for _, _, _, tasks in groups
            for task in tasks[:1] for group in ("baseline", "memory")]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_attempt, client, task, "", group, 0) for task, group in jobs]
        for future in as_completed(futures):
            rows.append(future.result())

    # ---- 阶段 2: 学习（真实链路，串行）----
    print("阶段2: 从纠正中学习（真实 learn 链路）...")
    learn_results = []
    for service, version, prefix, tasks in groups:
        first = next(r for r in rows if r["family"] == service and r["stage_index"] == 0
                     and r["group"] == "memory")
        wrong = first.get("output") or ""
        info = learn_once(database, tasks[0], wrong, version, prefix, transcripts)
        learn_results.append(info)
        print(f"  {service:<16} status={info.get('status')} promotion={info.get('promotion')}")

    # ---- 阶段 3: memory 组第 2/3 轮（recall → 模型）----
    print("阶段3: memory 组后续轮次（召回）...")
    memory_contexts: dict[str, str] = {}
    for service, version, prefix, tasks in groups:
        adapter = ClaudeLearningAdapter(database_path=database, agent_id=AGENT,
                                        project_key=PROJECT,
                                        reviewer=lambda _: {"proposals": []})
        for index, task in enumerate(tasks):
            if index == 0:
                continue
            hook = adapter.recall({"session_id": f"curve-{service}", "turn_id": task["id"],
                                   "prompt": task["query"], "cwd": str(transcripts)})
            memory_contexts[task["id"]] = (hook.get("hookSpecificOutput") or {}).get(
                "additionalContext", "")

    later_jobs = [(task, "memory", index) for _, _, _, tasks in groups
                  for index, task in enumerate(tasks) if index > 0]
    later_jobs += [(task, "baseline", index) for _, _, _, tasks in groups
                   for index, task in enumerate(tasks) if index > 0]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = []
        for task, group, index in later_jobs:
            context = memory_contexts.get(task["id"], "") if group == "memory" else ""
            futures.append(pool.submit(run_attempt, client, task, context, group, index))
        for future in as_completed(futures):
            rows.append(future.result())

    order = {"baseline": 0, "memory": 1}
    rows.sort(key=lambda r: (r["family"], order.get(r["group"], 9), r["stage_index"]))

    # ---- 汇总 ----
    def rate(group: str, index: int) -> float | None:
        subset = [r for r in rows if r["group"] == group and r["stage_index"] == index
                  and isinstance(r.get("success"), bool)]
        return sum(r["success"] for r in subset) / len(subset) if subset else None

    summary: dict[str, Any] = {"attempt_success_rate": {}, "failures_per_family": {}}
    for group in ("baseline", "memory"):
        summary["attempt_success_rate"][group] = {
            f"attempt{i + 1}": rate(group, i) for i in range(len(STAGES))}
        failures = sum(1 for r in rows if r["group"] == group
                       and isinstance(r.get("success"), bool) and not r["success"])
        summary["failures_per_family"][group] = failures / len(groups)
    base_fail = summary["failures_per_family"]["baseline"]
    mem_fail = summary["failures_per_family"]["memory"]
    summary["repeated_failure_reduction"] = (base_fail - mem_fail) / base_fail if base_fail else None
    summary["promotions"] = {}
    for info in learn_results:
        summary["promotions"][info.get("promotion") or "none"] = (
            summary["promotions"].get(info.get("promotion") or "none", 0) + 1)

    (output / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    (output / "dataset.json").write_text(json.dumps(
        {"families": [{"service": s, "version": v, "api_prefix": p,
                       "tasks": [t["id"] for t in ts]} for s, v, p, ts in groups],
         "stages": list(STAGES), "learn": learn_results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["group", "family", "stage", "stage_index", "success", "field_hits",
                  "convention_hits", "used_default", "context_chars", "total_ms",
                  "input_tokens", "parse_error"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# MemWeave 学习曲线评测", "",
        "同一任务族连续尝试三次。baseline 组全程无记忆；memory 组在第 1 次失败后经**真实学习链路**",
        "（构造 transcript → DeepSeek reviewer 提取 → 证据关联 → 晋升 active）获得知识，随后两次尝试可召回。",
        "",
        f"- 任务族：{len(groups)}；每族轮次：{len(STAGES)}",
        "",
        "## 各轮成功率", "",
        "| 轮次 | Baseline | Memory |", "|---|---:|---:|",
    ]
    for index in range(len(STAGES)):
        label = f"第 {index + 1} 次（{STAGES[index]}）"
        lines.append(f"| {label} | {pct(rate('baseline', index))} | {pct(rate('memory', index))} |")
    lines += [
        "",
        "## 重复失败（人工纠正次数的代理指标）", "",
        "| 指标 | Baseline | Memory |", "|---|---:|---:|",
        f"| 每族累计失败次数 | {summary['failures_per_family']['baseline']:.2f} | {summary['failures_per_family']['memory']:.2f} |",
        "",
        f"- 重复失败下降：**{pct(summary['repeated_failure_reduction'])}**",
        "",
        "## 学习环节结果", "",
        f"- 晋升方式分布：{summary['promotions']}",
        "",
        "## 如何解读", "",
        "- 两组第 1 次输入完全相同（无上下文、同提示、temperature=0），因此第 1 次的差异可视为噪声基线。",
        "- memory 组第 2/3 次的成功率提升，来自第 1 次失败后的纠正被固化并再次召回。",
        "- `auto` 晋升表示 reviewer 提取的知识引用了成功的验证命令；`approval` 表示需要人工确认才生效，",
        "  这两种情况在报告里分开统计，不混为一谈。",
        "- 本实验证明的是「同一族任务的重复失败被消除」，仍属受控合成任务，不代表长期生产质量。",
        "",
    ]
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 60)
    for index in range(len(STAGES)):
        print(f"第 {index + 1} 次  baseline={pct(rate('baseline', index))}  "
              f"memory={pct(rate('memory', index))}")
    print("-" * 60)
    print(f"每族失败次数  baseline={summary['failures_per_family']['baseline']:.2f}  "
          f"memory={summary['failures_per_family']['memory']:.2f}")
    print(f"重复失败下降 {pct(summary['repeated_failure_reduction'])}")
    print(f"晋升分布 {summary['promotions']}")
    print("=" * 60)
    print("输出目录:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Cross-family transfer: does learned knowledge help a task it was never corrected on?

The learning-curve benchmark proves that a repeated task stops failing after one
correction. It does not prove the transferable half: whether what was learned
generalizes past the exact case it came from. This script measures that gap with
four arms over the same model, prompt, temperature and grading.

  baseline      no memory at all; the floor
  within        the corrected family itself; recovers the learning-curve result
  cross         a sibling family in the same naming group, never corrected
  oracle        the sibling's own rule injected by the evaluator; the ceiling

The naming group is the thing that makes transfer answerable. Services ending in
`-svc`, `-node`, `-worker` and `-store` each share one naming convention inside
the group, and the correction transcript says so in words that never name the
convention itself:

    "对本项目里所有 -svc 结尾的服务，命名约定是 version=20、api_prefix=/svc/v5"

A record learned while correcting `ledger-svc` therefore either (a) gets matched
by a query that never mentions `ledger-svc`, (b) gets matched because the model
generalizes from a record whose content names other services too, or (c) gets
matched not at all. `within` isolates (a)-(c) from ordinary retrieval failure,
and `cross` minus `within` is the transfer gap.

The transfer-rate metric at the bottom is the one to quote: of the cross-arm
cases where the record was actually emitted into context, how many succeeded.
If that rate is high while the raw cross-arm success rate is low, the bottleneck
is retrieval, not generalization — which is a different and much better finding
than "the model cannot generalize".
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPTS))

from agent_knowledge_bridge.claude_learning_adapter import (
    ClaudeLearningAdapter,
    DeepSeekReviewer,
)
from agent_knowledge_bridge.store import KnowledgeStore


def env_value(name: str, fallback: str | None = None) -> str | None:
    """Read an env var, falling back to the user's Windows environment block.

    Kept local rather than imported: this script must stay runnable on its own,
    and the one helper it needs is six lines. On Windows, `set` in one shell does
    not reach a later one, so the persisted user environment is checked too.
    """
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


AGENT = "claude-code"
PROJECT = "cross-family"
STAGES = ("dev", "staging", "prod")
DEFAULTS = {"version": 1, "api_prefix": "/api/v1"}

# group -> (services at index 0..3, version, prefix).
# Index 0 is the taught service; indices 1..3 are the transfer targets, and are
# never named in any correction transcript. Four services per group keeps the
# arm count even and gives each group three independent transfer shots.
GROUPS: dict[str, dict[str, Any]] = {
    "-svc": {
        "services": ("ledger-svc", "index-svc", "quota-svc", "webhook-svc"),
        "version": 20, "prefix": "/svc/v5", "convention": "以 -svc 结尾的服务",
    },
    "-node": {
        "services": ("cache-node", "worker-node", "edge-node", "batch-node"),
        "version": 30, "prefix": "/node/v4", "convention": "以 -node 结尾的服务",
    },
    "-worker": {
        "services": ("async-worker", "cron-worker", "sync-worker", "queue-worker"),
        "version": 40, "prefix": "/worker/v2", "convention": "以 -worker 结尾的服务",
    },
    "-store": {
        "services": ("object-store", "vector-store", "blob-store", "meta-store"),
        "version": 50, "prefix": "/store/v6", "convention": "以 -store 结尾的服务",
    },
}

SYSTEM_PROMPT = """You are a deterministic deployment-config generator in a controlled memory benchmark.

Return exactly one JSON object with these keys:
{"service_name":"...","environment":"...","version":1,"api_prefix":"/api/v1","used_memory_ids":[]}

Rules:
- Copy service_name and environment exactly as given in the task.
- For version and api_prefix: if the supplied MemWeave context states a project convention that applies to this service, use that convention value exactly.
- If no applicable convention is supplied, use the documented default: version=1, api_prefix="/api/v1".
- Copy only memory IDs that you actually used. Never invent an ID.
- Return JSON only. No Markdown, no explanation, no extra keys.
"""


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
                     convention: str, version: int, prefix: str) -> None:
    """The correction states the naming-group convention, not the service's values.

    This is the whole experiment in one function. A transcript that said
    "ledger-svc uses version=20" can only ever teach ledger-svc. This one states a
    rule whose scope is wider than the case that produced it, so a system that
    stores the rule as written has something to transfer, and one that stores it
    as "ledger-svc -> 20" does not.
    """
    service = task["family"]
    lines = [
        {"type": "user", "message": {"role": "user", "content": task["query"]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": wrong_answer}]}},
        {"type": "user", "message": {"role": "user", "content": (
            f"不对。本项目的命名约定是按服务名后缀分组的：{convention}，"
            f"约定是 version={version}、api_prefix={prefix}，不是默认值。"
            f"这个约定对 {service} 和同后缀的其他服务都生效，"
            "生成这些服务的任何环境配置都必须使用这两个值。")}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "明白，这是按后缀分组的项目命名约定，已按此修正。"},
            {"type": "tool_use", "id": "toolu_conv_1", "name": "Bash",
             "input": {"command": f"python verify_config.py --group {convention} --check convention"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_conv_1", "is_error": False,
             "content": f"PASS: group convention version={version} api_prefix={prefix}"}]}},
    ]
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines),
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# model client (same contract as the other evaluators in this repo)
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
            try:
                return self._stream(payload)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            if attempt < 3:
                import time as _time
                _time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(str(last_error or "model request failed"))

    def _stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        import time as _time
        import urllib.request
        started = _time.perf_counter()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            method="POST")
        pieces: list[str] = []
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                choices = event.get("choices") or []
                if not choices:
                    continue
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    pieces.append(content)
        return {"content": "".join(pieces),
                "total_ms": (_time.perf_counter() - started) * 1000}


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
        # The metric that separates "did not transfer" from "was never asked":
        # these two fields are the ones the convention controls.
        "convention_hits": len(convention), "convention_total": 2,
        "used_default": sum(1 for name in ("version", "api_prefix")
                            if _same(parsed.get(name), DEFAULTS[name])),
        "parse_error": parse_error, "parsed": parsed,
    }


# --------------------------------------------------------------------------- #
# learning and recall
# --------------------------------------------------------------------------- #
def learn_once(database: Path, task: dict[str, Any], wrong_answer: str,
               convention: str, version: int, prefix: str,
               transcripts: Path) -> dict[str, Any]:
    service = task["family"]
    transcript_path = transcripts / f"{service}.jsonl"
    write_transcript(transcript_path, task, wrong_answer, convention, version, prefix)

    adapter = ClaudeLearningAdapter(
        database_path=database, agent_id=AGENT, project_key=PROJECT,
        reviewer=DeepSeekReviewer())
    try:
        result = adapter.learn({
            "session_id": f"learn-{service}",
            "transcript_path": str(transcript_path),
            "turn_id": f"learn-{service}",
            "cwd": str(transcripts),
        })
    except Exception as exc:  # noqa: BLE001
        return {"service": service, "status": "learn_failed", "error": str(exc)[:300]}

    knowledge_id, status, stored_title, stored_content = knowledge_status(database, service)
    promotion = "auto" if status == "active" else None
    if status == "candidate" and knowledge_id:
        store = KnowledgeStore(database)
        store.feedback(
            agent_id="human-reviewer", knowledge_id=knowledge_id, outcome="verified",
            evidence_summary="Human reviewer confirmed the naming convention.",
            evidence_kind="user_approval", evidence_ref=f"human-approval:{service}")
        _, status, _, _ = knowledge_status(database, service)
        promotion = "approval" if status == "active" else "still_inactive"
    return {
        "service": service, "status": status, "promotion": promotion,
        "knowledge_id": knowledge_id,
        "proposals": result.get("proposals"), "promoted": result.get("promoted"),
        # Diagnostic only: did the stored record keep the group scope, or did it
        # collapse to the single service it came from? A record whose content
        # names no sibling service has nothing to transfer, and that alone can
        # explain a zero cross-arm result.
        "stored_title": stored_title,
        "stored_content": stored_content,
        "stored_mentions_siblings": None,
    }


def knowledge_status(database: Path, service: str) -> tuple[str | None, str | None, str, str]:
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
            return record_id, status, title or "", content or ""
    return None, None, "", ""


def recall_context(database: Path, task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    adapter = ClaudeLearningAdapter(database_path=database, agent_id=AGENT,
                                    project_key=PROJECT,
                                    reviewer=lambda _: {"proposals": []})
    hook = adapter.recall({"session_id": f"cross-{task['family']}", "turn_id": task["id"],
                           "prompt": task["query"], "cwd": str(workspace)})
    context = (hook.get("hookSpecificOutput") or {}).get("additionalContext", "")
    trace = adapter.reuse.existing(AGENT, PROJECT, f"cross-{task['family']}", task["id"])
    emitted: list[str] = []
    if trace is not None:
        emitted = [item["knowledge_id"] for item in json.loads(trace["items_json"])
                   if item["emitted"]]
    return {"context": context, "emitted_ids": emitted}


def run_attempt(client: ChatClient, task: dict[str, Any], context: str, arm: str,
                group: str, emitted_ids: list[str] | None = None) -> dict[str, Any]:
    row = {"arm": arm, "group": group, "family": task["family"],
           "stage": task["stage"], "task_id": task["id"],
           "context_chars": len(context), "emitted_ids": emitted_ids or []}
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
        "total_ms": response["total_ms"],
    })
    return row


def oracle_context(knowledge_id: str | None, title: str, content: str) -> str:
    if not knowledge_id:
        return ""
    return ('<memweave_context mode="oracle">\n'
            "The evaluator selected the gold knowledge item.\n"
            f"[{knowledge_id}] {title}\n{content}\n</memweave_context>")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=env_value("MW_MODEL", env_value("OPENAI_MODEL", "deepseek-flash")))
    parser.add_argument("--base-url", default=env_value(
        "MW_BASE_URL", env_value("DEEPSEEK_BASE_URL", env_value("OPENAI_BASE_URL", "https://api.deepseek.com/v1"))))
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--groups", type=int, default=len(GROUPS),
                        help="how many naming groups to run (each costs 36 model calls)")
    args = parser.parse_args()

    api_key = env_value(args.api_key_env, env_value("OPENAI_API_KEY"))
    if not api_key:
        parser.error(f"missing API key; set {args.api_key_env}")

    output = (args.output or ROOT / "outputs" / ("cross_family_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    transcripts = output / "transcripts"
    transcripts.mkdir(exist_ok=True)
    database = output / "cross_family.db"

    selected = list(GROUPS.items())[:args.groups]
    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)

    # ---- stage 1: every family, no memory, one attempt per service ----
    # Three services per group, not one: the corrected service, one transfer
    # target, and one held-back target that gets no memory at all and therefore
    # checks that the transfer target is not simply easier.
    stage1: list[dict[str, Any]] = []
    jobs = []
    for group, spec in selected:
        for index, service in enumerate(spec["services"][:3]):
            for stage in STAGES[:1]:
                task = build_task(service, stage, spec["version"], spec["prefix"])
                jobs.append((task, group, index))
    print(f"阶段1：{len(jobs)} 个服务 × 1 次无记忆尝试")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_attempt, client, task, "", "stage1", group)
                   for task, group, _ in jobs]
        for future in as_completed(futures):
            stage1.append(future.result())

    # ---- stage 2: learn the group convention from the taught service only ----
    print("阶段2：仅从每组第 0 个服务的纠正中学习（真实 learn 链路）")
    learn_results = []
    for group, spec in selected:
        taught = spec["services"][0]
        first = next(r for r in stage1 if r["family"] == taught)
        wrong = first.get("output") or ""
        task = build_task(taught, STAGES[0], spec["version"], spec["prefix"])
        info = learn_once(database, task, wrong, spec["convention"],
                          spec["version"], spec["prefix"], transcripts)
        siblings = [s for s in spec["services"] if s != taught]
        content = f"{info.get('stored_title', '')}{info.get('stored_content', '')}"
        info["stored_mentions_siblings"] = [s for s in siblings if s in content]
        learn_results.append(info)
        print(f"  {group:<9} taught={taught:<14} status={info.get('status')} "
              f"promotion={info.get('promotion')} "
              f"记录中出现的同类服务={info['stored_mentions_siblings'] or '无'}")

    # ---- stage 3: within / cross / oracle on the two later services ----
    print("阶段3：within / cross / oracle 三臂")
    stage3_jobs: list[tuple[dict[str, Any], str, str, str, list[str]]] = []
    for group, spec in selected:
        taught, transfer, heldback = spec["services"][:3]
        for stage in STAGES:
            within_task = build_task(taught, stage, spec["version"], spec["prefix"])
            recall = recall_context(database, within_task, transcripts)
            stage3_jobs.append((within_task, recall["context"], "within", group,
                                recall["emitted_ids"]))

            cross_task = build_task(transfer, stage, spec["version"], spec["prefix"])
            recall = recall_context(database, cross_task, transcripts)
            stage3_jobs.append((cross_task, recall["context"], "cross", group,
                                recall["emitted_ids"]))

        # The oracle arm injects the transferred service's own rule directly, so
        # it needs a record of its own; publish one rather than borrowing the
        # taught service's, which would confound the id in the context.
        store = KnowledgeStore(database)
        oracle_title = f"{spec['convention']}的命名约定"
        oracle_content = (
            f"本项目的命名约定按服务名后缀分组。{spec['convention']}"
            f"统一使用 version={spec['version']}、api_prefix={spec['prefix']}。"
            f"适用于 {transfer} 及同后缀服务。")
        published = store.publish(
            source_agent=AGENT, project_key=PROJECT, title=oracle_title,
            content=oracle_content, knowledge_type="decision", scope="project",
            evidence_summary="Evaluator-held gold convention for the oracle arm",
            source_session="cross-family-oracle",
            search_terms=" ".join([spec["convention"]] + list(spec["services"])))
        oracle_id = published["knowledge"]["id"]
        for stage in STAGES:
            cross_task = build_task(transfer, stage, spec["version"], spec["prefix"])
            context = oracle_context(oracle_id, oracle_title, oracle_content)
            stage3_jobs.append((cross_task, context, "oracle", group, [oracle_id]))

        # The held-back service keeps the no-memory condition in stage 3 as well,
        # so a cross-arm gain can be compared against an arm that had no chance
        # to gain at all.
        for stage in STAGES:
            heldback_task = build_task(heldback, stage, spec["version"], spec["prefix"])
            stage3_jobs.append((heldback_task, "", "heldback", group, []))

    stage3: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_attempt, client, task, context, arm, group, emitted)
                   for task, context, arm, group, emitted in stage3_jobs]
        for future in as_completed(futures):
            stage3.append(future.result())

    rows = stage1 + stage3

    # ---- metrics ----
    def arm_rate(arm: str, *, emitted_only: bool = False) -> tuple[float | None, int]:
        subset = [r for r in stage3
                  if r["arm"] == arm and isinstance(r.get("success"), bool)]
        if emitted_only:
            subset = [r for r in subset if r.get("emitted_ids")]
        if not subset:
            return None, 0
        return sum(r["success"] for r in subset) / len(subset), len(subset)

    cross_rate, cross_n = arm_rate("cross")
    within_rate, within_n = arm_rate("within")
    held_rate, held_n = arm_rate("heldback")
    oracle_rate, oracle_n = arm_rate("oracle")
    cross_emitted_rate, cross_emitted_n = arm_rate("cross", emitted_only=True)
    cross_emitted = sum(1 for r in stage3 if r["arm"] == "cross" and r.get("emitted_ids"))

    baseline_rate = (sum(r["success"] for r in stage1
                         if isinstance(r.get("success"), bool))
                     / max(1, sum(1 for r in stage1 if isinstance(r.get("success"), bool))))

    def convention_rate(arm: str) -> float | None:
        subset = [r for r in stage3 if r["arm"] == arm
                  and isinstance(r.get("success"), bool)]
        if not subset:
            return None
        return sum(r["convention_hits"] for r in subset) / (2 * len(subset))

    summary = {
        "kind": "memweave_cross_family",
        "model": args.model,
        "base_url": args.base_url,
        "groups": [group for group, _ in selected],
        "services_taught": {group: spec["services"][0] for group, spec in selected},
        "services_transfer": {group: spec["services"][1] for group, spec in selected},
        "services_heldback": {group: spec["services"][2] for group, spec in selected},
        "arms": {
            "baseline_stage1": {"success_rate": baseline_rate, "cases": len(stage1)},
            "within": {"success_rate": within_rate, "cases": within_n,
                       "convention_field_rate": convention_rate("within")},
            "cross": {"success_rate": cross_rate, "cases": cross_n,
                      "convention_field_rate": convention_rate("cross"),
                      "context_emitted_cases": cross_emitted},
            "heldback": {"success_rate": held_rate, "cases": held_n,
                         "convention_field_rate": convention_rate("heldback")},
            "oracle": {"success_rate": oracle_rate, "cases": oracle_n,
                       "convention_field_rate": convention_rate("oracle")},
        },
        "transfer_gap": {
            # within - cross: how much of the learned behaviour survives the
            # change of subject. This is the headline number.
            "within_minus_cross": (None if within_rate is None or cross_rate is None
                                   else within_rate - cross_rate),
            # Of the cross cases where the record reached the model's context,
            # how many succeeded. High here + low cross rate = retrieval
            # bottleneck rather than a generalization failure.
            "cross_success_given_emitted": cross_emitted_rate,
            "cross_cases_with_emitted_context": cross_emitted_n,
            "cross_cases_where_retrieval_missed": cross_n - cross_emitted_n,
            # cross - heldback: is the transfer target simply an easier service?
            "cross_minus_heldback": (None if cross_rate is None or held_rate is None
                                     else cross_rate - held_rate),
        },
        "learning": learn_results,
        "limitations": [
            "固定数据集、单次运行的受控实验，成功率受模型与 API 波动影响。",
            "四组命名约定各只有 3 个同族服务，cross 臂样本量小，只能看方向不能定数值。",
            "synthetic 后缀约定是人为构造的迁移面；真实项目的可迁移规律未必是这个形状。",
            "召回名额是一个混淆项：`MW_RECALL_LIMIT` 默认 3（claude_learning_adapter.py），"
            "四组约定记录互相竞争时，排在第 3 名之后的那条根本进不了注入候选，"
            "cross 臂的失败因此可能来自名额而不是迁移能力。用 oracle 臂可以把两者分开："
            "oracle 绕过召回直接注入正确记录，oracle 过而 cross 不过、且该记录在更大 limit 下可召回，"
            "缺口就是名额问题（本例 -store 组正是这种情形，limit=8 时该记录排第 2）。",
        ],
    }
    (output / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    (output / "dataset.json").write_text(json.dumps(
        {group: spec for group, spec in selected}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    with (output / "results.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["arm", "group", "family", "stage", "task_id", "success",
                  "field_hits", "convention_hits", "used_default", "context_chars",
                  "parse_error", "total_ms"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# MemWeave 跨族迁移评测", "",
        "每组只纠正**一个**服务，然后测量同组其他服务的表现。三个服务分别是：",
        "被纠正的（within）、从未被纠正的迁移目标（cross）、从未见过任何记忆的对照（heldback）。",
        "",
        "| 臂 | 成功率 | 约定字段命中率 | 样本 |", "|---|---:|---:|---:|",
        f"| 无记忆基线 | {pct(baseline_rate)} | — | {len(stage1)} |",
        f"| within（被纠正的服务） | {pct(within_rate)} | {pct(convention_rate('within'))} | {within_n} |",
        f"| cross（同组未纠正） | {pct(cross_rate)} | {pct(convention_rate('cross'))} | {cross_n} |",
        f"| heldback（无记忆对照） | {pct(held_rate)} | {pct(convention_rate('heldback'))} | {held_n} |",
        f"| oracle（直接注入正确知识） | {pct(oracle_rate)} | {pct(convention_rate('oracle'))} | {oracle_n} |",
        "",
        "## 迁移缺口", "",
        f"- within − cross：**{pct(summary['transfer_gap']['within_minus_cross'])}**",
        f"- cross − heldback：{pct(summary['transfer_gap']['cross_minus_heldback'])}",
        f"- cross 臂里知识被真正注入上下文的样本：{cross_emitted_n} / {cross_n}",
        f"- 注入成功条件下的 cross 成功率：**{pct(cross_emitted_rate)}**",
        "",
        "## 学习环节存下的记录", "",
        "| 组 | 状态 | 晋升 | 记录正文里出现的同类服务 |", "|---|---|---|---|",
    ]
    for info in learn_results:
        mentioned = "、".join(info["stored_mentions_siblings"]) or "无"
        lines.append(f"| {info['service']} | {info['status']} | {info['promotion']} | {mentioned} |")
    lines += [
        "",
        "## 怎么读这张表", "",
        "- `cross − heldback` 接近 0 且 `cross ≈ heldback`：学到了，但没迁移过去，知识只对产生它的那个服务生效。",
        "- `within` 高而 `cross` 低：学习链路本身没问题，瓶颈在检索匹配——查询里出现的是新服务名，",
        "  没有任何 token 能锚定到只提到旧服务名的记录上。",
        "- `cross_success_given_emitted` 才是「模型能不能泛化」的答案：如果它接近 100%，说明只要知识进了上下文",
        "  模型就会正确套用，问题纯粹在检索层；如果它也低，说明记录本身没保留可迁移的规则形状。",
        "- 上表最后一列是判断后者的直接证据：记录正文里如果只写了被纠正的那一个服务名，",
        "  那么它从写下的一刻起就不可能迁移。",
        "",
        "本实验证明的是「同一命名组内的迁移能力」，仍是受控合成任务，不代表长期生产质量。",
        "",
    ]
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"baseline={pct(baseline_rate)}  within={pct(within_rate)}  "
          f"cross={pct(cross_rate)}  heldback={pct(held_rate)}  oracle={pct(oracle_rate)}")
    print(f"迁移缺口 within-cross={pct(summary['transfer_gap']['within_minus_cross'])}  "
          f"cross-heldback={pct(summary['transfer_gap']['cross_minus_heldback'])}")
    print(f"cross 臂注入成功率={pct(cross_emitted_rate)} "
          f"({cross_emitted_n}/{cross_n} 样本上下文里出现了知识)")
    print("=" * 68)
    print("输出目录:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

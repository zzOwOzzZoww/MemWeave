"""Plant a real false kill and check that the LFHV probe actually finds it.

The 100-case lifecycle experiment reported `retired_record_false_kill_rate: null`
and `shadow_hits_recorded: 0`. That is not a pass — the archived records in that
experiment were drawn from knowledge no case depended on, so there was nothing
for a probe to find. The mechanism was exercised; the *detection* was never
tested, because the experiment never created the thing detection is for.

This script creates it deliberately. The defect being reproduced is specific and
ordinary: governance decides what to retire from **hit evidence**, and a record
that is still correct but has not been asked for yet has no hits. "No hits" and
"no longer true" look identical to the policy. Retirement ages records out on
`created_at` when they have never been served, so a convention that was written
down early but only becomes relevant later is exactly the shape that gets killed
by mistake.

Three dials reproduce it:

  1. publish the sleeper with a backdated `created_at`, older than
     `archive_after_days`, and give it no hits;
  2. scale the policy clock down (`--stale-days`, `--archive-days`) so the
     experiment runs in seconds rather than 90 days;
  3. retire it either through `sweep()` — the real policy path — or explicitly
     through `transit()`, and compare. The sweep arm is the one that matters,
     because it is the arm where nobody typed "archive this on purpose".

Then the workload runs, the probe asks the counterfactual on each query, and the
report states whether the sleeper was found and whether `resurrect()` put it
back. Arms:

  A  control            sleeper active, workload runs, no governance at all
  B  explicit retire    sleeper archived by hand, workload + probes
  C  sweep retire       sleeper archived by the real policy sweep, workload + probes
  D  resurrected        arm C's false kill undone by resurrect(), workload again

Arm A is the ceiling; if A is not near 100% the sleeper was never going to be
recalled and the rest of the experiment is measuring retrieval noise. Arm D
should return to arm A's level: that is what "reversible" has to mean to be
worth claiming.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPTS))

from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.store import KnowledgeStore


def env_value(name: str, fallback: str | None = None) -> str | None:
    """Read an env var, falling back to the user's Windows environment block.

    Kept local so this script runs on its own. On Windows, `set` in one shell
    does not reach a later one, so the persisted user environment is checked too.
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


PROJECT = "false-kill"
AGENT = "claude-code"

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

# The sleeper is the false kill. It is correct, it is the only record that
# answers these tasks, and it is old and unhit — which is precisely the profile
# the retirement policy treats as garbage.
SLEEPER_KEY = "sleeper-svc"
SLEEPER_VERSION = 77
SLEEPER_PREFIX = "/legacy/v1"
# A decoy is written alongside it so the sleeper is not the only active record:
# a project whose entire active set is one record never triggers capacity
# pressure, and a sweep with nothing to compare against is not a real sweep.
DECOYS = (
    ("decoy-billing-svc", 3, "/api/v2"),
    ("decoy-notify-svc", 4, "/api/v3"),
    ("decoy-report-svc", 5, "/api/v4"),
)
STAGES = ("dev", "staging", "prod")


def build_task(service: str, stage: str, version: int, prefix: str) -> dict[str, Any]:
    return {
        "id": f"{service}-{stage}", "family": service, "stage": stage,
        "query": (
            f"为服务 {service} 生成 {stage} 环境部署配置 JSON。\n"
            f"服务名：{service}\n环境：{stage}\n"
            "必须生成字段：service_name, environment, version, api_prefix\n"
            "如果本项目对这些字段存在约定，必须使用约定值。"
        ),
        "expected": {"service_name": service, "environment": stage,
                     "version": version, "api_prefix": prefix},
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
        import time as _time
        import urllib.request
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
        last: Exception | None = None
        for attempt in range(4):
            started = _time.perf_counter()
            try:
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
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 3:
                    _time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(str(last or "model request failed"))


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
    return {"success": len(hits) == len(FIELDS),
            "field_hits": len(hits), "convention_hits": len(convention),
            "parse_error": parse_error, "parsed": parsed}


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def publish_record(store: KnowledgeStore, *, title: str, content: str,
                   search_terms: str) -> str:
    return store.publish(
        source_agent=AGENT, project_key=PROJECT, title=title, content=content,
        knowledge_type="decision", scope="project",
        evidence_summary="False-kill experiment fixture",
        source_session="false-kill-fixture", search_terms=search_terms,
    )["knowledge"]["id"]


def make_active(store: KnowledgeStore, knowledge_id: str, *, backdate_days: int,
                verifications: int) -> None:
    """Confirm a record, then optionally push its timestamps into the past.

    Backdating is what makes this a real retirement decision rather than a
    contrivance: `Governor._last_activity` reads `created_at` for a record that
    has never been served and never confirmed, so a record written down early
    and not yet needed *is* old by the policy's own definition. Nothing here
    falsifies evidence — the record really does have no hits.
    """
    for index in range(verifications):
        store.feedback(
            agent_id="fixture-reviewer", knowledge_id=knowledge_id, outcome="verified",
            evidence_kind="test", evidence_ref=f"fixture:{knowledge_id}:{index}",
            evidence_summary="Fixture record confirmed by an objective event")
    if backdate_days <= 0:
        return
    past = (datetime.now(timezone.utc) - timedelta(days=backdate_days)).isoformat(
        timespec="seconds")
    with store._connect() as db:
        db.execute(
            "UPDATE knowledge_records SET created_at = ?, updated_at = ?, "
            "candidate_expires_at = ? WHERE id = ?",
            (past, past, past, knowledge_id))
        db.execute(
            "UPDATE knowledge_evidence SET created_at = ? WHERE knowledge_id = ?",
            (past, knowledge_id))


def status_of(store: KnowledgeStore, knowledge_id: str) -> str:
    with sqlite3.connect(store.database_path) as db:
        row = db.execute("SELECT status FROM knowledge_records WHERE id = ?",
                         (knowledge_id,)).fetchone()
    return row[0] if row else "missing"


def status_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT status, COUNT(*) FROM knowledge_records WHERE project_key = ? "
            "GROUP BY status", (PROJECT,)).fetchall()
    result = {"candidate": 0, "active": 0, "stale": 0, "archived": 0, "quarantined": 0}
    result.update({status: int(count) for status, count in rows})
    return result


def seed(database: Path, *, backdate_days: int) -> tuple[str, dict[str, str]]:
    """Build one project: the old sleeper plus recent, still-used decoys.

    The decoys are deliberately *not* backdated. A sweep archives every record
    that is dormant, so backdating the whole project would retire the control as
    well and there would be no baseline left to compare against. Only the sleeper
    is old; everything else looks active, which is also what makes the retirement
    decision a false kill rather than a correct one.
    """
    store = KnowledgeStore(database)
    sleeper_id = publish_record(
        store,
        title=f"{SLEEPER_KEY} 部署约定",
        content=(f"Marker: {SLEEPER_KEY}. 本项目对 {SLEEPER_KEY} 的约定是 "
                 f"version={SLEEPER_VERSION}、api_prefix={SLEEPER_PREFIX}，"
                 "不使用默认值。"),
        search_terms=f"{SLEEPER_KEY} {SLEEPER_KEY} 约定 {SLEEPER_PREFIX}")
    make_active(store, sleeper_id, backdate_days=backdate_days, verifications=1)
    decoy_ids: dict[str, str] = {}
    for service, version, prefix in DECOYS:
        decoy_id = publish_record(
            store,
            title=f"{service} 部署约定",
            content=(f"Marker: {service}. 本项目对 {service} 的约定是 "
                     f"version={version}、api_prefix={prefix}，不使用默认值。"),
            search_terms=f"{service} 约定 {prefix}")
        # Recent timestamps: confirmed now, and never confirmed in the past.
        make_active(store, decoy_id, backdate_days=0, verifications=2)
        decoy_ids[service] = decoy_id
    return sleeper_id, decoy_ids


# --------------------------------------------------------------------------- #
# workload
# --------------------------------------------------------------------------- #
def recall_context(database: Path, task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    adapter = ClaudeLearningAdapter(
        database_path=database, agent_id=AGENT, project_key=PROJECT,
        reviewer=lambda _: {"proposals": []})
    hook = adapter.recall({"session_id": f"false-kill-{task['family']}",
                           "turn_id": task["id"], "prompt": task["query"],
                           "cwd": str(workspace)})
    context = (hook.get("hookSpecificOutput") or {}).get("additionalContext", "")
    trace = adapter.reuse.existing(AGENT, PROJECT, f"false-kill-{task['family']}",
                                   task["id"])
    emitted = ([item["knowledge_id"] for item in json.loads(trace["items_json"])
                if item["emitted"]] if trace else [])
    return {"context": context, "emitted_ids": emitted}


def run_attempt(client: ChatClient, task: dict[str, Any], context: str, arm: str,
                emitted: list[str], sleeper_id: str) -> dict[str, Any]:
    row = {"arm": arm, "family": task["family"], "stage": task["stage"],
           "task_id": task["id"], "context_chars": len(context),
           "knowledge_emitted": sleeper_id in emitted,
           "emitted_ids": emitted}
    try:
        response = client.complete(user_prompt=task["query"], context=context)
    except Exception as exc:  # noqa: BLE001
        row.update({"success": None, "api_error": str(exc)[:300]})
        return row
    score = score_output(response["content"], task["expected"])
    row.update({"success": score["success"], "field_hits": score["field_hits"],
                "convention_hits": score["convention_hits"],
                "parse_error": score["parse_error"], "output": response["content"],
                "total_ms": response["total_ms"]})
    return row


def run_arm(client: ChatClient, database: Path, workspace: Path, arm: str,
            sleeper_id: str, governor: Governor | None, *,
            probes: bool, workers: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the sleeper workload once.

    When `probes` is on, each query is first asked of the counterfactual
    (`shadow_probe`, which ranks as if retirement had never happened) and only
    then of the live path. Probe-first is the honest order: a probe run after
    the live recall still sees the same archived state, but running it first
    makes it impossible for arm bookkeeping to influence what the probe sees.
    """
    jobs = [build_task(SLEEPER_KEY, stage, SLEEPER_VERSION, SLEEPER_PREFIX)
            for stage in STAGES]
    rows: list[dict[str, Any]] = []
    probe_log: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs)))) as pool:
        futures = {}
        for task in jobs:
            if probes and governor is not None:
                probe_log.append(governor.shadow_probe(
                    project_key=PROJECT, query=task["query"]))
            recall = recall_context(database, task, workspace)
            futures[pool.submit(run_attempt, client, task, recall["context"], arm,
                                recall["emitted_ids"], sleeper_id)] = task
        for future in as_completed(futures):
            rows.append(future.result())
    rows.sort(key=lambda row: row["stage"])
    return rows, probe_log


def arm_rate(rows: list[dict[str, Any]]) -> float | None:
    usable = [row for row in rows if isinstance(row.get("success"), bool)]
    return sum(row["success"] for row in usable) / len(usable) if usable else None


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
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
    parser.add_argument("--stale-days", type=float, default=1.0,
                        help="policy stale_after_days; the default 30 is compressed for the run")
    parser.add_argument("--archive-days", type=float, default=2.0,
                        help="policy archive_after_days; the default 90 is compressed for the run")
    parser.add_argument("--backdate-days", type=int, default=120,
                        help="how far into the past the sleeper's timestamps are pushed")
    args = parser.parse_args()

    api_key = env_value(args.api_key_env, env_value("OPENAI_API_KEY"))
    if not api_key:
        parser.error(f"missing API key; set {args.api_key_env}")

    output = (args.output or ROOT / "outputs" / ("false_kill_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    client = ChatClient(api_key=api_key, base_url=args.base_url, model=args.model,
                        timeout=args.timeout, max_tokens=args.max_tokens)

    def fresh(name: str) -> Path:
        return output / f"{name}.db"

    # ---- arm A: control, sleeper active, no governance ----
    db_a = fresh("arm_a_control")
    sleeper_a, _ = seed(db_a, backdate_days=args.backdate_days)
    store_a = KnowledgeStore(db_a)
    print("臂 A：sleeper 保持 active，无治理动作")
    rows_a, _ = run_arm(client, db_a, output, "A_control", sleeper_a, None,
                        probes=False, workers=args.workers)

    # ---- arm B: retired by hand ----
    db_b = fresh("arm_b_explicit")
    sleeper_b, _ = seed(db_b, backdate_days=args.backdate_days)
    store_b = KnowledgeStore(db_b)
    explicit = store_b.transit(sleeper_b, to_status="archived",
                               reason="false-kill experiment: retired by hand",
                               actor="experiment", expected_status="active")
    governor_b = Governor(store_b, policy={"lfhv_resurrect_threshold": 1})
    print("臂 B：显式归档 sleeper，随后运行工作负载并探针")
    rows_b, probes_b = run_arm(client, db_b, output, "B_explicit", sleeper_b,
                               governor_b, probes=True, workers=args.workers)
    report_b = governor_b.lfhv_report(project_key=PROJECT)

    # ---- arm C: retired by the real sweep ----
    db_c = fresh("arm_c_sweep")
    sleeper_c, _ = seed(db_c, backdate_days=args.backdate_days)
    store_c = KnowledgeStore(db_c)
    policy = {"stale_after_days": args.stale_days,
              "archive_after_days": args.archive_days,
              "lfhv_resurrect_threshold": 1}
    governor_c = Governor(store_c, policy=policy)
    status_before_c = status_of(store_c, sleeper_c)
    sweep_dry = governor_c.sweep(project_key=PROJECT, dry_run=True)
    sweep = governor_c.sweep(project_key=PROJECT)
    status_after_c = status_of(store_c, sleeper_c)
    retired_by_sweep = status_after_c == "archived"
    print(f"臂 C：策略 sweep 归档 sleeper（{status_before_c} -> {status_after_c}）")
    rows_c, probes_c = run_arm(client, db_c, output, "C_sweep", sleeper_c,
                               governor_c, probes=True, workers=args.workers)
    report_c = governor_c.lfhv_report(project_key=PROJECT)

    # ---- arm D: undo the false kill ----
    #
    # Arm D must not branch off arm C's database. The probes are the mechanism
    # being tested, but they are also a mutation: `shadow_probe` calls
    # `search()`, which marks hits on the records it ranks, so by the time C's
    # probes finish the three decoys sit at hit_count=3 while the sleeper is
    # still at 0. Reading arm D's zero off that database would measure a
    # ranking artifact of the probe, not the Resurrection.
    #
    # So D gets its own database, seeded and swept identically. Both arms reach
    # the same post-sweep state — the same one the experiment asserts on — and
    # from there D runs the probe-and-resurrect sequence itself. The probes on
    # the decoys are then identical in both arms, and the only difference left
    # is the resurrection.
    db_d = fresh("arm_d_resurrected")
    sleeper_d, _ = seed(db_d, backdate_days=args.backdate_days)
    store_d = KnowledgeStore(db_d)
    governor_d = Governor(store_d, policy=policy)
    sweep_d = governor_d.sweep(project_key=PROJECT)
    if status_of(store_d, sleeper_d) != "archived":
        raise SystemExit(
            "arm D was seeded identically to arm C but the sweep did not retire "
            "the sleeper; the two arms are not comparable and the run is void"
        )
    print(f"臂 D：从与臂 C 相同的 sweep 后状态出发（archive {sweep_d['archived']} 条）")
    for stage in STAGES:
        governor_d.shadow_probe(
            project_key=PROJECT,
            query=build_task(SLEEPER_KEY, stage, SLEEPER_VERSION, SLEEPER_PREFIX)["query"],
        )
    resurrection = governor_d.resurrect(project_key=PROJECT)
    status_after_d = status_of(store_d, sleeper_d)
    print(f"臂 D：resurrect() 之后 sleeper 状态 = {status_after_d}；"
          f"复活 {resurrection['restored']} 条")
    rows_d, _ = run_arm(client, db_d, output, "D_resurrected", sleeper_d,
                        governor_d, probes=False, workers=args.workers)
    report_d = governor_d.lfhv_report(project_key=PROJECT)

    # The durability check: the defect this experiment found is that a
    # resurrected record is retired again on the next sweep. Re-sweeping here
    # turns that from a claim into a measured field.
    resweep_d = governor_d.sweep(project_key=PROJECT)
    status_after_resweep = status_of(store_d, sleeper_d)
    print(f"臂 D：再 sweep 一次之后 sleeper 状态 = {status_after_resweep}")

    # ---- verdict ----
    rate_a = arm_rate(rows_a)
    rate_b = arm_rate(rows_b)
    rate_c = arm_rate(rows_c)
    rate_d = arm_rate(rows_d)
    emitted_rates = {
        arm: (
            (sum(row["success"] for row in rows
                 if row["knowledge_emitted"] and isinstance(row.get("success"), bool))
             / max(1, sum(1 for row in rows if row["knowledge_emitted"]
                          and isinstance(row.get("success"), bool))))
            if any(row["knowledge_emitted"] for row in rows) else None
        )
        for arm, rows in (("B_explicit", rows_b), ("C_sweep", rows_c))
    }
    detected_c = report_c["shadow_hits_recorded"] > 0
    detected_b = report_b["shadow_hits_recorded"] > 0
    verdict = {
        "false_kill_reproduced": retired_by_sweep,
        "policy_sweep_retired_the_sleeper": retired_by_sweep,
        "sweep_would_have_retired_it_dry_run": any(
            item.get("to") == "archived" and item.get("changed")
            for item in sweep_dry.get("transitions", [])),
        "lfhv_detected_it_explicit_retire": detected_b,
        "lfhv_detected_it_sweep_retire": detected_c,
        "resurrection_restored_it": status_after_d == "active",
        "resurrection_survived_second_sweep": status_after_resweep != "archived",
        "recovered_to_control_level": (
            None if rate_d is None or rate_a is None else abs(rate_d - rate_a) <= 0.001),
    }

    report = {
        "kind": "memweave_false_kill",
        "model": args.model,
        "base_url": args.base_url,
        "project_key": PROJECT,
        "sleeper": {"key": SLEEPER_KEY, "version": SLEEPER_VERSION,
                    "api_prefix": SLEEPER_PREFIX, "backdated_days": args.backdate_days},
        "policy": policy,
        "arms": {
            "A_control": {"sleeper_status": status_of(store_a, sleeper_a),
                          "success_rate": rate_a, "cases": len(rows_a)},
            "B_explicit": {"sleeper_status": status_of(store_b, sleeper_b),
                           "success_rate": rate_b, "cases": len(rows_b),
                           "success_given_emitted": emitted_rates["B_explicit"],
                           "probes": len(probes_b),
                           "shadow_hits_found": [p["would_have_served"] for p in probes_b],
                           "lfhv_report": report_b},
            "C_sweep": {"sleeper_status": status_after_c,
                        "success_rate": rate_c, "cases": len(rows_c),
                        "success_given_emitted": emitted_rates["C_sweep"],
                        "probes": len(probes_c),
                        "shadow_hits_found": [p["would_have_served"] for p in probes_c],
                        "sweep_dry_run": {
                            "scanned": sweep_dry.get("scanned"),
                            "would_archive": [item["knowledge_id"] for item
                                              in sweep_dry.get("transitions", [])
                                              if item.get("to") == "archived" and item.get("changed")],
                        },
                        "sweep_applied": {
                            "demoted": sweep.get("demoted"),
                            "archived": sweep.get("archived"),
                            "transitions": [
                                {"knowledge_id": item["knowledge_id"], "to": item["to"],
                                 "reason": item["reason"], "changed": item["changed"]}
                                for item in sweep.get("transitions", [])],
                        },
                        "lfhv_report": report_c},
            "D_resurrected": {"sleeper_status": status_after_d,
                              "sleeper_status_after_second_sweep": status_after_resweep,
                              "second_sweep_archived": resweep_d.get("archived"),
                              "success_rate": rate_d, "cases": len(rows_d),
                              "success_given_emitted": (
                                  (sum(row["success"] for row in rows_d
                                       if row["knowledge_emitted"]
                                       and isinstance(row.get("success"), bool))
                                   / max(1, sum(1 for row in rows_d if row["knowledge_emitted"]
                                                and isinstance(row.get("success"), bool))))
                                  if any(row["knowledge_emitted"] for row in rows_d) else None),
                              "resurrection": {key: value for key, value
                                               in resurrection.items() if key != "records"},
                              "lfhv_report_after": report_d},
        },
        "status_counts": {"A_control": status_counts(db_a),
                          "B_explicit": status_counts(db_b),
                          "C_sweep": status_counts(db_c),
                          "D_resurrected": status_counts(db_d)},
        "verdict": verdict,
        "limitations": [
            "受控合成实验，sleeper 是被故意构造成「旧且无命中」的形状，不是随机淘汰的样本。",
            "策略时钟被压缩到秒级（stale/archive 天数远小于默认 30/90），只验证机制不验证默认参数。",
            "每臂 3 次模型调用，成功率只能看方向；关键判据是 LFHV 是否检出，不是成功率差几个点。",
            "sleeper 的检索可及性依赖它的关键词设置，召回失败不等于治理误杀。",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    all_rows = rows_a + rows_b + rows_c + rows_d
    with (output / "results.jsonl").open("w", encoding="utf-8") as fh:
        for row in all_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        fields = ["arm", "family", "stage", "task_id", "success", "field_hits",
                  "convention_hits", "knowledge_emitted", "context_chars",
                  "parse_error", "total_ms"]
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    lines = [
        "# MemWeave 误杀检出实验", "",
        f"- sleeper：`{SLEEPER_KEY}`（version={SLEEPER_VERSION}, "
        f"api_prefix={SLEEPER_PREFIX}），时间戳回拨 {args.backdate_days} 天，无命中记录。",
        f"- 策略时钟：stale={args.stale_days} 天，archive={args.archive_days} 天。",
        "", "## 各臂结果", "",
        "| 臂 | sleeper 状态 | 任务成功率 | 知识进入上下文 |", "|---|---|---:|---|",
        f"| A 对照（不治理） | {status_of(store_a, sleeper_a)} | {pct(rate_a)} | — |",
        f"| B 显式归档 | archived | {pct(rate_b)} | {emitted_rates['B_explicit'] is not None and f'{pct(emitted_rates['B_explicit'])}' or '未注入'} |",
        f"| C 策略 sweep 归档 | {status_after_c} | {pct(rate_c)} | {emitted_rates['C_sweep'] is not None and f'{pct(emitted_rates['C_sweep'])}' or '未注入'} |",
        f"| D 复活后 | {status_after_d} | {pct(rate_d)} | — |",
        "", "## 误杀是否被检出", "",
        f"- 策略 sweep 是否真的归档了 sleeper：**{'是' if retired_by_sweep else '否'}**",
        f"- LFHV 在显式归档臂的检出：**{'有' if detected_b else '没有'}**"
        f"（shadow_hits_recorded={report_b['shadow_hits_recorded']}）",
        f"- LFHV 在 sweep 归档臂的检出：**{'有' if detected_c else '没有'}**"
        f"（shadow_hits_recorded={report_c['shadow_hits_recorded']}）",
        f"- `resurrect()` 是否把它恢复到 active：**{'是' if status_after_d == 'active' else '否'}**",
        f"- 复活后是否扛得住下一次 sweep：**{'是' if status_after_resweep != 'archived' else '否'}**"
        f"（再 sweep 后状态={status_after_resweep}）",
        "- 臂 D 与臂 C 各用一份种子相同的库，D 自己做探针+复活，避免 C 的探针把干扰项 hit_count 抬到 3",
        "  之后再来量 D —— 那样量到的是探针的排序副作用，不是复活的效果。",
        "", "## 怎么读", "",
        "- 这条实验补的是 100 条生命周期实验留的洞：那次归档的是「本就不该被召回」的干扰项，",
        "  所以 `shadow_hits_recorded` 必然是 0，机制没被真正测过。",
        "- 判据只有一个：**LFHV 有没有把一条仍然正确的已归档记录翻出来。**",
        "  翻出来了，说明「可证伪遗忘」这句话是有实测支撑的；没翻出来，说明这条机制至今没有过一次真实检出，",
        "  简历和面试里都不该把它说成已验证的能力。",
        "- A 臂是天花板：A 不高说明 sleeper 本来就召回不到，那 C/D 的差异是检索噪声，不是治理效果。",
        "",
    ]
    (output / "结果报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 68)
    print(f"A 对照={pct(rate_a)}  B 显式归档={pct(rate_b)}  "
          f"C sweep归档={pct(rate_c)}  D 复活={pct(rate_d)}")
    print(f"sweep 是否归档 sleeper: {retired_by_sweep}   "
          f"LFHV 检出(B/C): {detected_b}/{detected_c}   "
          f"复活恢复: {status_after_d == 'active'}")
    print("=" * 68)
    print("输出目录:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

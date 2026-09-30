"""One fail-open execution path for native and declarative client integrations."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from agent_knowledge_bridge import runtime_state
from agent_knowledge_bridge.claude_transcript import redact_text, redact_value
from agent_knowledge_bridge.codex_transcript import resolve_transcript_path
from agent_knowledge_bridge.integration_profiles import IntegrationProfile
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from agent_knowledge_bridge.runtime_learning_adapter import RuntimeLearningAdapter, transcript_parser
from agent_knowledge_bridge.store import utc_now


MAX_INPUT_BYTES = 2_000_000
TRANSCRIPT_RESOLVERS = {"codex": resolve_transcript_path}


def pin_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def daemon_is_live(url: str, token: str, timeout: float = 0.5) -> bool:
    request = urllib.request.Request(f"{url.rstrip('/')}/v1/health",
                                     headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            health = json.loads(response.read(65_536))
    except (OSError, urllib.error.URLError, ValueError):
        return False
    return isinstance(health, dict) and health.get("status") == "ok"


def selected_runtime(profile: IntegrationProfile) -> MemWeaveRuntimeClient | None:
    if os.getenv("MW_RUNTIME_MODE") == "local":
        return None
    agent = runtime_state.agent_id(profile.agent_id)
    explicit_url = os.getenv("MW_DAEMON_URL", "").strip()
    for url, token in runtime_state.runtime_candidates():
        if not url or not token:
            continue
        if daemon_is_live(url, token):
            return MemWeaveRuntimeClient(base_url=url, token=token, agent_id=agent,
                project_key=runtime_state.project_key(agent=agent),
                timeout=float(os.getenv("MW_RECALL_TIMEOUT", "2")))
        if url == explicit_url:
            return None
    return None


def _append(profile: IntegrationProfile, suffix: str, record: dict) -> None:
    if not suffix:
        return
    try:
        path = runtime_state.database_path().with_suffix(suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"created_at": utc_now(), "agent_id": runtime_state.agent_id(profile.agent_id), **record}
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact_value(record), ensure_ascii=True) + "\n")
    except (OSError, ValueError):
        pass  # Diagnostics cannot block the client when its disk is unavailable.


def append_error(profile: IntegrationProfile, message: str, payload: dict | None = None) -> None:
    payload = payload or {}
    _append(profile, profile.error_suffix, {"event": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"), "error": redact_text(message, 1000)})


def append_audit(profile: IntegrationProfile, payload: dict, *, status: str, detail: dict | None = None) -> None:
    _append(profile, profile.audit_suffix, {"status": status, "event": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"), "turn_id": payload.get("turn_id"),
        "cwd": str(payload.get("cwd") or "")[:500],
        "transcript_path": str(payload.get("transcript_path") or "")[:1000], **({"detail": detail} if detail else {})})


def read_input() -> dict:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("hook input exceeds the bounded limit")
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    return payload


def run_hook(profile: IntegrationProfile, *, runtime_selector=None, workspace: str | None = None) -> int:
    pin_streams()
    try:
        payload = read_input()
        if workspace:
            _, normalized = profile.normalize(payload)
            cwd = str(normalized.get("cwd") or "")
            if not cwd or not Path(cwd).resolve().is_relative_to(Path(workspace).resolve()):
                print("{}")
                return 0
    except Exception as exc:
        append_error(profile, f"invalid hook input: {exc}")
        print("{}")
        return 0
    return run_hook_input(profile, payload, runtime_selector=runtime_selector)


def run_hook_input(profile: IntegrationProfile, payload: dict, *, runtime_selector=None) -> int:
    output = {}
    normalized = {}
    try:
        if profile.baseline_bypass and os.getenv("MW_LATENCY_BASELINE") == "hook_bypass":
            print("{}")
            return 0
        agent = runtime_state.agent_id(profile.agent_id)
        if not runtime_state.hook_enabled(agent):
            print("{}")
            return 0
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        operation, normalized = profile.normalize(payload)
        if not operation:
            print("{}")
            return 0
        if profile.transcript_resolver:
            normalized["transcript_path"] = TRANSCRIPT_RESOLVERS[profile.transcript_resolver](normalized)
        append_audit(profile, normalized, status="started")
        project = runtime_state.project_key(agent=agent, cwd=str(normalized.get("cwd") or ""))
        runtime = runtime_selector() if runtime_selector else selected_runtime(profile)
        if runtime is not None:
            runtime.project_key = project
        turn = {"session_id": str(normalized.get("session_id") or "unknown-session"),
            "turn_id": normalized.get("turn_id") or None,
            "cwd": str(normalized.get("cwd") or ""),
            "transcript_path": str(normalized.get("transcript_path") or "")}
        if operation == "recall":
            args = {**turn, "prompt": str(normalized.get("prompt") or ""),
                "transcript_format": profile.transcript_format,
                "bind_transcript_boundary": profile.bind_transcript_boundary}
            if runtime:
                result = runtime.recall(**args)
                context = (result.get("hookSpecificOutput") or {}).get("additionalContext", "")
            else:
                adapter = RuntimeLearningAdapter(database_path=runtime_state.database_path(), agent_id=agent,
                    project_key=project, transcript_format=profile.transcript_format,
                    bind_transcript_boundary=profile.bind_transcript_boundary)
                context = adapter.recall_context(args)
            output = profile.encode_context(context)
        elif normalized.get("turn") is not None:
            if turn["transcript_path"]:
                raise ValueError("supply normalized turn or transcript_path, not both")
            if runtime is None:
                raise ValueError("inline learning needs a live Runtime; private chat is never spooled")
            runtime.timeout = min(120.0, max(1.0, float(os.getenv("MW_LEARN_TIMEOUT", "120"))))
            runtime.learn(**turn, turn=normalized["turn"])
        else:
            args = {**turn, "transcript_format": profile.transcript_format}
            if runtime:
                runtime.enqueue_learning(**args)
            else:
                transcript_parser(agent, profile.transcript_format)
                LearningQueue(runtime_state.database_path(), None).submit(
                    {**args, "agent_id": agent, "project_key": project})
        append_audit(profile, normalized, status="completed", detail={"runtime": runtime is not None,
            "context_chars": len(context) if operation == "recall" else 0})
    except Exception as exc:
        append_error(profile, str(exc), normalized)
        append_audit(profile, normalized, status="failed", detail={"error": redact_text(str(exc), 1000)})
    print(json.dumps(output, ensure_ascii=True))
    return 0


def compatibility_main(profile: IntegrationProfile, *, runtime_selector=None,
                       allow_review: bool = False, allow_workspace: bool = False, argv=None) -> int:
    pin_streams()
    parser = argparse.ArgumentParser(description=f"MemWeave {profile.agent_id} integration")
    choices = ("hook", "metrics", "pending", "approve", "reject") if allow_review else ("hook", "metrics")
    parser.add_argument("command", nargs="?", default="hook", choices=choices)
    if allow_workspace:
        parser.add_argument("--workspace")
    if allow_review:
        parser.add_argument("knowledge_id", nargs="?")
        parser.add_argument("--reason")
        parser.add_argument("--status", choices=("candidate", "quarantined", "all"), default="candidate")
    args = parser.parse_args(argv)
    if args.command == "hook":
        return run_hook(profile, runtime_selector=runtime_selector, workspace=getattr(args, "workspace", None))
    store = LearningStore(runtime_state.database_path())
    agent = runtime_state.agent_id(profile.agent_id)
    project = runtime_state.project_key(agent=agent)
    if args.command == "metrics":
        result = store.metrics(project)
    elif args.command == "pending":
        result = store.knowledge.review_queue(requester_agent=agent, project_key=project, status=args.status, limit=100)
    else:
        if not args.knowledge_id or not args.reason:
            parser.error("review requires knowledge_id and --reason")
        result = store.knowledge.feedback(agent_id=agent, project_key=project, knowledge_id=args.knowledge_id,
            outcome="verified" if args.command == "approve" else "rejected", evidence_summary=args.reason,
            evidence_kind="user_approval", evidence_ref=f"manual-review:{utc_now()}")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0

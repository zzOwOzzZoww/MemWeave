from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Codex reads the hook response as UTF-8 JSON.  On Windows the inherited
# console stream can be GBK, and a recalled context containing symbols such as
# "⇾" or "➜" then fails during ``print(json.dumps(...))``.  That failure is
# silent from the user's point of view: the knowledge is retrievable in the
# database, but Codex receives an empty hook response.  Pin both streams to
# UTF-8 before any adapter work starts.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")



from agent_knowledge_bridge import runtime_state
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.claude_transcript import redact_text
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from agent_knowledge_bridge.store import utc_now


def selected_database() -> Path:
    # Shares one resolver with the Claude hook and the daemon.  This hook used
    # to ignore the recorded Runtime state entirely, so a daemon launched with
    # a non-default database left the two agents writing to different files
    # with no error anywhere.
    return runtime_state.database_path()


def selected_project(cwd: str = "") -> str:
    return runtime_state.project_key(cwd=cwd)


def selected_agent() -> str:
    return runtime_state.agent_id("codex")


def selected_runtime() -> MemWeaveRuntimeClient | None:
    if os.getenv("MW_RUNTIME_MODE") == "local":
        return None
    # Candidates arrive newest-Runtime-first, with an explicitly configured
    # endpoint ahead of everything. Probing in that order keeps the hook off the
    # leftovers of an earlier launch after an upgrade or restart.
    for url, token in runtime_state.runtime_candidates():
        if not url or not token:
            continue
        request = urllib.request.Request(
            f"{url.rstrip('/')}/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=0.5) as response:
                health = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            continue
        if isinstance(health, dict) and health.get("status") == "ok":
            return MemWeaveRuntimeClient(
                base_url=url,
                token=token,
                agent_id=selected_agent(),
                project_key=selected_project(),
                timeout=float(os.getenv("MW_RECALL_TIMEOUT", "2")),
            )
    return None


def append_error(message: str, hook_input: dict | None = None) -> None:
    path = selected_database().with_suffix(".codex-hook-errors.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": utc_now(),
        "event": (hook_input or {}).get("hook_event_name"),
        "session_id": (hook_input or {}).get("session_id"),
        "error": redact_text(message, 1000),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def append_audit(hook_input: dict, *, status: str, detail: dict | None = None) -> None:
    """Persist a small, redacted proof that Codex actually invoked the hook.

    This is intentionally separate from the learning tables: a missing row in
    ``knowledge_records`` cannot distinguish "no reusable lesson" from "hook
    never ran".  The audit file answers that operational question directly.
    """
    path = selected_database().with_suffix(".codex-hook-runs.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": utc_now(),
        "status": status,
        "event": hook_input.get("hook_event_name"),
        "session_id": hook_input.get("session_id"),
        "turn_id": hook_input.get("turn_id"),
        "cwd": str(hook_input.get("cwd") or "")[:500],
        "transcript_path": str(hook_input.get("transcript_path") or "")[:1000],
    }
    if detail:
        record["detail"] = detail
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def resolve_transcript_path(hook_input: dict) -> str:
    """Return the rollout file for a Codex Stop event.

    Codex normally supplies ``transcript_path``.  Some desktop builds omit it
    on Stop, however, which used to make the hook record a failed learning run
    even though the rollout was already on disk.  The session id is embedded
    in the first ``session_meta`` record, so a bounded scan can recover the
    exact file without reading or copying transcript contents.
    """
    supplied = str(hook_input.get("transcript_path") or "").strip()
    if supplied and Path(supplied).is_file():
        return supplied
    session_id = str(hook_input.get("session_id") or "").strip()
    if not session_id:
        return supplied
    roots = [Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"]
    candidates: list[tuple[float, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Rollouts are sharded by date; inspect only the recent files and stop
        # at the first matching session_meta record.
        try:
            files = sorted(root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:80]
        except OSError:
            continue
        for path in files:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    first = stream.readline(20000)
                item = json.loads(first)
                payload = item.get("payload") or {}
                if str(payload.get("session_id") or payload.get("id") or "") == session_id:
                    candidates.append((path.stat().st_mtime, path))
            except (OSError, ValueError, TypeError):
                continue
    if candidates:
        return str(max(candidates, key=lambda item: item[0])[1])
    return supplied


def run_hook() -> int:
    try:
        hook_input = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception as exc:
        append_error(f"invalid Codex hook input: {exc}")
        print("{}")
        return 0
    return run_hook_input(hook_input)


def run_hook_input(hook_input: dict) -> int:
    # Explicit experiment-only bypass. Normal sessions never choose a baseline
    # automatically. Interpreter/import cost remains, hence "hook_bypass".
    if os.getenv('MW_LATENCY_BASELINE') == 'hook_bypass':
        print('{}')
        return 0
    if not runtime_state.hook_enabled(selected_agent()):
        print('{}')
        return 0
    hook_input = dict(hook_input)
    resolved = resolve_transcript_path(hook_input)
    if resolved:
        hook_input["transcript_path"] = resolved
    append_audit(hook_input, status="started")
    try:
        event = str(hook_input.get("hook_event_name") or "")
        session_id = str(hook_input.get("session_id") or "unknown-session")
        event_project = selected_project(str(hook_input.get("cwd") or ""))
        runtime = selected_runtime()
        if runtime is not None:
            runtime.project_key = event_project
        if runtime is not None and event == "UserPromptSubmit":
            output = runtime.recall(
                turn_id=hook_input.get("turn_id") or None,
                cwd=str(hook_input.get("cwd") or ""),
                session_id=session_id,
                prompt=str(hook_input.get("prompt") or ""),
                transcript_path=str(hook_input.get("transcript_path") or ""),
            )
        elif runtime is not None and event == "Stop":
            runtime.enqueue_learning(
                turn_id=hook_input.get("turn_id") or None,
                cwd=str(hook_input.get("cwd") or ""),
                session_id=session_id,
                transcript_path=str(hook_input.get("transcript_path") or ""),
                last_assistant_message=str(hook_input.get("last_assistant_message") or ""),
            )
            output = {}
        elif event == "Stop":
            from agent_knowledge_bridge.learning_queue import LearningQueue
            LearningQueue(selected_database(), None).submit({**hook_input, "agent_id": selected_agent(), "project_key": event_project})
            output = {}
        else:
            adapter = CodexLearningAdapter(
                database_path=selected_database(),
                agent_id="codex",
                project_key=event_project,
            )
            if event == "UserPromptSubmit":
                output = adapter.recall(hook_input)
            elif event == "Stop":
                adapter.learn(hook_input)
                output = {}
            else:
                output = {}
        append_audit(
            hook_input,
            status="completed",
            detail={
                "runtime": bool(runtime),
                "context_chars": len(
                    str(
                        (output.get("hookSpecificOutput") or {}).get(
                            "additionalContext", ""
                        )
                    )
                ),
            },
        )
        # Keep the transport ASCII-safe even when a desktop build passes lone
        # UTF-16 surrogates in a prompt or transcript field. Codex decodes the
        # escaped Unicode back to the original context.
        print(json.dumps(output, ensure_ascii=True))
    except Exception as exc:
        append_error(str(exc), hook_input)
        append_audit(hook_input, status="failed", detail={"error": redact_text(str(exc), 1000)})
        # Memory must never block a coding session because the local service or
        # review model is unavailable.
        print("{}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MemWeave Codex Hooks adapter")
    parser.add_argument("command", nargs="?", default="hook", choices=("hook", "metrics"))
    parser.add_argument("--workspace", help="Only process turns from this workspace")
    args = parser.parse_args()
    if args.command == "metrics":
        print(json.dumps(LearningStore(selected_database()).metrics(selected_project()), ensure_ascii=False, indent=2))
        return 0
    if args.workspace:
        try:
            hook_input = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        except (ValueError, OSError):
            print("{}")
            return 0
        cwd = str(hook_input.get("cwd") or "")
        if not cwd or not Path(cwd).resolve().is_relative_to(Path(args.workspace).resolve()):
            print("{}")
            return 0
        return run_hook_input(hook_input)
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())

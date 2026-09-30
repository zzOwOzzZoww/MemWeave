from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path




# Claude Code reads the hook response as UTF-8 JSON.  On Windows the inherited
# console stream can be GBK, and a recalled context containing symbols such as
# "⇒" or "➜" then fails during ``print(json.dumps(...))``.  That failure is
# silent from the user's point of view: the knowledge is retrievable in the
# database, but Claude Code receives an empty hook response and never sees it.
# The Codex hook has pinned its streams since it hit the same wall; this side
# was still exposed, which is what filled ``.hook-errors.jsonl`` with
# "'gbk' codec can't encode character" on UserPromptSubmit.  Pin both streams
# to UTF-8 before any adapter work starts.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from agent_knowledge_bridge import runtime_state
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.claude_transcript import redact_text
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from agent_knowledge_bridge.store import utc_now


def selected_database() -> Path:
    return runtime_state.database_path()


def selected_project(cwd: str = "") -> str:
    return runtime_state.project_key(cwd=cwd, agent=selected_agent())


def _runtime_state() -> dict:
    states = runtime_state.read_runtime_states()
    return states[0] if states else {}


def selected_agent() -> str:
    return runtime_state.agent_id("claude-code")


def selected_runtime() -> MemWeaveRuntimeClient | None:
    if os.getenv("MW_RUNTIME_MODE") == "local":
        return None
    for base_url, token in runtime_state.runtime_candidates():
        if not base_url or not token:
            continue
        client = MemWeaveRuntimeClient(
            base_url=base_url,
            token=token,
            agent_id=selected_agent(),
            project_key=selected_project(),
            timeout=float(os.getenv("MW_RECALL_TIMEOUT", "2")),
        )
        # Probe before committing.  A recorded endpoint whose process has since
        # exited used to be returned unconditionally, so recall failed against
        # a dead port instead of falling through to the next candidate or to
        # the in-process adapter.
        if _daemon_is_live(base_url, token):
            return client
        if os.getenv("MW_DAEMON_URL", "").strip() == base_url:
            # An explicitly configured endpoint is authoritative: if it is
            # down, report that rather than silently using a different Runtime.
            return None
    return None


def _daemon_is_live(base_url: str, token: str, timeout: float = 0.5) -> bool:
    """Return True when ``base_url`` answers an authenticated health check.

    The hook's own timeout is 30s, so the client default of 120s meant a slow
    daemon got the hook killed before it could log why it failed. The recall
    path is a local FTS query: if it has not answered in seconds, something is
    wrong and returning no context is the correct outcome.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            health = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return isinstance(health, dict) and health.get("status") == "ok"


def append_error(message: str, hook_input: dict | None = None) -> None:
    path = selected_database().with_suffix(".hook-errors.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "created_at": utc_now(),
        "event": (hook_input or {}).get("hook_event_name"),
        "session_id": (hook_input or {}).get("session_id"),
        "error": redact_text(message, 1000),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=True) + "\n")


def run_hook() -> int:
    try:
        hook_input = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception as exc:
        append_error(f"invalid hook input: {exc}")
        print("{}")
        return 0
    try:
        if not hook_input.get("turn_id") and hook_input.get("prompt_id"):
            hook_input["turn_id"] = "prompt:" + str(hook_input["prompt_id"])
        if not runtime_state.hook_enabled(selected_agent()):
            print("{}")
            return 0
        event_project = selected_project(str(hook_input.get("cwd") or ""))
        runtime = selected_runtime()
        if runtime is not None:
            runtime.project_key = event_project
        event = hook_input.get("hook_event_name")
        if runtime is not None and event == "UserPromptSubmit":
            output = runtime.recall(
                turn_id=hook_input.get("turn_id") or None,
                cwd=str(hook_input.get("cwd") or ""),
                session_id=str(hook_input.get("session_id") or "unknown-session"),
                prompt=str(hook_input.get("prompt") or ""),
                transcript_path=str(hook_input.get("transcript_path") or ""),
            )
        elif runtime is not None and event == "Stop":
            runtime.enqueue_learning(
                turn_id=hook_input.get("turn_id") or None,
                cwd=str(hook_input.get("cwd") or ""),
                session_id=str(hook_input.get("session_id") or "unknown-session"),
                transcript_path=str(hook_input.get("transcript_path") or ""),
                last_assistant_message=str(
                    hook_input.get("last_assistant_message") or ""
                ),
            )
            output = {}
        elif event == "Stop":
            from agent_knowledge_bridge.learning_queue import LearningQueue
            LearningQueue(selected_database(), None).submit({**hook_input, "agent_id": selected_agent(), "project_key": event_project})
            output = {}
        else:
            adapter = ClaudeLearningAdapter(
                database_path=selected_database(),
                agent_id=selected_agent(),
                project_key=event_project,
            )
            if event == "UserPromptSubmit":
                output = adapter.recall(hook_input)
            elif event == "Stop":
                adapter.learn(hook_input)
                output = {}
            else:
                output = {}
        # Keep the transport ASCII-safe even when the recalled context contains
        # characters the console codec cannot represent.  Claude Code decodes
        # the escaped Unicode back to the original context, so no knowledge is
        # lost and no encoding error can truncate the response.
        print(json.dumps(output, ensure_ascii=True))
    except Exception as exc:
        append_error(str(exc), hook_input)
        print("{}")
    return 0


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2))


def show_pending(status: str) -> int:
    store = LearningStore(selected_database())
    print_json(
        store.knowledge.review_queue(
            requester_agent=selected_agent(),
            project_key=selected_project(),
            status=status,
            limit=100,
        )
    )
    return 0


def submit_review(knowledge_id: str, outcome: str, reason: str) -> int:
    store = LearningStore(selected_database())
    print_json(
        store.knowledge.feedback(
            agent_id=selected_agent(),
            knowledge_id=knowledge_id,
            outcome=outcome,
            evidence_summary=reason,
            evidence_kind="user_approval",
            evidence_ref=f"manual-review:{utc_now()}",
            project_key=selected_project(),
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MemWeave Claude Code learning adapter")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("hook", help="Read one Claude Code hook event from stdin")
    subparsers.add_parser("metrics", help="Show learning and recall metrics")
    pending = subparsers.add_parser("pending", help="List knowledge awaiting review")
    pending.add_argument(
        "--status", choices=("candidate", "quarantined", "all"), default="candidate"
    )
    for command in ("approve", "reject"):
        review = subparsers.add_parser(command)
        review.add_argument("knowledge_id")
        review.add_argument("--reason", required=True)
    args = parser.parse_args()

    if args.command in {None, "hook"}:
        return run_hook()
    if args.command == "metrics":
        print_json(LearningStore(selected_database()).metrics(selected_project()))
        return 0
    if args.command == "pending":
        return show_pending(args.status)
    return submit_review(
        args.knowledge_id,
        "verified" if args.command == "approve" else "rejected",
        args.reason,
    )


if __name__ == "__main__":
    raise SystemExit(main())

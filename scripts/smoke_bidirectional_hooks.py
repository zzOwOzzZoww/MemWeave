from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_knowledge_bridge.store import KnowledgeStore


ROOT = Path(__file__).resolve().parents[1]


def active_knowledge(store: KnowledgeStore, *, agent: str, title: str, marker: str) -> str:
    published = store.publish(
        source_agent=agent,
        project_key="claude-codex-mvp",
        title=title,
        content=f"Use this validated procedure: {marker}.",
        knowledge_type="procedure",
        scope="project",
        evidence_summary="Created by the isolated bidirectional adapter smoke test.",
        source_session=f"{agent}-source",
    )
    knowledge_id = published["knowledge"]["id"]
    store.feedback(
        agent_id=agent,
        knowledge_id=knowledge_id,
        outcome="verified",
        evidence_summary="The isolated objective verifier returned PASS.",
        evidence_kind="test",
        evidence_ref=f"smoke:{agent}:PASS",
    )
    return knowledge_id


def run_hook(script: Path, database: Path, *, event: str, prompt: str) -> dict:
    environment = os.environ.copy()
    environment.update(
        {
            "MW_DB_PATH": str(database),
            "MW_RUNTIME_MODE": "local",
            "MW_PROJECT_KEY": "claude-codex-mvp",
            "PYTHONPATH": str(ROOT / "src"),
            "MW_DAEMON_URL": "",
            "MW_DAEMON_TOKEN": "",
            "DEEPSEEK_API_KEY": "",
        }
    )
    payload = {
        "hook_event_name": event,
        "session_id": f"{event.lower()}-smoke",
        "turn_id": "smoke-" + hashlib.sha256(prompt.encode()).hexdigest()[:12],
        "cwd": str(ROOT),
        "model": "smoke",
        "permission_mode": "default",
        "prompt": prompt,
        "transcript_path": None,
    }
    result = subprocess.run(
        [sys.executable, str(script), "hook"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=environment,
        check=True,
    )
    return json.loads(result.stdout.strip() or "{}")


def context(output: dict) -> str:
    return str(output.get("hookSpecificOutput", {}).get("additionalContext") or "")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="memweave-bidirectional-") as directory:
        database = Path(directory) / "knowledge.db"
        store = KnowledgeStore(database)
        # Native hooks intentionally fail closed unless the Agent was explicitly
        # added to MemWeave. The smoke fixture must model that real product
        # state instead of testing an unregistered database.
        for agent, display_name, adapter_type in (
            ("codex", "Codex", "codex-hook"),
            ("claude-code", "Claude Code", "claude-hook"),
        ):
            store.register_agent(
                agent_id=agent,
                display_name=display_name,
                adapter_type=adapter_type,
                installed=True,
                detected_by=["smoke-test"],
            )
        claude_id = active_knowledge(
            store, agent="claude-code", title="Claude source procedure", marker="CLAUDE_TO_CODEX"
        )
        codex_id = active_knowledge(
            store, agent="codex", title="Codex source procedure", marker="CODEX_TO_CLAUDE"
        )

        codex_output = run_hook(
            ROOT / "scripts" / "codex_learning_hook.py",
            database,
            event="UserPromptSubmit",
            prompt="Apply the Claude source procedure to this Codex task.",
        )
        claude_output = run_hook(
            ROOT / "scripts" / "claude_learning_hook.py",
            database,
            event="UserPromptSubmit",
            prompt="Apply the Codex source procedure to this Claude task.",
        )
        unrelated_output = run_hook(
            ROOT / "scripts" / "codex_learning_hook.py",
            database,
            event="UserPromptSubmit",
            prompt="Unrelated question about office hours.",
        )

        if "CLAUDE_TO_CODEX" not in context(codex_output):
            raise SystemExit("Codex did not recall Claude knowledge")
        if "CODEX_TO_CLAUDE" not in context(claude_output):
            raise SystemExit("Claude did not recall Codex knowledge")
        if unrelated_output != {}:
            raise SystemExit("unrelated prompt received injected knowledge")

        result = {
            "status": "passed",
            "database": str(database),
            "claude_source_knowledge": claude_id,
            "codex_source_knowledge": codex_id,
            "codex_recalled_claude": True,
            "claude_recalled_codex": True,
            "unrelated_prompt_empty": True,
            "mcp_required": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


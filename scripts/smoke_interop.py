from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result) -> dict:
    if result.structuredContent:
        return result.structuredContent
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise RuntimeError("MCP tool returned no JSON object")


@asynccontextmanager
async def agent_session(project: Path, agent_id: str, database_path: Path):
    started = time.perf_counter()
    environment = os.environ.copy()
    environment["AKB_DB_PATH"] = str(database_path)
    parameters = StdioServerParameters(
        command="powershell.exe",
        args=[
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(project / "run-mcp.ps1"),
            "-AgentId",
            agent_id,
            "-ProjectKey",
            "interop-smoke",
        ],
        env=environment,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            startup_ms = round((time.perf_counter() - started) * 1000, 2)
            yield session, startup_ms


async def timed_call(session: ClientSession, tool: str, arguments: dict):
    started = time.perf_counter()
    result = payload(await session.call_tool(tool, arguments))
    elapsed_ms = (time.perf_counter() - started) * 1000
    return result, round(elapsed_ms, 2)


async def main() -> None:
    project = Path(__file__).resolve().parents[1]
    marker = uuid.uuid4().hex[:10].upper()
    with tempfile.TemporaryDirectory() as temp_directory:
        database_path = Path(temp_directory) / "interop.db"

        async with agent_session(project, "claude-code", database_path) as (
            claude,
            claude_startup_ms,
        ):
            claude_tools = sorted(
                tool.name for tool in (await claude.list_tools()).tools
            )
            claude_created, claude_publish_ms = await timed_call(
                claude,
                "knowledge_publish",
                {
                    "title": "Claude Code interop proof",
                    "content": f"CLAUDE_TO_CODEX_{marker}",
                    "knowledge_type": "fact",
                    "evidence_summary": "Created by the deterministic MCP smoke test.",
                },
            )

        async with agent_session(project, "codex", database_path) as (
            codex,
            codex_startup_ms,
        ):
            codex_tools = sorted(tool.name for tool in (await codex.list_tools()).tools)
            codex_queue, codex_queue_ms = await timed_call(
                codex, "knowledge_review_queue", {"status": "candidate"}
            )
            claude_candidate = next(
                item
                for item in codex_queue["results"]
                if f"CLAUDE_TO_CODEX_{marker}" in item["content"]
            )
            claude_feedback, claude_feedback_ms = await timed_call(
                codex,
                "knowledge_feedback",
                {
                    "knowledge_id": claude_candidate["id"],
                    "outcome": "verified",
                    "evidence_summary": "The deterministic smoke assertion matched the unique marker.",
                    "evidence_kind": "test",
                    "evidence_ref": "scripts/smoke_interop.py:claude-to-codex",
                },
            )
            codex_read, codex_search_ms = await timed_call(
                codex, "knowledge_search", {"query": f"CLAUDE_TO_CODEX_{marker}"}
            )
            codex_created, codex_publish_ms = await timed_call(
                codex,
                "knowledge_publish",
                {
                    "title": "Codex interop proof",
                    "content": f"CODEX_TO_CLAUDE_{marker}",
                    "knowledge_type": "fact",
                    "evidence_summary": "Created by the deterministic MCP smoke test.",
                },
            )

        async with agent_session(project, "claude-code", database_path) as (
            claude,
            claude_restart_ms,
        ):
            claude_queue, claude_queue_ms = await timed_call(
                claude, "knowledge_review_queue", {"status": "candidate"}
            )
            codex_candidate = next(
                item
                for item in claude_queue["results"]
                if f"CODEX_TO_CLAUDE_{marker}" in item["content"]
            )
            feedback, feedback_ms = await timed_call(
                claude,
                "knowledge_feedback",
                {
                    "knowledge_id": codex_created["knowledge"]["id"],
                    "outcome": "verified",
                    "evidence_summary": "Claude Code retrieved the Codex record by its unique marker.",
                    "evidence_kind": "test",
                    "evidence_ref": "scripts/smoke_interop.py:codex-to-claude",
                },
            )
            claude_read, claude_search_ms = await timed_call(
                claude, "knowledge_search", {"query": f"CODEX_TO_CLAUDE_{marker}"}
            )

        required = {
            "knowledge_capabilities",
            "knowledge_publish",
            "knowledge_review_queue",
            "knowledge_search",
            "knowledge_get",
            "knowledge_feedback",
        }
        if not required.issubset(claude_tools) or not required.issubset(codex_tools):
            raise RuntimeError("one of the Agent clients did not discover all MCP tools")
        if codex_read["count"] != 1 or not codex_read["results"][0]["cross_agent"]:
            raise RuntimeError("Codex did not retrieve Claude Code knowledge")
        if claude_read["count"] != 1 or not claude_read["results"][0]["cross_agent"]:
            raise RuntimeError("Claude Code did not retrieve Codex knowledge")
        if feedback["knowledge"]["verified_count"] != 1:
            raise RuntimeError("cross-agent verification feedback was not persisted")
        if claude_feedback["knowledge"]["status"] != "active":
            raise RuntimeError("Claude Code candidate was not promoted to active")

        print(
            json.dumps(
                {
                    "interop": "PASS",
                    "storage": "shared-local-sqlite",
                    "mcp_tools": claude_tools,
                    "claude_to_codex": {
                        "knowledge_id": claude_created["knowledge"]["id"],
                        "source_agent": codex_read["results"][0]["source_agent"],
                        "cross_agent": codex_read["results"][0]["cross_agent"],
                    },
                    "codex_to_claude": {
                        "knowledge_id": codex_created["knowledge"]["id"],
                        "source_agent": claude_read["results"][0]["source_agent"],
                        "cross_agent": claude_read["results"][0]["cross_agent"],
                        "verified_count": feedback["knowledge"]["verified_count"],
                    },
                    "latency_ms": {
                        "claude_mcp_startup": claude_startup_ms,
                        "codex_mcp_startup": codex_startup_ms,
                        "claude_mcp_restart": claude_restart_ms,
                        "claude_publish": claude_publish_ms,
                        "codex_search": codex_search_ms,
                        "codex_review_queue": codex_queue_ms,
                        "codex_verify_candidate": claude_feedback_ms,
                        "codex_publish": codex_publish_ms,
                        "claude_review_queue": claude_queue_ms,
                        "claude_search": claude_search_ms,
                        "claude_feedback": feedback_ms,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())

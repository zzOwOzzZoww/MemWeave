from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as error:  # pragma: no cover - install guidance
    # MCP is an optional gateway, so the console script must degrade the same
    # way the daemon does rather than dying with a bare traceback.
    raise SystemExit(
        f"the MemWeave MCP gateway needs the optional 'mcp' extra "
        f"(missing: {error.name}).\n"
        "  pip install 'memweave-runtime[mcp]'\n"
        "MCP is optional: automatic recall, learning and evidence promotion "
        "run through the hooks and do not need it."
    ) from error

from agent_knowledge_bridge.service import KnowledgeBridgeService


service = KnowledgeBridgeService()
mcp = FastMCP(
    name="agent-knowledge-bridge",
    instructions=(
        "Search shared knowledge only when the task depends on prior user, project, or "
        "cross-agent context. Zero results are valid. New knowledge is a candidate until "
        "objective verification promotes it. Do not publish ordinary conversation, secrets, "
        "or unverified guesses as facts."
    ),
)


@mcp.tool(name="knowledge_capabilities")
def knowledge_capabilities() -> dict:
    """Return this bridge process identity and implemented capabilities."""
    return service.capabilities()


@mcp.tool(name="knowledge_publish")
def knowledge_publish(
    title: str,
    content: str,
    knowledge_type: str,
    evidence_summary: str,
    scope: str = "project",
    source_session: str | None = None,
    evidence_speaker: str | None = None,
    search_terms: str | None = None,
    subject_terms: list[str] | None = None,
) -> dict:
    """Publish a durable knowledge candidate under this Agent's identity."""
    return service.publish(
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        evidence_summary=evidence_summary,
        scope=scope,
        source_session=source_session,
        evidence_speaker=evidence_speaker,
        search_terms=search_terms,
        subject_terms=subject_terms,
    )


@mcp.tool(name="knowledge_review_queue")
def knowledge_review_queue(status: str = "candidate", limit: int = 20) -> dict:
    """List candidate or quarantined knowledge awaiting objective review."""
    return service.review_queue(status=status, limit=limit)


@mcp.tool(name="knowledge_search")
def knowledge_search(query: str, limit: int = 5) -> dict:
    """Search active shared knowledge; returning zero records is a normal outcome."""
    return service.search(query=query, limit=limit)


@mcp.tool(name="knowledge_get")
def knowledge_get(knowledge_id: str) -> dict:
    """Read one knowledge record and its evidence history."""
    return service.get(knowledge_id)


@mcp.tool(name="knowledge_feedback")
def knowledge_feedback(
    knowledge_id: str,
    outcome: str,
    evidence_summary: str,
    evidence_kind: str | None = None,
    evidence_ref: str | None = None,
) -> dict:
    """Record use or objective verification; verified/rejected require an evidence reference."""
    return service.feedback(
        knowledge_id=knowledge_id,
        outcome=outcome,
        evidence_summary=evidence_summary,
        evidence_kind=evidence_kind,
        evidence_ref=evidence_ref,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

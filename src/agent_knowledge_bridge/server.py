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
    """Record use only. Decisions belong in a guarded governance preview, not model approval."""
    if outcome != 'used':
        raise ValueError('MCP cannot assert verification or user approval; use knowledge_governance_preview')
    return service.feedback(
        knowledge_id=knowledge_id,
        outcome=outcome,
        evidence_summary=evidence_summary,
        evidence_kind=evidence_kind,
        evidence_ref=evidence_ref,
    )


@mcp.tool(name='knowledge_governance_preview')
def knowledge_governance_preview(actions: list[dict]) -> dict:
    """Preview 1..20 actions after reading knowledge_get evidence. No knowledge is changed.

    Each action: action (approve/quarantine/archive/merge/supersede), knowledge_id,
    reason, target_id (merge/supersede only). Exact candidate duplicates may be
    merged automatically; all other changes require separate operator approval.
    Never treat model confidence or unrelated tool success as verification.
    """
    return service.governance_preview(actions)


@mcp.tool(name='knowledge_governance_plans')
def knowledge_governance_plans(limit: int = 20) -> dict:
    """Read this Agent's governance previews and execution/undo status."""
    return service.governance_plans(limit)


@mcp.tool(name='knowledge_governance_plan')
def knowledge_governance_plan(plan_id: str) -> dict:
    """Read an immutable preview and its current status."""
    return service.governance_plan(plan_id)


@mcp.tool(name='knowledge_governance_apply')
def knowledge_governance_apply(plan_id: str) -> dict:
    """Execute a fresh preview: exact duplicate merge or separately approved actions.

    This tool cannot grant approval. If blocked, ask the user to open the local
    /governance page from the knowledge dashboard and review the preview there.
    """
    return service.governance_apply(plan_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

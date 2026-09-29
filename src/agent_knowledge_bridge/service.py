from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from agent_knowledge_bridge import __version__ as RUNTIME_VERSION
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.agent_registry import discover_agents, hook_configuration, install_native_hook
from agent_knowledge_bridge.paths import default_database_path
from agent_knowledge_bridge.store import KnowledgeStore, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class KnowledgeBridgeService:
    def __init__(
        self,
        *,
        agent_id: str | None = None,
        project_key: str | None = None,
        database_path: Path | None = None,
        clock: Callable[[], str] = utc_now,
        auto_install_hooks: bool = False,
    ) -> None:
        self.agent_id = agent_id or os.getenv("AKB_AGENT_ID", "unknown-agent")
        self.project_key = project_key or os.getenv(
            "AKB_PROJECT_KEY", "claude-codex-mvp"
        )
        selected_path = database_path or default_database_path()
        self.store = KnowledgeStore(selected_path, clock=clock)
        self.governor = Governor(self.store)
        self.auto_install_hooks = auto_install_hooks

    def capabilities(self) -> dict[str, Any]:
        return {
            "service": "memweave-core",
            "version": RUNTIME_VERSION,
            "agent_id": self.agent_id,
            "project_key": self.project_key,
            "storage": "local-sqlite",
            "retrieval": "sqlite-fts5-enriched",
            "automatic_context_injection": True,
            "lifecycle": "candidate/active/stale/archived/quarantined",
            "retirement_audit": "lfhv-shadow-probe",
            "operations": [
                "publish",
                "review_queue",
                "search",
                "get",
                "feedback",
                "governance_report",
                "governance_sweep",
                "governance_resurrect",
                "governance_lfhv",
                "agent_discovery",
                "agent_registry",
            ],
        }

    def discover_agents(self) -> list[dict[str, Any]]:
        """Scan known local Agent installations without reading credentials."""
        discovered = discover_agents()
        registered = {item["agent_id"] for item in self.store.list_agents(include_disabled=True)}
        for item in discovered:
            if item["agent_id"] in registered:
                self.store.update_agent_discovery(item)
            item["hook"] = hook_configuration(item["agent_id"], database_path=self.store.database_path)
            item["learning"] = self.store.latest_learning(item["agent_id"])
        return discovered

    def list_agents(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        agents = self.store.list_agents(include_disabled=include_disabled)
        for agent in agents:
            agent["hook"] = hook_configuration(agent["agent_id"], database_path=self.store.database_path)
            agent["learning"] = self.store.latest_learning(agent["agent_id"])
        return agents

    def register_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        hook_install = None
        if self.auto_install_hooks and agent.get("adapter_type") in {"claude-hook", "codex-hook"}:
            hook_install = install_native_hook(str(agent["agent_id"]))
        registered = self.store.register_agent(**agent)
        if hook_install is not None:
            registered["hook_install"] = hook_install
            registered["hook"] = hook_configuration(registered["agent_id"], database_path=self.store.database_path)
        return registered

    def disable_agent(self, agent_id: str) -> dict[str, Any]:
        return self.store.disable_agent(agent_id)

    def publish(
        self,
        title: str,
        content: str,
        knowledge_type: str,
        evidence_summary: str,
        scope: str = "project",
        source_session: str | None = None,
        evidence_speaker: str | None = None,
        search_terms: str | None = None,
        subject_terms: list[str] | tuple[str, ...] | str | None = None,
    ) -> dict[str, Any]:
        self.store.assert_agent_allowed(self.agent_id)
        return self.store.publish(
            source_agent=self.agent_id,
            project_key=self.project_key,
            title=title,
            content=content,
            knowledge_type=knowledge_type,
            scope=scope,
            evidence_summary=evidence_summary,
            source_session=source_session,
            evidence_speaker=evidence_speaker,
            search_terms=search_terms,
            subject_terms=subject_terms,
        )

    def search(
        self, query: str, limit: int = 5, *, expand_siblings: bool = True
    ) -> dict[str, Any]:
        """Retrieve knowledge; ``expand_siblings=False`` returns the raw ranking.

        The flag exists so a caller can see exactly what the query matched,
        without the records inferred from it. Governance needs that for its
        counterfactual probes, and the retrieval tests use it as the baseline
        that expansion must not disturb.
        """
        self.store.assert_agent_allowed(self.agent_id)
        return self.store.search(
            requester_agent=self.agent_id,
            project_key=self.project_key,
            query=query,
            limit=limit,
            expand_siblings=expand_siblings,
        )

    def review_queue(
        self, status: str = "candidate", limit: int = 20
    ) -> dict[str, Any]:
        self.expire_candidates()
        return self.store.review_queue(
            requester_agent=self.agent_id,
            project_key=self.project_key,
            status=status,
            limit=limit,
        )

    def get(self, knowledge_id: str) -> dict[str, Any]:
        self.store.assert_agent_allowed(self.agent_id)
        return self.store.get(
            requester_agent=self.agent_id,
            knowledge_id=knowledge_id,
            project_key=self.project_key,
        )

    def list_records(
        self,
        status: str = "all",
        limit: int = 100,
        *,
        query: str = "",
        knowledge_type: str = "all",
        scope: str = "all",
        source_agent: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
    ) -> dict[str, Any]:
        self.expire_candidates()
        return self.store.list_records(
            requester_agent=self.agent_id,
            project_key=self.project_key,
            status=status,
            limit=limit,
            query=query,
            knowledge_type=knowledge_type,
            scope=scope,
            source_agent=source_agent,
            updated_from=updated_from,
            updated_to=updated_to,
        )

    def overview(
        self, project_key: str, agent_ids: list[str], limit: int = 1000
    ) -> dict[str, Any]:
        self.expire_candidates(project_key=project_key)
        return self.store.overview(
            project_key=project_key, agent_ids=agent_ids, limit=limit
        )

    def remove_many(self, knowledge_ids: list[str]) -> dict[str, Any]:
        return self.store.remove_many(agent_id=self.agent_id,
            project_key=self.project_key, knowledge_ids=knowledge_ids)

    def feedback(
        self,
        knowledge_id: str,
        outcome: str,
        evidence_summary: str,
        evidence_kind: str | None = None,
        evidence_ref: str | None = None,
        supersedes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.store.assert_agent_allowed(self.agent_id)
        return self.store.feedback(
            agent_id=self.agent_id,
            knowledge_id=knowledge_id,
            outcome=outcome,
            evidence_summary=evidence_summary,
            evidence_kind=evidence_kind,
            evidence_ref=evidence_ref,
            supersedes=supersedes,
            project_key=self.project_key,
        )

    # ------------------------------------------------------------------
    # Lifecycle governance. These are explicit, caller-driven operations: the
    # runtime does not mutate lifecycle state behind an agent's back, so a
    # retirement decision can always be inspected before it is applied.
    # ------------------------------------------------------------------
    def governance_report(self, project_key: str | None = None) -> dict[str, Any]:
        return self.governor.report(project_key=project_key or self.project_key)

    def expire_candidates(
        self, project_key: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        return self.governor.expire_candidates(
            project_key=project_key or self.project_key, dry_run=dry_run
        )

    def governance_sweep(
        self, project_key: str | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        return self.governor.sweep(
            project_key=project_key or self.project_key, dry_run=dry_run
        )

    def governance_resurrect(
        self, project_key: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        return self.governor.resurrect(
            project_key=project_key or self.project_key, dry_run=dry_run
        )

    def lfhv_report(self, project_key: str | None = None) -> dict[str, Any]:
        return self.governor.lfhv_report(project_key=project_key or self.project_key)

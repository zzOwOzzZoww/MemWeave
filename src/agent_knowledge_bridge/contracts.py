from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_url: str
    model: str
    api_key: str | None = None


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentContext(Contract):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    project_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class KnowledgeSearchRequest(AgentContext):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class KnowledgePublishRequest(AgentContext):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=8000)
    knowledge_type: Literal["fact", "preference", "procedure", "decision"]
    evidence_summary: str = Field(min_length=1, max_length=1000)
    scope: Literal["project", "user"] = "project"
    source_session: str | None = Field(default=None, max_length=160)
    evidence_speaker: str | None = Field(default=None, max_length=160)
    search_terms: str | None = Field(default=None, max_length=1000)
    subject_terms: list[str] = Field(default_factory=list, max_length=16)


class KnowledgeReviewRequest(AgentContext):
    status: Literal["candidate", "quarantined", "all"] = "candidate"
    limit: int = Field(default=20, ge=1, le=100)


class KnowledgeListRequest(AgentContext):
    status: Literal[
        "candidate", "active", "stale", "archived", "quarantined", "all"
    ] = "all"
    limit: int = Field(default=100, ge=1, le=1000)
    query: str = Field(default="", max_length=500)
    knowledge_type: Literal["fact", "preference", "procedure", "decision", "all"] = "all"
    scope: Literal["user", "project", "all"] = "all"
    source_agent: str | None = Field(default=None, max_length=64)
    updated_from: str | None = Field(default=None, max_length=40)
    updated_to: str | None = Field(default=None, max_length=40)


class KnowledgeOverviewRequest(Contract):
    project_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    agent_ids: list[str] = Field(
        default_factory=lambda: ["claude-code", "codex"], min_length=2, max_length=10
    )
    limit: int = Field(default=1000, ge=1, le=2000)


class AgentRegisterRequest(Contract):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    adapter_type: Literal["claude-hook", "codex-hook", "runtime-api", "custom"] = "runtime-api"
    installed: bool = False
    detected_by: list[str] = Field(default_factory=list, max_length=8)
    executable_path: str | None = Field(default=None, max_length=1000)
    config_path: str | None = Field(default=None, max_length=1000)
    capabilities: list[str] = Field(default_factory=lambda: ["shared-knowledge"], max_length=12)


class AgentDisableRequest(Contract):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class KnowledgeGetRequest(AgentContext):
    knowledge_id: str = Field(pattern=r"^kn_[A-Za-z0-9]+$")


class KnowledgeFeedbackRequest(KnowledgeGetRequest):
    supersedes: list[str] | None = Field(default=None, max_length=100)
    outcome: Literal["used", "verified", "rejected"]
    evidence_summary: str = Field(min_length=1, max_length=1000)
    evidence_kind: Literal[
        "observation", "test", "command", "user_approval", "artifact", "duplicate"
    ] | None = None
    evidence_ref: str | None = Field(default=None, max_length=1000)


class KnowledgeRemoveRequest(AgentContext):
    knowledge_ids: list[str] = Field(min_length=1, max_length=100)
    confirm_permanent: Literal[True]


class RecallRequest(AgentContext):
    turn_id: str | None = Field(default=None, max_length=160)
    cwd: str = Field(default="", max_length=2000)
    session_id: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20_000)
    transcript_path: str = Field(default="", max_length=2000)


class LearnTurnRequest(AgentContext):
    turn_id: str | None = Field(default=None, max_length=160)
    cwd: str = Field(default="", max_length=2000)
    session_id: str = Field(min_length=1, max_length=160)
    transcript_path: str = Field(min_length=1, max_length=2000)
    last_assistant_message: str = Field(default="", max_length=20_000)


class MetricsRequest(Contract):
    project_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class LatencyPairRequest(MetricsRequest):
    experiment_id: str = Field(min_length=1, max_length=128)
    pair_id: str = Field(min_length=1, max_length=128)
    agent_id: Literal['codex'] = 'codex'
    baseline_mode: Literal['framework_absent', 'hook_bypass']
    execution_order: Literal['on_off', 'off_on']
    conditions_confirmed: Literal[True]
    on: dict[str, Any]
    off: dict[str, Any]


class ReuseTracesRequest(MetricsRequest):
    knowledge_id: str | None = Field(default=None, pattern=r"^kn_[A-Za-z0-9]+$")
    limit: int = Field(default=100, ge=1, le=1000)


class GovernanceRequest(AgentContext):
    """Lifecycle governance is always dry-run unless the caller opts out.

    Retirement changes what agents can retrieve, so the default for a sweep is
    to report the planned transitions and write nothing.
    """

    dry_run: bool = True

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    adapter_type: Literal["claude-hook", "codex-hook", "gemini-hook", "runtime-api", "custom"] = "runtime-api"
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
    learning_run_id: str | None = Field(default=None, pattern=r"^lr_[A-Za-z0-9]+$")
    expected_status: Literal["candidate"] | None = None


class LearningRunRecordsRequest(AgentContext):
    run_id: str = Field(pattern=r"^lr_[A-Za-z0-9]+$")
    source_agent: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    status: Literal["all", "candidate"] = "all"
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class KnowledgeRemoveRequest(AgentContext):
    knowledge_ids: list[str] = Field(min_length=1, max_length=100)
    confirm_permanent: Literal[True]


class RecallRequest(AgentContext):
    turn_id: str | None = Field(default=None, max_length=160)
    cwd: str = Field(default="", max_length=2000)
    session_id: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20_000)
    transcript_path: str = Field(default="", max_length=2000)
    transcript_format: Literal["auto", "claude", "codex", "gemini"] = "auto"
    bind_transcript_boundary: bool | None = Field(default=None, strict=True)


class RuntimeToolRequest(Contract):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    input: dict[str, Any] = Field(default_factory=dict)
    output: str = Field(default="", max_length=20_000)
    exit_code: int | None = Field(default=None, strict=True)
    is_error: bool | None = Field(default=None, strict=True)
    interrupted: bool | None = Field(default=None, strict=True)
    status: Literal["completed", "failed", "error", "running", "pending"] | None = None

    @model_validator(mode="after")
    def bounded_input(self):
        import json
        if len(json.dumps(self.input, ensure_ascii=False)) > 20_000:
            raise ValueError("tool input exceeds the bounded limit")
        return self


class RuntimeTurnRequest(Contract):
    user_text: str = Field(min_length=1, max_length=20_000)
    assistant_text: str = Field(default="", max_length=20_000)
    tools: list[RuntimeToolRequest] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def unique_tool_ids(self):
        if len({tool.id for tool in self.tools}) != len(self.tools):
            raise ValueError("tool IDs must be unique within a turn")
        return self


class LearnTurnRequest(AgentContext):
    turn_id: str | None = Field(default=None, max_length=160)
    cwd: str = Field(default="", max_length=2000)
    session_id: str = Field(min_length=1, max_length=160)
    transcript_path: str = Field(default="", max_length=2000)
    transcript_format: Literal["auto", "claude", "codex", "gemini"] = "auto"
    turn: RuntimeTurnRequest | None = None
    last_assistant_message: str = Field(default="", max_length=20_000)

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.transcript_path) == (self.turn is not None):
            raise ValueError("supply exactly one of transcript_path or turn")
        if self.turn is not None and self.transcript_format != "auto":
            raise ValueError("inline turn cannot also specify transcript_format")
        return self


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

from __future__ import annotations

import argparse
import hmac
import json
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

try:
    import uvicorn
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse
except ModuleNotFoundError as error:  # pragma: no cover - install guidance
    # The daemon is the only part of MemWeave that needs a web stack. The core,
    # the hooks, and every retrieval path run on the standard library alone, so
    # a missing web stack must not look like a broken installation.
    raise SystemExit(
        f"the MemWeave HTTP daemon needs the optional 'runtime' extra "
        f"(missing: {error.name}).\n"
        "  pip install 'memweave-runtime[runtime]'\n"
        "The hooks and the core need no dependencies at all."
    ) from error

from agent_knowledge_bridge import __version__ as RUNTIME_VERSION
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.contracts import (
    ProviderSettingsRequest,
    AgentDisableRequest,
    AgentRegisterRequest,
    GovernanceRequest,
    KnowledgeFeedbackRequest,
    KnowledgeRemoveRequest,
    KnowledgeGetRequest,
    KnowledgeListRequest,
    KnowledgeOverviewRequest,
    KnowledgePublishRequest,
    KnowledgeReviewRequest,
    KnowledgeSearchRequest,
    LearnTurnRequest,
    MetricsRequest,
    LatencyPairRequest,
    RecallRequest,
    ReuseTracesRequest,
)
from agent_knowledge_bridge.learning import LearningStore
from agent_knowledge_bridge.paths import default_database_path
from agent_knowledge_bridge.reuse import ReuseStore
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.turn_timing import TimingCollector


def create_app(
    *,
    database_path: Path | None = None,
    api_token: str | None = None,
    reviewer: Callable[[str], dict[str, Any]] | None = None,
) -> FastAPI:
    selected_database = (database_path or default_database_path()).resolve()
    selected_token = api_token if api_token is not None else os.getenv("MW_DAEMON_TOKEN", "")
    if not selected_token:
        raise RuntimeError("MW_DAEMON_TOKEN is required")

    # Schema migration happens at startup, not on the first timing callback.
    ReuseStore(selected_database)
    timing_collector = TimingCollector(selected_database)

    @asynccontextmanager
    async def lifespan(_app):
        learning_queue.start()
        try:
            yield
        finally:
            learning_queue.close()
            timing_collector.close()

    app = FastAPI(
        title="MemWeave Runtime",
        version=RUNTIME_VERSION,
        description="Protocol-independent local runtime for governed Agent memory.",
        lifespan=lifespan,
    )
    app.state.database_path = selected_database
    app.state.api_token = selected_token
    app.state.reviewer = reviewer
    app.state.timing_collector = timing_collector
    startup_service = KnowledgeBridgeService(
        agent_id="memweave-governor",
        project_key="runtime-startup",
        database_path=selected_database,
    )
    startup_service.store.migrate_candidate_review_deadlines()
    # One atomic upgrade at startup; repeated Hooks never rebuild the index.
    startup_service.store.rebuild_search_index(only_if_outdated=True)
    startup_service.governor.expire_candidates(project_key=None)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def authorize(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {app.state.api_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid runtime token")

    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Pydantic's default response can echo secret input values.
        return JSONResponse(status_code=422, content={'detail':'请求格式无效，请检查字段及长度'})

    from . import provider
    @app.get('/v1/settings/provider', dependencies=[Depends(authorize)])
    def read_provider():
        return provider.public_settings()

    @app.post('/v1/settings/provider', dependencies=[Depends(authorize)])
    def save_provider(request: ProviderSettingsRequest):
        return provider.save(**request.model_dump())

    @app.post('/v1/settings/provider/check', dependencies=[Depends(authorize)])
    def check_provider():
        try:
            return provider.check_connection()
        except (RuntimeError, OSError):
            raise HTTPException(status_code=400,detail='API 检查失败，请核对地址、Key、模型、网络和额度') from None

    def service(agent_id: str, project_key: str) -> KnowledgeBridgeService:
        return KnowledgeBridgeService(
            agent_id=agent_id,
            project_key=project_key,
            database_path=app.state.database_path,
            auto_install_hooks=True,
        )

    def learning(agent_id: str, project_key: str) -> ClaudeLearningAdapter:
        adapter_type = CodexLearningAdapter if agent_id == "codex" else ClaudeLearningAdapter
        return adapter_type(
            database_path=app.state.database_path,
            agent_id=agent_id,
            project_key=project_key,
            reviewer=app.state.reviewer,
        )

    from .learning_queue import LearningQueue
    def process_learning(payload):
        return learning(payload['agent_id'], payload['project_key']).learn(payload)
    learning_queue = LearningQueue(selected_database, process_learning)
    app.state.learning_queue = learning_queue

    @app.get("/knowledge", response_class=HTMLResponse)
    def knowledge_dashboard() -> str:
        return files("agent_knowledge_bridge").joinpath("knowledge_dashboard.html").read_text(
            encoding="utf-8"
        )

    @app.get("/v1/instance/status", dependencies=[Depends(authorize)])
    def instance_status() -> dict[str, Any]:
        """Relay a status file that a vertical instance maintains on disk.

        The engine has no opinion about what the payload means: a DPI harness
        publishes pipeline stages, another instance would publish something else.
        The file is chosen by the operator via environment, and path-like strings
        are stripped so a dashboard running in a browser can never read back
        absolute paths from the host.
        """
        configured = os.getenv("MW_INSTANCE_STATUS_PATH", "").strip()
        if not configured:
            return {"state": "not_configured"}
        path = Path(configured).resolve()
        if not path.is_file():
            return {"state": "not_started"}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"state": "unavailable"}
        if not isinstance(value, dict):
            return {"state": "unavailable"}
        return _redact_paths(value)

    @app.get("/v1/health", dependencies=[Depends(authorize)])
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "pid": os.getpid(),
            "service": "memweave-runtime",
            "version": RUNTIME_VERSION,
            "storage": "sqlite",
            "retrieval": "fts5-enriched",
        }

    @app.get("/v1/agents/discover", dependencies=[Depends(authorize)])
    def discover_agents() -> dict[str, Any]:
        runtime = service("memweave-console", "agent-registry")
        return {"results": runtime.discover_agents()}

    @app.get("/v1/agents", dependencies=[Depends(authorize)])
    def list_agents(include_disabled: bool = False) -> dict[str, Any]:
        runtime = service("memweave-console", "agent-registry")
        results = runtime.list_agents(include_disabled=include_disabled)
        return {"count": len(results), "results": results}

    @app.post("/v1/agents/register", dependencies=[Depends(authorize)])
    def register_agent(request: AgentRegisterRequest) -> dict[str, Any]:
        runtime = service("memweave-console", "agent-registry")
        return {"agent": runtime.register_agent(request.model_dump())}

    @app.post("/v1/agents/disable", dependencies=[Depends(authorize)])
    def disable_agent(request: AgentDisableRequest) -> dict[str, Any]:
        runtime = service("memweave-console", "agent-registry")
        return {"agent": runtime.disable_agent(request.agent_id)}

    @app.post("/v1/knowledge/search", dependencies=[Depends(authorize)])
    def search(request: KnowledgeSearchRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).search(
            request.query, request.limit
        )

    @app.post("/v1/knowledge/publish", dependencies=[Depends(authorize)])
    def publish(request: KnowledgePublishRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).publish(
            title=request.title,
            content=request.content,
            knowledge_type=request.knowledge_type,
            evidence_summary=request.evidence_summary,
            scope=request.scope,
            source_session=request.source_session,
            evidence_speaker=request.evidence_speaker,
            search_terms=request.search_terms,
            subject_terms=request.subject_terms,
        )

    @app.post("/v1/knowledge/review-queue", dependencies=[Depends(authorize)])
    def review_queue(request: KnowledgeReviewRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).review_queue(
            request.status, request.limit
        )

    @app.post("/v1/knowledge/list", dependencies=[Depends(authorize)])
    def list_knowledge(request: KnowledgeListRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).list_records(
            request.status,
            request.limit,
            query=request.query,
            knowledge_type=request.knowledge_type,
            scope=request.scope,
            source_agent=request.source_agent,
            updated_from=request.updated_from,
            updated_to=request.updated_to,
        )

    @app.post("/v1/knowledge/overview", dependencies=[Depends(authorize)])
    def knowledge_overview(request: KnowledgeOverviewRequest) -> dict[str, Any]:
        return service(request.agent_ids[0], request.project_key).overview(
            project_key=request.project_key,
            agent_ids=request.agent_ids,
            limit=request.limit,
        )

    @app.post("/v1/knowledge/get", dependencies=[Depends(authorize)])
    def get_knowledge(request: KnowledgeGetRequest) -> dict[str, Any]:
        result = service(request.agent_id, request.project_key).get(request.knowledge_id)
        result["compilation"] = LearningStore(
            app.state.database_path
        ).compilation_provenance(request.knowledge_id)
        return result

    @app.post("/v1/knowledge/remove", dependencies=[Depends(authorize)])
    def remove_knowledge(request: KnowledgeRemoveRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).remove_many(request.knowledge_ids)

    @app.post("/v1/knowledge/feedback", dependencies=[Depends(authorize)])
    def feedback(request: KnowledgeFeedbackRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).feedback(
            knowledge_id=request.knowledge_id,
            outcome=request.outcome,
            evidence_summary=request.evidence_summary,
            evidence_kind=request.evidence_kind,
            evidence_ref=request.evidence_ref,
            supersedes=request.supersedes,
        )

    @app.post('/v1/metrics/latency-pairs', dependencies=[Depends(authorize)])
    def latency_pair(request: LatencyPairRequest) -> dict[str, Any]:
        from agent_knowledge_bridge.latency_impact import record_pair
        return record_pair(app.state.database_path, **request.model_dump())

    @app.post("/v1/learning/recall", dependencies=[Depends(authorize)])
    def recall(request: RecallRequest) -> dict[str, Any]:
        return learning(request.agent_id, request.project_key).recall(
            request.model_dump(exclude={"agent_id", "project_key"})
        )

    @app.post('/v1/learning/queue', dependencies=[Depends(authorize)])
    def queue_learning(request: LearnTurnRequest) -> dict[str, Any]:
        store = LearningStore(app.state.database_path)
        if not store.knowledge.agent_allowed(request.agent_id, require_registered=True):
            return {'status': 'disabled'}
        result = learning_queue.submit(request.model_dump())
        timing_collector.submit(agent_id=request.agent_id, project_key=request.project_key,
            session_id=request.session_id, turn_id=request.turn_id, transcript_path=request.transcript_path)
        return result

    @app.get('/v1/learning/queue', dependencies=[Depends(authorize)])
    def queue_status() -> dict[str, Any]:
        return {'counts': learning_queue.summary()}

    @app.post("/v1/learning/turn", dependencies=[Depends(authorize)])
    def learn_turn(request: LearnTurnRequest) -> dict[str, Any]:
        try:
            return learning(request.agent_id, request.project_key).learn(
                request.model_dump(exclude={"agent_id", "project_key"}))
        finally:
            # Native task_complete may be appended only after the Stop hook
            # exits. Never wait for it in this HTTP request or in recall.
            timing_collector.submit(agent_id=request.agent_id, project_key=request.project_key,
                session_id=request.session_id, turn_id=request.turn_id,
                transcript_path=request.transcript_path)

    @app.post("/v1/reuse/traces", dependencies=[Depends(authorize)])
    def reuse_traces(request: ReuseTracesRequest) -> dict[str, Any]:
        records = ReuseStore(app.state.database_path).list(
            request.project_key, request.knowledge_id, request.limit)
        return {"results": records, "count": len(records)}

    @app.post("/v1/metrics", dependencies=[Depends(authorize)])
    def metrics(request: MetricsRequest) -> dict[str, Any]:
        return LearningStore(app.state.database_path).metrics(request.project_key)

    @app.post("/v1/governance/report", dependencies=[Depends(authorize)])
    def governance_report(request: GovernanceRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).governance_report()

    @app.post("/v1/governance/sweep", dependencies=[Depends(authorize)])
    def governance_sweep(request: GovernanceRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).governance_sweep(
            dry_run=request.dry_run
        )

    @app.post("/v1/governance/resurrect", dependencies=[Depends(authorize)])
    def governance_resurrect(request: GovernanceRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).governance_resurrect(
            dry_run=request.dry_run
        )

    @app.post("/v1/governance/lfhv", dependencies=[Depends(authorize)])
    def governance_lfhv(request: GovernanceRequest) -> dict[str, Any]:
        return service(request.agent_id, request.project_key).lfhv_report()

    return app


def _redact_paths(value: Any) -> Any:
    """Keep a status payload's shape while dropping host-local locations.

    A status file records where inputs came from, and a browser-side dashboard
    has no business learning the operator's directory layout. Path-like strings
    are reduced to their final component, or dropped when that component is a
    bare drive or the string is a filesystem URI.
    """
    if isinstance(value, dict):
        return {key: _redact_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_paths(item) for item in value]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    path_like = (
        ("\\" in stripped)
        or (stripped.startswith("/") and not stripped.startswith("//"))
        or (len(stripped) > 1 and stripped[1] == ":")
    )
    if not path_like:
        return value
    tail = stripped.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail if tail and len(tail) > 1 else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local MemWeave Runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("MemWeave Runtime only accepts a loopback host")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

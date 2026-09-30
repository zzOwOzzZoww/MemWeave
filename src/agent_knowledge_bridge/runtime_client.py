from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class MemWeaveRuntimeClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        agent_id: str,
        project_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url is required")
        if not token.strip():
            raise ValueError("runtime token is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.agent_id = agent_id
        self._automatic_project = project_key is None
        if project_key is None:
            from .runtime_state import project_key as resolve_project
            project_key = resolve_project(agent=agent_id)
        if not project_key.strip():
            raise ValueError('project_key must be non-empty')
        self.project_key = project_key
        self.timeout = timeout

    def recall(self, *, session_id: str, prompt: str, turn_id: str | None = None, cwd: str = "", transcript_path: str = "") -> dict[str, Any]:
        return self._post(
            "/v1/learning/recall",
            {
                **self._context(cwd=cwd),
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "prompt": prompt,
                "transcript_path": transcript_path,
            },
        )

    def learn(
        self,
        *,
        session_id: str,
        transcript_path: str,
        last_assistant_message: str = "",
        turn_id: str | None = None,
        cwd: str = "",
    ) -> dict[str, Any]:
        return self._post(
            "/v1/learning/turn",
            {
                **self._context(cwd=cwd),
                "session_id": session_id,
                "turn_id": turn_id,
                "cwd": cwd,
                "transcript_path": transcript_path,
                "last_assistant_message": last_assistant_message,
            },
        )

    def enqueue_learning(self, **turn: Any) -> dict[str, Any]:
        return self._post('/v1/learning/queue', {**self._context(cwd=str(turn.get('cwd') or '')), **turn})

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/search",
            {**self._context(), "query": query, "limit": limit},
        )

    def publish(self, **knowledge: Any) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/publish", {**self._context(), **knowledge}
        )

    def review_queue(
        self, status: str = "candidate", limit: int = 20
    ) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/review-queue",
            {**self._context(), "status": status, "limit": limit},
        )

    def get(self, knowledge_id: str) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/get",
            {**self._context(), "knowledge_id": knowledge_id},
        )

    def feedback(self, **feedback: Any) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/feedback", {**self._context(), **feedback}
        )

    def metrics(self) -> dict[str, Any]:
        return self._post(
            "/v1/metrics", {"project_key": self.project_key}
        )

    def list_knowledge(self, status: str = "all", limit: int = 100) -> dict[str, Any]:
        return self._post(
            "/v1/knowledge/list",
            {**self._context(), "status": status, "limit": limit},
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health", None)

    def discover_agents(self) -> dict[str, Any]:
        return self._request("GET", "/v1/agents/discover", None)

    def list_agents(self, *, include_disabled: bool = False) -> dict[str, Any]:
        suffix = "?include_disabled=true" if include_disabled else ""
        return self._request("GET", f"/v1/agents{suffix}", None)

    def register_agent(self, **agent: Any) -> dict[str, Any]:
        return self._post("/v1/agents/register", agent)

    def disable_agent(self, agent_id: str) -> dict[str, Any]:
        return self._post("/v1/agents/disable", {"agent_id": agent_id})

    def _context(self, *, cwd: str = '') -> dict[str, str]:
        project = self.project_key
        if self._automatic_project and cwd:
            from .runtime_state import project_key as resolve_project
            project = resolve_project(agent=self.agent_id, cwd=cwd)
        return {"agent_id": self.agent_id, "project_key": project}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(
                f"MemWeave Runtime HTTP {exc.code} for {path}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MemWeave Runtime unavailable at {self.base_url}: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"MemWeave Runtime returned a non-object for {path}")
        return result

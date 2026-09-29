"""Shared Runtime-state resolution for every hook and launcher.

Both hooks need to answer the same three questions before they can talk to
MemWeave: which Runtime is live, which SQLite file it owns, and which project
key the pair shares.  They used to answer them independently, and the two
answers had drifted apart:

* ``claude_learning_hook`` returned the *first* state file that parsed, so a
  stale file shadowed the live one.
* ``codex_learning_hook`` sorted by ``started_at`` but only consulted two of
  the three known locations.
* Only ``claude_learning_hook`` read the recorded ``database_path`` at all, so
  the two agents could silently be pointed at different databases.
* A launcher wrote its state under the key ``database`` while the reader looked
  for ``database_path``; the value was ignored without any error.

Everything below is stdlib-only and side-effect free, so the hooks can import
it on the hot path without pulling in the web stack.

Precedence, highest first:

1. Explicit environment variables (``MW_DB_PATH`` / ``AKB_DB_PATH``, and the
   matching keys for project, agent and Runtime endpoint).
2. The **newest** recorded Runtime state file.  Newest is decided by
   ``started_at`` and falls back to file mtime, so a legacy file left behind by
   an older launcher can never win over a live Runtime.
3. The ``database_path`` recorded in ``<home>/config.json`` by ``memweave init``.
4. The default database under MemWeave home.

A recorded state file may spell its database path ``database_path`` (current)
or ``database`` (legacy).  Both are honoured; the reader never ignores a key
silently.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from agent_knowledge_bridge.paths import (
    PACKAGE_PROJECT_ROOT,
    configured_database_path,
    memweave_home,
)


PROJECT_ROOT = PACKAGE_PROJECT_ROOT

#: The database path a fresh checkout uses when nothing has been recorded yet.
REPO_DATABASE_NAME = "knowledge.db"

#: Env vars that pin the shared project key, highest precedence first.
PROJECT_KEY_VARIABLES = ("MW_PROJECT_KEY", "AKB_PROJECT_KEY")

#: Env vars that pin the requesting agent identity, highest precedence first.
AGENT_ID_VARIABLES = ("MW_AGENT_ID", "AKB_AGENT_ID")


def state_paths() -> list[Path]:
    """Return every known Runtime state location, most likely live first.

    Order here only breaks ties when two files carry the same timestamp; the
    real ranking is done by :func:`read_runtime_states`.
    """
    return [
        memweave_home() / "runtime-state.json",
        PROJECT_ROOT / "data" / "runtime" / "runtime-state.json",
        PROJECT_ROOT / "data" / "knowledge-console" / "runtime-state.json",
    ]


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    return {**value, "_state_path": str(path)}


def _started_at(state: dict[str, Any]) -> float:
    raw = str(state.get("started_at") or "").replace("Z", "+00:00")
    try:
        return float(datetime.fromisoformat(raw).timestamp())
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return Path(str(state.get("_state_path") or "")).stat().st_mtime
    except OSError:
        return 0.0


def read_runtime_states() -> list[dict[str, Any]]:
    """Return all readable state files, newest first.

    Recency is the merge key because it is what distinguishes a live Runtime
    from the leftovers of an earlier launch.  A legacy file written by an older
    launcher stays readable but can never outrank the current one.
    """
    states: list[tuple[float, int, dict[str, Any]]] = []
    for order, path in enumerate(state_paths()):
        state = _read_state(path)
        if state is not None:
            states.append((_started_at(state), -order, state))
    states.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [state for _started, _order, state in states]


def recorded_database_path() -> Path | None:
    """Return the first recorded database path, newest state file first."""
    for state in read_runtime_states():
        # ``database`` is the spelling an older launcher used; reading both
        # keeps its value usable instead of silently discarding it.
        candidate = state.get("database_path") or state.get("database")
        if isinstance(candidate, str) and candidate.strip():
            return Path(candidate).expanduser().resolve()
    return None


def repo_database_path() -> Path:
    """The database a fresh, never-initialised checkout uses."""
    return (PROJECT_ROOT / "data" / REPO_DATABASE_NAME).resolve()


def database_path() -> Path:
    """Resolve the database every hook and the daemon should agree on."""
    for name in ("MW_DB_PATH", "AKB_DB_PATH"):
        configured = os.getenv(name)
        if configured and configured.strip():
            return Path(configured).expanduser().resolve()

    recorded = recorded_database_path()
    if recorded is not None:
        return recorded

    configured = configured_database_path()
    if configured is not None:
        return configured

    # Neither a Runtime nor an explicit configuration exists yet.  Prefer an
    # in-tree database so a working checkout keeps working, and only fall back
    # to MemWeave home when there is nothing in the tree to preserve.  The
    # daemon resolves its own default the same way, which is what keeps the
    # two sides from diverging on first run.
    repo = repo_database_path()
    return repo if repo.exists() else (memweave_home() / "data" / REPO_DATABASE_NAME).resolve()


def project_key(default: str = "claude-codex-mvp", *, cwd: str = '') -> str:
    """Resolve the shared project key: env, then newest state, then default."""
    for name in PROJECT_KEY_VARIABLES:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    if cwd:
        import hashlib
        from .paths import memweave_home
        path = Path(cwd).expanduser().resolve()
        config_path = memweave_home() / 'config.json'
        config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
        mappings = config.get('workspace_projects', {})
        for root, key in sorted(mappings.items(), key=lambda pair: len(pair[0]), reverse=True):
            if path.is_relative_to(Path(root).resolve()):
                return key
        root = next((p for p in (path, *path.parents) if (p / '.git').exists()), path)
        canonical = os.path.normcase(str(root))
        return 'workspace-' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]
    for state in read_runtime_states():
        value = state.get("project_key")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def agent_id(default: str = "claude-code") -> str:
    """Resolve the requesting agent identity from the environment."""
    for name in AGENT_ID_VARIABLES:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def hook_enabled(agent: str) -> bool:
    """Cheap read-only registration gate: no schema initialization per prompt."""
    import sqlite3
    from contextlib import closing
    path = database_path()
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
            row = db.execute('SELECT enabled FROM agent_registry WHERE agent_id=?', (agent,)).fetchone()
            return bool(row and row[0])
    except sqlite3.Error:
        return False


def runtime_token() -> str:
    """Return the daemon bearer token: env first, then the newest state file."""
    configured = os.getenv("MW_DAEMON_TOKEN", "")
    if configured.strip():
        return configured.strip()
    for state in read_runtime_states():
        value = state.get("token")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def runtime_candidates() -> Iterator[tuple[str, str]]:
    """Yield ``(url, token)`` pairs to probe, best candidate first.

    An explicit endpoint wins outright.  Recorded Runtimes follow, newest
    first, then any remaining readable state as a last resort so a live daemon
    behind a stale file is still reachable.
    """
    explicit_url = os.getenv("MW_DAEMON_URL", "").strip()
    explicit_token = runtime_token()
    if explicit_url and explicit_token:
        yield explicit_url, explicit_token

    for state in read_runtime_states():
        url = str(state.get("url") or "").strip()
        token = str(state.get("token") or "").strip()
        if not url or not token:
            # A state file with no endpoint is a status record, not a Runtime.
            continue
        if url == explicit_url:
            continue
        yield url, token


def runtime_endpoint() -> tuple[str, str]:
    """Return the newest recorded ``(url, token)`` without probing it."""
    for state in read_runtime_states():
        url = str(state.get("url") or "").strip()
        token = str(state.get("token") or "").strip()
        if url and token:
            return url, token
    return "", ""

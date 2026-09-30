"""Local Agent discovery and registration metadata.

Discovery is intentionally conservative: it checks known executable names and
configuration locations, but never starts an Agent or reads its credentials.
Detection and framework registration are separate operations. A detected Agent
is only able to use MemWeave after the user explicitly registers it.
"""
from __future__ import annotations

import os
import shutil
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .integration_profiles import PROFILES
from .integration_installation import prepare_hook_installation, install_hook_plan, inspect_hook_plan, is_owned_hook

PROJECT_ROOT = Path(__file__).resolve().parents[2]


SUPPORTED_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "agent_id": "claude-code", "display_name": "Claude Code",
        "adapter_type": "claude-hook", "executables": ("claude",),
        "config_paths": ("~/.claude",),
        "capabilities": ("recall", "learn", "shared-knowledge"),
    },
    {
        "agent_id": "codex", "display_name": "Codex",
        "adapter_type": "codex-hook", "executables": ("codex",),
        "config_paths": ("~/.codex",),
        "capabilities": ("recall", "learn", "shared-knowledge"),
    },
    {
        "agent_id": "cursor", "display_name": "Cursor",
        "adapter_type": "runtime-api", "executables": ("cursor",),
        "config_paths": ("~/.cursor", "%LOCALAPPDATA%/Programs/Cursor"),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "windsurf", "display_name": "Windsurf",
        "adapter_type": "runtime-api", "executables": ("windsurf",),
        "config_paths": ("~/.codeium/windsurf", "%LOCALAPPDATA%/Programs/Windsurf"),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "gemini-cli", "display_name": "Gemini CLI",
        "adapter_type": "gemini-hook", "executables": ("gemini",),
        "config_paths": ("~/.gemini",),
        "capabilities": ("recall", "learn", "shared-knowledge"),
    },
    {
        "agent_id": "aider", "display_name": "Aider",
        "adapter_type": "runtime-api", "executables": ("aider",),
        "config_paths": ("~/.aider.conf.yml", "~/.aider"),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "goose", "display_name": "Goose",
        "adapter_type": "runtime-api", "executables": ("goose",),
        "config_paths": ("~/.config/goose",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "openhands", "display_name": "OpenHands",
        "adapter_type": "runtime-api", "executables": ("openhands",),
        "config_paths": ("~/.openhands",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "opencode", "display_name": "OpenCode",
        "adapter_type": "runtime-api", "executables": ("opencode",),
        "config_paths": ("~/.config/opencode",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "continue", "display_name": "Continue",
        "adapter_type": "runtime-api", "executables": ("continue",),
        "config_paths": ("~/.continue",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "cline", "display_name": "Cline",
        "adapter_type": "runtime-api", "executables": (),
        "config_paths": ("~/.vscode/extensions/saoudrizwan.claude-dev-*",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "roo-cline", "display_name": "Roo Code",
        "adapter_type": "runtime-api", "executables": (),
        "config_paths": ("~/.vscode/extensions/rooveterinaryinc.roo-cline-*",),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "qoder", "display_name": "Qoder",
        "adapter_type": "runtime-api", "executables": ("qoder", "QoderWork CN"),
        "config_paths": (
            "%APPDATA%/Qoder", "%APPDATA%/QoderWork CN",
            "%LOCALAPPDATA%/Programs/Qoder", "%LOCALAPPDATA%/Programs/QoderWork CN",
        ),
        "capabilities": ("shared-knowledge",),
    },
    {
        "agent_id": "workbuddy", "display_name": "WorkBuddy",
        "adapter_type": "runtime-api", "executables": ("workbuddy", "WorkBuddy"),
        "config_paths": (
            "%APPDATA%/WorkBuddy", "%LOCALAPPDATA%/Programs/WorkBuddy",
        ),
        "capabilities": ("shared-knowledge",),
    },
)


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


NATIVE_HOOKS = {
    agent: (profile.launcher, profile.config_file, (profile.recall_event, profile.learn_event))
    for agent, profile in PROFILES.items()
}


def _agent_home(agent_id: str) -> Path:
    profile = PROFILES[agent_id]
    selected = os.getenv(profile.home_env)
    return _expand(selected) / profile.home_subdir if selected else _expand(profile.config_home)


def configure_agent_scope(agent_id: str) -> dict[str, Any]:
    """Persist an Agent-wide pool without overriding explicit isolation."""
    from .paths import memweave_home
    from .provider import atomic_json
    from .runtime_state import agent_shared_project
    path = memweave_home() / 'config.json'
    config = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    shared_project = agent_shared_project(agent_id, config=config)
    changed = False
    if path.is_file():
        projects = config.setdefault('agent_projects', {})
        if agent_id not in projects:
            projects[agent_id] = shared_project
            backup = path.with_name('config.json.memweave-backup')
            if not backup.exists():
                shutil.copy2(path, backup)
            atomic_json(path, config)
            changed = True
    return {'scope': 'user-global', 'shared_project': shared_project, 'scope_changed': changed}


_is_memweave_hook = is_owned_hook


def _existing_config(pattern: str) -> Path | None:
    path = _expand(pattern)
    if "*" in path.name or "?" in path.name:
        matches = sorted(path.parent.glob(path.name)) if path.parent.exists() else []
        return matches[0] if matches else None
    return path if path.exists() else None


def discover_agents() -> list[dict[str, Any]]:
    """Return supported local probes without reading secrets or launching tools."""
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    discovered: list[dict[str, Any]] = []
    for spec in SUPPORTED_AGENTS:
        executable_path = next(
            (shutil.which(name) for name in spec["executables"] if shutil.which(name)),
            None,
        )
        if spec['agent_id'] in NATIVE_HOOKS:
            home = _agent_home(spec['agent_id'])
            config_path = str(home) if home.exists() else None
        else:
            config_path = next(
                (str(found) for pattern in spec["config_paths"] if (found := _existing_config(pattern))),
                None,
            )
        evidence = []
        if executable_path:
            evidence.append("executable")
        if config_path:
            evidence.append("config")
        discovered.append({
            "agent_id": spec["agent_id"],
            "display_name": spec["display_name"],
            "adapter_type": spec["adapter_type"],
            "capabilities": list(spec["capabilities"]),
            "installed": bool(evidence),
            "detected_by": evidence,
            "executable_path": executable_path,
            "config_path": config_path,
            "checked_at": checked_at,
        })
    return discovered


def supported_agent(agent_id: str) -> dict[str, Any] | None:
    for spec in SUPPORTED_AGENTS:
        if spec["agent_id"] == agent_id:
            return dict(spec)
    return None


def _codex_hook_execution(database_path: Path) -> dict[str, Any]:
    """Only a hook from a real Codex rollout counts as desktop integration.

    The hook can also be invoked manually for diagnostics. Those invocations
    must never turn the UI green or imply that ordinary conversations work.
    """
    result: dict[str, Any] = {"observed": False, "last_prompt_at": None, "last_stop_at": None}
    audit = database_path.with_suffix(".codex-hook-runs.jsonl")
    if not audit.is_file():
        return result
    try:
        with audit.open('rb') as stream:
            stream.seek(max(0, audit.stat().st_size - 262144))
            lines = stream.read(262144).decode('utf-8', errors='replace').splitlines()[-400:]
    except OSError:
        return result
    sessions_root = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    checked: dict[str, bool] = {}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") != "completed" or row.get("event") not in {"UserPromptSubmit", "Stop"}:
            continue
        session_id = str(row.get("session_id") or "")
        if not session_id or len(session_id) > 80:
            continue
        if session_id not in checked:
            checked[session_id] = any(sessions_root.glob(f"**/rollout-*{session_id}.jsonl"))
        if not checked[session_id]:
            continue
        key = "last_prompt_at" if row["event"] == "UserPromptSubmit" else "last_stop_at"
        if result[key] is None:
            result[key] = row.get("created_at")
        if result["last_prompt_at"] and result["last_stop_at"]:
            break
    result["observed"] = bool(result["last_prompt_at"])
    return result


def _codex_hooks_feature_enabled() -> bool:
    config_path = _agent_home('codex') / 'config.toml'
    try:
        lines = config_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return False
    in_features = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            continue
        if in_features and stripped.startswith("hooks") and "=" in stripped and not stripped.startswith("#"):
            return stripped.split("=", 1)[1].strip().lower() == "true"
    return False


def hook_configuration(agent_id: str, *, database_path: Path | None = None) -> dict[str, Any]:
    """Inspect native configuration and its actual unified executor without writes."""
    from .runtime_state import agent_shared_project
    scope = {"scope": "user-global", "shared_project": agent_shared_project(agent_id)}
    if agent_id not in NATIVE_HOOKS:
        return {"supported": False, "configured": False, "config_paths": [], "matched_paths": [], **scope}
    profile = PROFILES[agent_id]
    path = _agent_home(agent_id) / profile.config_file
    try:
        status = inspect_hook_plan(prepare_hook_installation(profile, config_path=path))
    except (OSError, ValueError, RuntimeError):
        status = {"configured": False, "executor_ready": False}
    configuration = {
        "supported": True,
        **status,
        "config_paths": [str(path)] if path.is_file() else [],
        "matched_paths": [str(path)] if status["configured"] else [],
        **scope,
    }
    if agent_id == "codex":
        configuration["feature_enabled"] = _codex_hooks_feature_enabled()
        configuration["execution"] = _codex_hook_execution(
            database_path or PROJECT_ROOT / "data" / "knowledge.db")
    return configuration


def install_native_hook(agent_id: str, *, config_path: Path | None = None) -> dict[str, Any]:
    """Install a built-in confirmed profile through the unified installer."""
    if agent_id not in NATIVE_HOOKS:
        raise ValueError(f"Agent {agent_id} has no maintained native Hook adapter")
    profile = PROFILES[agent_id]
    if config_path is None:
        config_path = _agent_home(agent_id) / profile.config_file
    result = install_hook_plan(prepare_hook_installation(profile, config_path=config_path))
    feature_result = _enable_codex_hooks_feature() if agent_id == "codex" else None
    return {**result, "feature": feature_result, **configure_agent_scope(agent_id)}


def _enable_codex_hooks_feature() -> dict[str, Any]:
    """Enable the Codex feature gate required by 0.155+.

    ``hooks.json`` can be valid and still be ignored when ``[features].hooks``
    is false. Keep this update additive and create a timestamp-free backup only
    once, matching the hook configuration install behavior.
    """
    config_path = _agent_home('codex') / 'config.toml'
    try:
        text = config_path.read_text(encoding="utf-8-sig") if config_path.exists() else ""
    except OSError as exc:
        raise RuntimeError(f"无法读取 Codex 配置: {config_path}") from exc
    lines = text.splitlines()
    in_features = False
    found = False
    changed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
        elif in_features and stripped.startswith("hooks") and "=" in stripped and not stripped.startswith("#"):
            found = True
            if stripped.split("=", 1)[1].strip().lower() != "true":
                lines[index] = "hooks = true"
                changed = True
            break
    if not found:
        if any(line.strip() == "[features]" for line in lines):
            index = next(i for i, line in enumerate(lines) if line.strip() == "[features]") + 1
            lines.insert(index, "hooks = true")
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(["[features]", "hooks = true"])
        changed = True
    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        backup = config_path.with_name(f"{config_path.name}.memweave-hooks-backup")
        if config_path.exists() and not backup.exists():
            shutil.copy2(config_path, backup)
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"enabled": True, "changed": changed, "config_path": str(config_path)}

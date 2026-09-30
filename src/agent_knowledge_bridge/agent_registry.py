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
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        "adapter_type": "runtime-api", "executables": ("gemini",),
        "config_paths": ("~/.gemini",),
        "capabilities": ("shared-knowledge",),
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


def _agent_home(agent_id: str) -> Path:
    variable = 'CLAUDE_CONFIG_DIR' if agent_id == 'claude-code' else 'CODEX_HOME'
    return _expand(os.getenv(variable) or ('~/.claude' if agent_id == 'claude-code' else '~/.codex'))


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


def _is_memweave_hook(item: Any, marker: str) -> bool:
    return isinstance(item, dict) and any(
        re.search(r'(?<![\w-])' + re.escape(marker) + r'(?![\w.-])', str(item.get(key, '')))
        for key in ('command', 'commandWindows')
    )


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
        if spec['agent_id'] in {'claude-code', 'codex'}:
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
    """Inspect whether the native MemWeave hook is present.

    This is deliberately read-only and checks global configuration only. A
    project-level hook may exist in a different workspace, so its status
    cannot establish whether ordinary client sessions are connected.
    """
    if agent_id == "claude-code":
        candidates = [_agent_home(agent_id) / 'settings.json']
        marker = "claude_learning_hook.py"
    elif agent_id == "codex":
        candidates = [_agent_home(agent_id) / 'hooks.json']
        marker = "codex_learning_hook.py"
    else:
        from .runtime_state import agent_shared_project
        return {
            "supported": False,
            "configured": False,
            "config_paths": [],
            "matched_paths": [],
            "scope": "user-global",
            "shared_project": agent_shared_project(agent_id),
        }

    from .paths import memweave_home
    from .runtime_state import agent_shared_project
    scope = {'scope': 'user-global', 'shared_project': agent_shared_project(agent_id)}
    existing: list[str] = []
    matched: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        existing.append(str(path))
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        hooks = config.get('hooks', {}) if isinstance(config, dict) else {}
        valid = isinstance(config, dict) and isinstance(hooks, dict) and not config.get('disableAllHooks', False)
        launcher = memweave_home() / 'launchers' / marker
        for event in ('UserPromptSubmit', 'Stop'):
            entries = hooks.get(event, []) if isinstance(hooks, dict) else []
            matches = [(group, item) for group in entries if isinstance(group, dict)
                       and isinstance(group.get('hooks'), list)
                       for item in group['hooks'] if _is_memweave_hook(item, marker)] if isinstance(entries, list) else []
            if len(matches) != 1:
                valid = False
                continue
            group, item = matches[0]
            commands = [str(item.get('command', '')), str(item.get('commandWindows', item.get('command', '')))]
            valid = valid and group.get('matcher', '') in ('', '*') and item.get('type') == 'command' \
                and item.get('async', False) is False and 'shell' not in item and 'timeoutSec' not in item \
                and launcher.is_file() and all(str(launcher) in command and '--workspace' not in command
                                               and '--project' not in command for command in commands)
        if valid:
            matched.append(str(path))
    configuration = {
        "supported": True,
        "configured": bool(matched),
        "config_paths": existing,
        "matched_paths": matched,
        **scope,
    }
    if agent_id == "codex":
        configuration["feature_enabled"] = _codex_hooks_feature_enabled()
        configuration["execution"] = _codex_hook_execution(
            database_path or PROJECT_ROOT / "data" / "knowledge.db"
        )
    return configuration


def install_native_hook(agent_id: str, *, config_path: Path | None = None) -> dict[str, Any]:
    """Install the framework hook into an Agent's global configuration.

    The operation is additive and idempotent: existing settings and hooks are
    preserved, and a timestamped backup is written before the first change.
    Only adapters with a maintained native hook are accepted here.
    """
    if agent_id not in {"claude-code", "codex"}:
        raise ValueError(f"Agent {agent_id} has no maintained native Hook adapter")
    if config_path is None:
        config_path = _agent_home(agent_id) / ('settings.json' if agent_id == 'claude-code' else 'hooks.json')
    config_path = config_path.expanduser().resolve()
    marker = "claude_learning_hook.py" if agent_id == "claude-code" else "codex_learning_hook.py"
    from .paths import memweave_home
    packaged_script = Path(__file__).resolve().parent / 'hooks' / marker
    # The small launcher pins both the installation's interpreter and data home.
    # py.exe only bootstraps it; virtualenv dependencies are never resolved by py.
    launcher_root = memweave_home() / 'launchers'
    launcher_root.mkdir(parents=True, exist_ok=True)
    script = launcher_root / marker
    script.write_text('import os, subprocess, sys\n'
        + 'os.environ["MEMWEAVE_HOME"] = ' + repr(str(memweave_home())) + '\n'
        + 'raise SystemExit(subprocess.call(' + repr([str(Path(sys.executable).resolve()), '-X', 'utf8', '-m',
            'agent_knowledge_bridge.hooks.' + marker[:-3]]) + ' + sys.argv[1:]))\n', encoding='utf-8')
    if not packaged_script.is_file():
        raise RuntimeError('MemWeave packaged hook is missing; reinstall the package')
    # Codex 0.155 on Windows fails before Python starts when the executable in
    # a command hook lives below a path containing spaces (for example
    # ``C:\\Program Files\\Python312\\python.exe``).  Prefer the system Python
    # launcher for Codex: its stable path has no spaces and ``-3`` still pins
    # the intended major version.  Claude Code can keep using the exact
    # interpreter that launched MemWeave because its hook runner handles the
    # quoted executable correctly.
    if agent_id == "codex" and os.name == "nt" and (launcher := shutil.which("py")):
        command = f'{Path(launcher).resolve()} -3 "{script}" hook'
    else:
        command = f'"{Path(sys.executable).resolve()}" "{script}" hook'
    hook: dict[str, Any] = {
        "type": "command",
        "command": command,
        "commandWindows": command,
        "timeout": 120,
        # Recall must complete inside the prompt-submit turn for its output to
        # become context the model actually sees.  An asynchronous hook returns
        # to the client immediately, so the recalled knowledge is written to the
        # database and to the trace tables but never reaches the model: recall
        # "fires" while the agent stays ignorant.  Both agents therefore use a
        # synchronous UserPromptSubmit; the retrieval is a local FTS query
        # (P95 well under 10 ms), so the added latency is not perceptible.
        "async": False,
        "statusMessage": "MemWeave is reviewing this turn",
    }
    # No ``shell`` field for either agent.  Declaring ``shell: powershell``
    # made the client wrap this command as ``powershell -Command "<command>"``,
    # and PowerShell then re-tokenised it: the quoted interpreter became one
    # string and the trailing bare ``hook`` looked like a second command, so
    # every UserPromptSubmit and Stop died with ``UnexpectedToken`` before
    # Python ever started.  The same wrapping also put the non-ASCII path
    # through the console code page, which is what produced the mojibake in the
    # error text.  Spawning the resolved argv directly avoids both: there is no
    # interpreter left to re-split the quotes, and nothing decodes the path.
    if agent_id != "claude-code":
        hook["timeout"] = 30

    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 Agent 配置: {config_path}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Agent 配置不是 JSON 对象: {config_path}")
        config = loaded
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError(f"Agent 配置中的 hooks 不是对象: {config_path}")
    changed = False
    for event in ("UserPromptSubmit", "Stop"):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise RuntimeError(f"Agent 配置中的 {event} 不是数组: {config_path}")
        # Remove only our entries from every group, including conditional and
        # duplicate legacy installs. Other tools retain their matchers and order.
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('hooks'), list):
                cleaned.append(entry)
                continue
            remaining = [item for item in entry['hooks'] if not _is_memweave_hook(item, marker)]
            if remaining or remaining == entry['hooks']:
                cleaned.append({**entry, 'hooks': remaining})
        existing = next((entry for entry in cleaned if isinstance(entry, dict)
                         and entry.get('matcher', '') == '' and isinstance(entry.get('hooks'), list)), None)
        if existing is None:
            existing = {'matcher': '', 'hooks': []}
            cleaned.append(existing)
        existing['hooks'].append(dict(hook,
            timeout=30 if agent_id == 'codex' and event == 'UserPromptSubmit' else 120,
            statusMessage='MemWeave is recalling validated knowledge' if event == 'UserPromptSubmit'
            else 'MemWeave is reviewing this turn'))
        if cleaned != entries:
            hooks[event] = cleaned
            changed = True
    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            backup = config_path.with_name(f"{config_path.name}.memweave-backup")
            if not backup.exists():
                shutil.copy2(config_path, backup)
        payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=config_path.parent, delete=False) as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        try:
            temporary.replace(config_path)
        finally:
            temporary.unlink(missing_ok=True)
    feature_result = None
    if agent_id == "codex":
        feature_result = _enable_codex_hooks_feature()
    scope = configure_agent_scope(agent_id)
    return {
        "configured": not config.get('disableAllHooks', False),
        "changed": changed,
        "config_path": str(config_path),
        "backup_path": str(config_path.with_name(f"{config_path.name}.memweave-backup")) if changed else None,
        "feature": feature_result,
        **scope,
    }


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

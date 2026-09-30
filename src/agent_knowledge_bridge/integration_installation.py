"""Prepare and install confirmed hook protocols without Agent-specific executors."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .integration_profiles import IntegrationProfile, PROFILES, parse_profile, profile_spec
from .paths import memweave_home


EXECUTOR_MODULE = "agent_knowledge_bridge.hooks.generic_learning_hook"
EXECUTOR_PATH = Path(__file__).with_name("hooks") / "generic_learning_hook.py"
PROTOCOLS = frozenset({"command-json", "gemini-json", "codebuddy-json"})
MAX_CONFIG_BYTES = 2_000_000


@dataclass(frozen=True)
class HookInstallationPlan:
    profile: IntegrationProfile
    config_path: Path
    home: Path
    python: Path
    protocol: str
    marker: str
    builtin: bool

    @property
    def launcher_path(self) -> Path:
        return self.home / "launchers" / self.marker

    @property
    def profile_path(self) -> Path:
        return self.home / "integrations" / (self.profile.agent_id + ".json")


def prepare_hook_installation(profile: IntegrationProfile, *, config_path: Path,
                              protocol: str | None = None) -> HookInstallationPlan:
    # Only confirmed protocol data enters the installer; discovery must supply the evidence.
    validated = parse_profile(profile_spec(profile))
    if len(json.dumps(profile_spec(validated), ensure_ascii=False).encode("utf-8")) > 65_536:
        raise ValueError("integration profile exceeds 64 KiB")
    builtin = profile is PROFILES.get(profile.agent_id)
    selected = profile.hook_protocol if builtin and protocol is None else protocol
    if not isinstance(selected, str) or selected not in PROTOCOLS:
        raise ValueError("unconfirmed hook protocol")
    marker = profile.launcher if builtin else profile.agent_id + "_learning_hook.py"
    python = Path(sys.executable).resolve()
    if selected == 'codebuddy-json' and python.name.lower() == 'pythonw.exe':
        console = python.with_name('python.exe')
        if not console.is_file():
            raise RuntimeError('WorkBuddy hooks require a Python interpreter with standard streams')
        python = console
    plan = HookInstallationPlan(profile, Path(config_path).expanduser().resolve(),
        memweave_home().resolve(), python, selected, marker, builtin)
    _patch_config(plan, _read_config(plan.config_path))
    return plan


def is_owned_hook(item, marker: str) -> bool:
    if not isinstance(item, dict):
        return False
    for key in ("command", "commandWindows"):
        command = str(item.get(key, ""))
        if command.startswith('powershell.exe ') and len(command) <= 32_768:
            encoded = re.search(r'(?:^|\s)-EncodedCommand\s+([A-Za-z0-9+/=]+)(?:\s|$)', command)
            if encoded:
                try:
                    command = base64.b64decode(encoded[1], validate=True).decode('utf-16le')
                except (ValueError, UnicodeError):
                    continue
        if re.search(r"(?<![\w-])" + re.escape(marker) + r"(?![\w.-])", command):
            return True
    return False


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("config is too large")
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        raise RuntimeError("Agent configuration is unreadable or invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Agent configuration must be a JSON object")
    return value


def _command(plan: HookInstallationPlan) -> str:
    script = str(plan.launcher_path)
    if os.name == "nt" and plan.protocol == "codebuddy-json":
        # CodeBuddy normally runs commands through Bash; use its direct PowerShell path on Windows.
        code = "& '" + str(plan.python).replace("'", "''") + "' '" + script.replace("'", "''") + "' hook"
        encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
        return "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded
    if os.name == "nt" and plan.protocol == "gemini-json":
        return "& '" + str(plan.python).replace("'", "''") + "' '" + script.replace("'", "''") + "' hook"
    args = [str(plan.python), script, "hook"]
    if os.name == "nt" and plan.profile.prefer_python_launcher and (launcher := shutil.which("py")):
        args = [str(Path(launcher).resolve()), "-3", script, "hook"]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def _hook(plan: HookInstallationPlan, event: str) -> dict:
    command = _command(plan)
    recalling = event == plan.profile.recall_event
    if plan.protocol == "gemini-json":
        return {"type": "command", "command": command, "name": "memweave-" + event.lower(),
            "timeout": 10_000, "description": "MemWeave recall" if recalling else "MemWeave background learning"}
    return {"type": "command", "command": command, "commandWindows": command,
        "timeout": plan.profile.recall_timeout if recalling else 120, "async": False,
        "statusMessage": "MemWeave is recalling validated knowledge" if recalling else "MemWeave is reviewing this turn"}


def _patch_config(plan: HookInstallationPlan, config: dict) -> dict:
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("Agent hooks must be an object")
    for event in (plan.profile.recall_event, plan.profile.learn_event):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise RuntimeError("Agent hook events must contain arrays")
        cleaned = []
        for group in entries:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                cleaned.append(group)
                continue
            remaining = [item for item in group["hooks"] if not is_owned_hook(item, plan.marker)]
            if remaining or remaining == group["hooks"]:
                cleaned.append({**group, "hooks": remaining})
        target = next((group for group in cleaned if isinstance(group, dict)
            and group.get("matcher", "") == "" and isinstance(group.get("hooks"), list)), None)
        if target is None:
            target = {"matcher": "", "hooks": []}
            cleaned.append(target)
        target["hooks"].append(_hook(plan, event))
        hooks[event] = cleaned
    return config


def _launcher_source(plan: HookInstallationPlan) -> str:
    selection = ["--agent", plan.profile.agent_id] if plan.builtin else ["--profile", str(plan.profile_path)]
    args = [str(plan.python), "-X", "utf8", "-m", EXECUTOR_MODULE, *selection]
    return ("import os, subprocess, sys\n"
        + 'os.environ["MEMWEAVE_HOME"] = ' + repr(str(plan.home)) + "\n"
        + "raise SystemExit(subprocess.call(" + repr(args) + " + sys.argv[1:]))\n")


def _write_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_bytes() == text.encode("utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(text)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def install_hook_plan(plan: HookInstallationPlan) -> dict:
    if not EXECUTOR_PATH.is_file():
        raise RuntimeError("MemWeave unified executor is missing; reinstall the package")
    config = _read_config(plan.config_path)
    previous = json.dumps(config, ensure_ascii=False, sort_keys=True)
    patched = _patch_config(plan, config)
    changed = previous != json.dumps(patched, ensure_ascii=False, sort_keys=True)
    # Revalidate before writing any artifacts; a prepared plan never trusts stale config.
    executor_changed = _write_if_changed(plan.launcher_path, _launcher_source(plan))
    if not plan.builtin:
        executor_changed = _write_if_changed(plan.profile_path,
            json.dumps(profile_spec(plan.profile), ensure_ascii=False, indent=2) + "\n") or executor_changed
    backup = plan.config_path.with_name(plan.config_path.name + ".memweave-backup")
    if changed:
        if plan.config_path.is_file() and not backup.exists():
            shutil.copy2(plan.config_path, backup)
        _write_if_changed(plan.config_path, json.dumps(patched, ensure_ascii=False, indent=2) + "\n")
    status = inspect_hook_plan(plan)
    return {"configured": status["configured"], "changed": changed,
        "executor_changed": executor_changed, "executor": EXECUTOR_MODULE,
        "config_path": str(plan.config_path), "backup_path": str(backup) if backup.is_file() else None}


def inspect_hook_plan(plan: HookInstallationPlan) -> dict:
    try:
        config = _read_config(plan.config_path)
        launcher_ready = EXECUTOR_PATH.is_file() and plan.launcher_path.is_file() \
            and plan.launcher_path.read_text(encoding="utf-8") == _launcher_source(plan)
        if not plan.builtin:
            launcher_ready = launcher_ready and _read_config(plan.profile_path) == profile_spec(plan.profile)
    except (OSError, RuntimeError, UnicodeError):
        return {"configured": False, "executor_ready": False}
    valid = launcher_ready and not config.get("disableAllHooks", False)
    if plan.protocol == "codebuddy-json":
        valid = valid and not config.get("allowManagedHooksOnly", False)
    settings = config.get("hooksConfig", {})
    if plan.protocol == "gemini-json":
        valid = valid and isinstance(settings, dict) and settings.get("enabled", True) is True
    hooks = config.get("hooks", {})
    for event in (plan.profile.recall_event, plan.profile.learn_event):
        entries = hooks.get(event, []) if isinstance(hooks, dict) else []
        matches = [(group, item) for group in entries if isinstance(group, dict)
            and isinstance(group.get("hooks"), list) for item in group["hooks"]
            if is_owned_hook(item, plan.marker)] if isinstance(entries, list) else []
        if len(matches) != 1:
            valid = False
            continue
        group, item = matches[0]
        expected = _hook(plan, event)
        valid = valid and group.get("matcher", "") in ("", "*") and item == expected
        if plan.protocol == "gemini-json":
            disabled = settings.get("disabled", []) if isinstance(settings, dict) else []
            valid = valid and isinstance(disabled, list) and item.get("name") not in disabled
    return {"configured": bool(valid), "executor_ready": bool(launcher_ready)}

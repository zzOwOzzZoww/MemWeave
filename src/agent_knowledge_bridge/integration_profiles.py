"""Declarative client vocabulary; no transport or memory policy lives here."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


STANDARD_FIELDS = {name: name for name in (
    "session_id", "turn_id", "prompt_id", "cwd", "prompt", "transcript_path",
    "last_assistant_message", "turn",
)}
DEFAULT_OUTPUT = {"suppressOutput": True, "hookSpecificOutput": {
    "hookEventName": "{{event}}", "additionalContext": "{{context}}"}}
PROFILE_FIELDS = {"agent_id", "recall_event", "learn_event", "transcript_format", "event_field",
                  "fields", "recall_output", "bind_transcript_boundary"}


@dataclass(frozen=True)
class IntegrationProfile:
    agent_id: str
    recall_event: str
    learn_event: str
    transcript_format: str = "auto"
    event_field: str = "hook_event_name"
    fields: dict[str, str] = field(default_factory=lambda: dict(STANDARD_FIELDS))
    recall_output: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_OUTPUT))
    bind_transcript_boundary: bool = False
    # Built-in installation / legacy diagnostics, not customizable code hooks.
    launcher: str = ""
    config_file: str = ""
    config_home: str = ""
    home_env: str = ""
    home_subdir: str = ""
    error_suffix: str = ".hook-errors.jsonl"
    audit_suffix: str = ""
    transcript_resolver: str = ""
    baseline_bypass: bool = False
    hook_protocol: str = "command-json"
    windows_encoded_command: bool = False
    recall_timeout: int = 120

    def normalize(self, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        event = _lookup(payload, self.event_field)
        operation = "recall" if event == self.recall_event else "learn" if event == self.learn_event else ""
        result = {name: value for name, path in self.fields.items()
                  if (value := _lookup(payload, path)) is not None}
        limits = {"session_id": 160, "turn_id": 160, "prompt_id": 160, "cwd": 2000,
                  "prompt": 20_000, "transcript_path": 2000, "last_assistant_message": 20_000}
        for name, value in result.items():
            if name == "turn":
                if not isinstance(value, dict):
                    raise ValueError("normalized turn must be an object")
            elif name == "last_assistant_message" and not isinstance(value, str):
                result[name] = _assistant_text(value)
            elif not isinstance(value, str) or len(value) > limits[name]:
                raise ValueError(f"invalid normalized {name} field")
        result["hook_event_name"] = event
        if not result.get("turn_id") and result.get("prompt_id"):
            result["turn_id"] = "prompt:" + str(result["prompt_id"])
        return operation, result

    def encode_context(self, context: str) -> dict[str, Any]:
        if not context:
            return {}

        def render(value):
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, list):
                return [render(item) for item in value]
            if value == "{{context}}":
                return context
            if value == "{{event}}":
                return self.recall_event
            return value

        return render(self.recall_output)


def _assistant_text(value: Any, depth: int = 0) -> str:
    """Extract only recognizable text from structured client response fields."""
    if isinstance(value, str):
        return value[:20_000]
    if depth >= 8:
        return ""
    if isinstance(value, list):
        parts = [_assistant_text(item, depth + 1) for item in value[:32]]
        return "\n".join(part for part in parts if part)[:20_000]
    if isinstance(value, dict):
        for key in ("text", "response", "content", "message"):
            if key in value:
                result = _assistant_text(value[key], depth + 1)
                if result:
                    return result
    return ""


def _lookup(payload: dict, path: str):
    value = payload
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


PROFILES = {
    "claude-code": IntegrationProfile("claude-code", "UserPromptSubmit", "Stop", "claude",
        bind_transcript_boundary=True, launcher="claude_learning_hook.py", config_file="settings.json",
        config_home="~/.claude", home_env="CLAUDE_CONFIG_DIR"),
    "codex": IntegrationProfile("codex", "UserPromptSubmit", "Stop", "codex",
        launcher="codex_learning_hook.py", config_file="hooks.json", error_suffix=".codex-hook-errors.jsonl",
        audit_suffix=".codex-hook-runs.jsonl", transcript_resolver="codex", baseline_bypass=True,
        config_home="~/.codex", home_env="CODEX_HOME", windows_encoded_command=True, recall_timeout=30),
    "gemini-cli": IntegrationProfile("gemini-cli", "BeforeAgent", "AfterAgent", "gemini",
        bind_transcript_boundary=True, launcher="gemini_learning_hook.py", config_file="settings.json",
        config_home="~/.gemini", home_env="GEMINI_CLI_HOME", home_subdir=".gemini",
        fields={**STANDARD_FIELDS, "last_assistant_message": "prompt_response"}, hook_protocol="gemini-json"),
    "workbuddy": IntegrationProfile("workbuddy", "UserPromptSubmit", "Stop", "codebuddy",
        bind_transcript_boundary=True, launcher="workbuddy_learning_hook.py", config_file="settings.json",
        config_home="~/.workbuddy", home_env="WORKBUDDY_CONFIG_DIR",
        hook_protocol="codebuddy-json", audit_suffix=".workbuddy-hook-runs.jsonl"),
}


def load_profile(path: str | Path) -> IntegrationProfile:
    with Path(path).open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("integration profile exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except RecursionError as exc:
        raise ValueError("integration profile is too deeply nested") from exc
    return parse_profile(value)


def profile_spec(profile: IntegrationProfile) -> dict[str, Any]:
    return {key: getattr(profile, key) for key in sorted(PROFILE_FIELDS)}


def parse_profile(value: Any) -> IntegrationProfile:
    if not isinstance(value, dict) or set(value) - PROFILE_FIELDS:
        raise ValueError("invalid integration profile fields")
    if not isinstance(value.get("agent_id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value["agent_id"]):
        raise ValueError("invalid integration agent_id")
    for key in ("recall_event", "learn_event"):
        if not isinstance(value.get(key), str) or not 0 < len(value[key]) <= 160:
            raise ValueError("profile requires recall_event and learn_event")
    if value["recall_event"] == value["learn_event"]:
        raise ValueError("recall and learn events must differ")
    selected_format = value.get("transcript_format", "auto")
    if not isinstance(selected_format, str) or selected_format not in {"auto", "claude", "codex", "gemini", "codebuddy"}:
        raise ValueError("unsupported transcript format")
    fields = value.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - set(STANDARD_FIELDS):
        raise ValueError("unsupported normalized fields")
    for selected in (value.get("event_field", "hook_event_name"), *fields.values()):
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", selected):
            raise ValueError("field mappings must be dotted object paths")
    output = value.get("recall_output", DEFAULT_OUTPUT)

    def has_context(item, depth=0):
        if depth > 32:
            raise ValueError("recall_output is too deeply nested")
        if isinstance(item, (dict, list)):
            found = False
            for child in item.values() if isinstance(item, dict) else item:
                found = has_context(child, depth + 1) or found
            return found
        return item == "{{context}}"

    if not isinstance(output, dict) or not has_context(output):
        raise ValueError("recall_output must contain a context placeholder")
    if type(value.get("bind_transcript_boundary", False)) is not bool:
        raise ValueError("bind_transcript_boundary must be boolean")
    return IntegrationProfile(**{**value, "fields": {**STANDARD_FIELDS, **fields}})

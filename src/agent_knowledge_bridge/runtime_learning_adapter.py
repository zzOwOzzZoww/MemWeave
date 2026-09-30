"""Agent-neutral turn input backed by the existing evidence-governed learner."""
from __future__ import annotations

import hashlib
from typing import Any

from .learning_engine import LearningEngine
from .integration_profiles import PROFILES
from .claude_transcript import (
    TranscriptTurn, ToolEvent, _json_summary, _objective_kind,
    parse_latest_turn, redact_text, tool_success,
)
from .codex_transcript import parse_latest_codex_turn
from .gemini_transcript import parse_latest_gemini_turn


def transcript_parser(agent_id: str, transcript_format: str = "auto"):
    profile = PROFILES.get(agent_id)
    selected = (profile.transcript_format if profile else None) if transcript_format == "auto" else transcript_format
    parsers = {"claude": parse_latest_turn, "codex": parse_latest_codex_turn,
               "gemini": parse_latest_gemini_turn}
    if selected not in parsers:
        raise ValueError("Agent transcript format is unknown; supply turn or an explicit transcript_format")
    return parsers[selected]


def normalized_tool(item: dict[str, Any]) -> ToolEvent:
    name = item["name"]
    tool_input = item.get("input", {})
    output = item.get("output", "")
    command_name = "Shell" if name == "run_shell_command" else name
    metadata = {key: item[key] for key in ("exit_code", "is_error", "status", "interrupted")
                if item.get(key) is not None}
    if command_name in {"Bash", "Shell", "PowerShell", "exec_command", "shell_command"} \
            and metadata.get("is_error") is False:
        metadata.pop("is_error")  # Transport success is not a shell exit code.
    return ToolEvent(
        tool_use_id=item["id"], tool_name=name,
        input_summary=_json_summary(tool_input), output_summary=redact_text(output, 2000),
        success=tool_success(output, metadata),
        objective_kind=_objective_kind(command_name, tool_input),
    )


class RuntimeLearningAdapter(LearningEngine):
    def __init__(self, *, transcript_format: str = "auto", **kwargs):
        agent = kwargs["agent_id"]
        profile = PROFILES.get(agent)
        if "transcript_parser" not in kwargs:
            kwargs["transcript_parser"] = transcript_parser(agent, transcript_format) \
                if profile or transcript_format != "auto" else None
        kwargs.setdefault("bind_transcript_boundary", profile.bind_transcript_boundary if profile else False)
        if kwargs["bind_transcript_boundary"] and kwargs["transcript_parser"] is None:
            raise ValueError("transcript boundary binding requires a known transcript_format")
        super().__init__(**kwargs)

    def recall(self, hook_input):
        # Preserve the published Runtime API envelope; native output is encoded by profiles.
        return PROFILES["claude-code"].encode_context(self.recall_context(hook_input))

    def _parse_turn(self, hook_input: dict[str, Any]) -> TranscriptTurn:
        inline = hook_input.get("turn")
        if inline is not None:
            key = hook_input.get("turn_id")
            return TranscriptTurn(
                user_text=redact_text(inline["user_text"], 5000),
                assistant_text=redact_text(inline.get("assistant_text", ""), 5000),
                tools=tuple(normalized_tool(item) for item in inline.get("tools", [])),
                source_turn_key="runtime:" + hashlib.sha256(str(key).encode()).hexdigest() if key else None,
            )
        parser = transcript_parser(self.agent_id, hook_input.get("transcript_format", "auto"))
        options = {"fallback_assistant": str(hook_input.get("last_assistant_message") or "")}
        if hook_input.get("transcript_end") is not None:
            options["end_offset"] = hook_input["transcript_end"]
        if parser is parse_latest_gemini_turn:
            for field in ("message_count", "expected_turn_hash"):
                if hook_input.get("transcript_" + field) is not None:
                    options[field] = hook_input["transcript_" + field]
        return parser(hook_input.get("transcript_path") or "", **options)

"""Bounded Gemini JSON/JSONL parsing; native thoughts never become evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .claude_transcript import (
    MAX_TRANSCRIPT_BYTES, TranscriptTurn, ToolEvent, _json_summary, _objective_kind,
    _tail_lines, redact_text, tool_success,
)


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_content(part) for part in value)))
    if not isinstance(value, dict):
        return ""
    if value.get("thought") is True:
        return ""
    if isinstance(value.get("text"), str):
        return value["text"]
    if "functionResponse" in value:
        return _content(value["functionResponse"].get("response"))
    return "\n".join(_content(value[key]) for key in ("output", "error", "llmContent") if key in value)


def _messages(path: Path, *, max_bytes: int, end_offset: int | None = None):
    if not path.is_file():
        raise ValueError("Gemini transcript not found")
    if path.suffix == ".json":
        # Legacy JSON is rewritten, so byte offsets cannot freeze a queued turn.
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Gemini JSON transcript exceeds the bounded read limit")
        value = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise ValueError("Invalid Gemini transcript messages")
        messages = value["messages"]
    else:
        by_id = {}
        for line in _tail_lines(path, max_bytes, end_offset):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Invalid Gemini transcript record")
            if "$rewindTo" in value:
                keys = list(by_id)
                boundary = keys.index(value["$rewindTo"]) if value["$rewindTo"] in by_id else 0
                for key in keys[boundary:]:
                    del by_id[key]
            elif isinstance(value.get("$set"), dict) and "messages" in value["$set"]:
                replacement = value["$set"]["messages"]
                if not isinstance(replacement, list):
                    raise ValueError("Invalid Gemini checkpoint messages")
                by_id = {message["id"]: message for message in replacement
                         if isinstance(message, dict) and message.get("id")}
            elif isinstance(value.get("messages"), list):
                by_id.update({message["id"]: message for message in value["messages"]
                              if isinstance(message, dict) and message.get("id")})
            elif value.get("id"):
                by_id[value["id"]] = value
        messages = list(by_id.values())
    if any(not isinstance(message, dict) for message in messages):
        raise ValueError("Invalid Gemini message")
    return messages


def _result_metadata(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for part in value for item in _result_metadata(part)]
    if not isinstance(value, dict):
        return []
    return [value, *(item for child in value.values() for item in _result_metadata(child))]


def _turn(messages: list[dict], path: Path, fallback_assistant: str) -> TranscriptTurn:
    indices = [index for index, message in enumerate(messages)
               if message.get("type") == "user" and _content(message.get("content")).strip()]
    if not indices:
        raise ValueError("Gemini transcript contains no user prompt")
    user_index = indices[-1]

    def key(message):
        return "transcript:" + hashlib.sha256(
            (str(path.resolve()) + "\x1f" + str(message["id"])).encode()).hexdigest() if message.get("id") else None

    assistant = []
    tools = {}
    for message in messages[user_index + 1:]:
        if message.get("type") != "gemini":
            continue
        assistant.append(_content(message.get("content")))
        calls = message.get("toolCalls") or []
        if not isinstance(calls, list):
            raise ValueError("Invalid Gemini toolCalls")
        for call in calls:
            if not isinstance(call, dict) or not call.get("id"):
                continue
            name = str(call.get("name") or "unknown")
            output = _content(call.get("result"))
            state = call.get("status")
            metadata = [{"status": "failed" if state in {"error", "cancelled"} else
                         "pending" if state not in {"success", "completed"} else "completed"}]
            tools[str(call["id"])] = ToolEvent(
                tool_use_id=str(call["id"]), tool_name=name,
                input_summary=_json_summary(call.get("args", {})),
                output_summary=redact_text(output, 2000),
                success=tool_success(output, *metadata, *_result_metadata(call.get("result"))),
                objective_kind=_objective_kind("Shell" if name == "run_shell_command" else name, call.get("args")),
            )
    return TranscriptTurn(
        user_text=redact_text(_content(messages[user_index].get("content")), 5000),
        assistant_text=redact_text("\n".join(filter(None, assistant)) or fallback_assistant, 5000),
        tools=tuple(tools.values()), source_turn_key=key(messages[user_index]),
        previous_turn_key=key(messages[indices[-2]]) if len(indices) > 1 else None,
    )


def parse_latest_gemini_turn(
    transcript_path: str | Path, *, fallback_assistant: str = "", max_bytes: int = MAX_TRANSCRIPT_BYTES,
    end_offset: int | None = None, message_count: int | None = None, expected_turn_hash: str | None = None,
) -> TranscriptTurn:
    path = Path(transcript_path)
    messages = _messages(path, max_bytes=max_bytes, end_offset=end_offset)
    if message_count is not None:
        if not 0 < message_count <= len(messages):
            raise ValueError("Gemini queued transcript was truncated")
        messages = messages[:message_count]
    turn = _turn(messages, path, fallback_assistant)
    if expected_turn_hash is not None and turn.turn_hash != expected_turn_hash:
        raise ValueError("Gemini queued turn changed before learning")
    return turn


def freeze_gemini_turn(path: Path, end_offset: int) -> dict:
    messages = _messages(path, max_bytes=MAX_TRANSCRIPT_BYTES, end_offset=end_offset)
    turn = _turn(messages, path, "")
    return {"transcript_message_count": len(messages), "transcript_expected_turn_hash": turn.turn_hash}

from __future__ import annotations

"""Convert Codex rollout JSONL into the adapter's protocol-neutral turn model.

Codex stores Responses-style items rather than Claude's message-shaped JSONL.
Only bounded text, tool arguments, and tool outputs are retained; encrypted
reasoning, credentials, and the raw transcript are never published.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from agent_knowledge_bridge.claude_transcript import (
    MAX_TRANSCRIPT_BYTES,
    ToolEvent,
    TranscriptTurn,
    _objective_kind,
    _text_content,
    redact_text,
    tool_success,
)


def resolve_transcript_path(hook_input: dict) -> str:
    """Recover an omitted desktop rollout path from a bounded session scan."""
    supplied = str(hook_input.get("transcript_path") or "").strip()
    if supplied and Path(supplied).is_file():
        return supplied
    session_id = str(hook_input.get("session_id") or "").strip()
    if not session_id:
        return supplied
    root = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    if not root.is_dir():
        return supplied
    try:
        paths = sorted(root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:80]
    except OSError:
        return supplied
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                item = json.loads(stream.readline(20_000))
            payload = item.get("payload") or {}
            if str(payload.get("session_id") or payload.get("id") or "") == session_id:
                return str(path)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return supplied


def _tail_lines(path: Path, max_bytes: int = MAX_TRANSCRIPT_BYTES, end_offset: int | None = None) -> list[str]:
    from .claude_transcript import _tail_lines as read_tail
    return read_tail(path, max_bytes, end_offset)


def _string(value: Any, limit: int = 4000) -> str:
    if isinstance(value, str):
        return redact_text(value, limit)
    if value is None:
        return ""
    try:
        return redact_text(json.dumps(value, ensure_ascii=False, sort_keys=True), limit)
    except (TypeError, ValueError):
        return redact_text(str(value), limit)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"input_text", "output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _call_arguments(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = str(payload.get("name") or payload.get("tool_name") or "unknown")
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"command": raw} if name in {"exec_command", "shell_command"} else {"input": raw}
    elif isinstance(raw, dict):
        parsed = raw
    else:
        parsed = {}
    return name, parsed


def _tool_success(output: str, payload: dict[str, Any]) -> bool | None:
    return tool_success(output, payload)


def _literal_command_wrapper(arguments: dict[str, Any], output: str):
    """Recognize two single-call wrapper shapes; never evaluate JavaScript.

    Batches, variable commands and arbitrary code have no safe call/result
    pairing in the outer transcript. They deliberately remain unverified.
    """
    script = arguments.get('input', '')
    if not isinstance(script, str) or len(script) > 20000:
        return None
    script = re.sub(r'^\s*// @exec:[^\n]*\n', '', script)
    direct = re.fullmatch(r'\s*text\(\s*await tools\.exec_command\((\{.*\})\)\s*\)\s*;?\s*', script, re.S)
    assigned = re.fullmatch(
        r'\s*(?:const|let) (\w+)\s*=\s*await tools\.exec_command\((\{.*\})\)\s*;\s*text\(\s*\1\s*\)\s*;?\s*',
        script, re.S)
    if not direct and not assigned:
        return None
    raw = direct[1] if direct else assigned[2]
    fields = {}
    position = 1
    while position < len(raw) - 1:
        key = re.match(r'\s*(?:"([A-Za-z_]+)"|([A-Za-z_]+))\s*:\s*', raw[position:])
        if not key:
            return None
        position += key.end()
        try:
            value, length = json.JSONDecoder().raw_decode(raw[position:])
        except ValueError:
            return None
        name = key[1] or key[2]
        if name in fields or isinstance(value, (dict, list)):
            return None
        fields[name] = value
        position += length
        separator = re.match(r'\s*([,}])', raw[position:])
        if not separator:
            return None
        position += separator.end()
        if separator[1] == '}':
            if position != len(raw):
                return None
            break
    if not isinstance(fields.get('cmd'), str) or not fields['cmd'].strip():
        return None
    if not output.startswith('Script completed\n') or '\nOutput:\n' not in output:
        return None
    try:
        result = json.loads(output.split('\nOutput:\n', 1)[1])
    except ValueError:
        return None
    if not isinstance(result, dict) or 'output' not in result or 'exit_code' not in result and 'session_id' not in result:
        return None
    return fields, result


def parse_latest_codex_turn(
    transcript_path: str | Path, *, fallback_assistant: str = "", max_bytes: int = MAX_TRANSCRIPT_BYTES,
    end_offset: int | None = None,
) -> TranscriptTurn:
    path = Path(transcript_path)
    if not path.is_file():
        raise ValueError(f"Codex transcript not found: {path}")

    entries: list[dict[str, Any]] = []
    for line in _tail_lines(path, max_bytes, end_offset):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)

    # Locate the final user request. The hook's prompt is preferred by the
    # caller, but parsing the rollout makes Stop-hook learning deterministic.
    user_index = -1
    user_text = ""
    for index in range(len(entries) - 1, -1, -1):
        payload = entries[index].get("payload") or {}
        if payload.get("type") == "user_message":
            candidate = payload.get("message") or ""
        elif payload.get("type") == "message" and payload.get("role") == "user":
            candidate = _content_text(payload.get("content"))
        else:
            continue
        if str(candidate).strip():
            user_index = index
            user_text = str(candidate)
            break
    if user_index < 0:
        raise ValueError("Codex transcript contains no user prompt")

    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    order: list[str] = []
    outputs: dict[str, tuple[str, dict[str, Any]]] = {}
    assistant_parts: list[str] = []
    for entry in entries[user_index + 1 :]:
        payload = entry.get("payload") or {}
        item_type = payload.get("type")
        if item_type == "message" and payload.get("role") == "assistant":
            text = _content_text(payload.get("content"))
            if text:
                assistant_parts.append(text)
        elif item_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                assistant_parts.append(message)
        elif item_type in {"function_call", "custom_tool_call"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                calls[call_id] = _call_arguments(payload)
                order.append(call_id)
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                raw_output = payload.get("output")
                output_text = _content_text(raw_output) or _string(raw_output)
                outputs[call_id] = (output_text, payload)

    tools: list[ToolEvent] = []
    for call_id in order:
        tool_name, arguments = calls[call_id]
        output, output_payload = outputs.get(call_id, ("", {}))
        if tool_name in {'exec', 'functions.exec'}:
            unwrapped = _literal_command_wrapper(arguments, output)
            if unwrapped:
                arguments, result = unwrapped
                tool_name = 'exec_command'
                output = json.dumps(result, ensure_ascii=False)
                output_payload = result
        objective_kind = _objective_kind(tool_name, arguments)
        tools.append(
            ToolEvent(
                tool_use_id=call_id,
                tool_name=tool_name,
                input_summary=_string(arguments, 2000),
                output_summary=redact_text(output, 2000),
                success=_tool_success(output, output_payload),
                objective_kind=objective_kind,
            )
        )

    return TranscriptTurn(
        user_text=redact_text(user_text, 5000),
        assistant_text=redact_text("\n".join(assistant_parts).strip() or fallback_assistant, 5000),
        tools=tuple(tools),
    )

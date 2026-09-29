from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_TRANSCRIPT_BYTES = 2_000_000
MAX_TEXT_CHARS = 12_000
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~-]{12,}"),
    re.compile(
        r'''(?i)([\"']?(?:api[_-]?key|access[_-]?token|authorization|password|secret)[\"']?)(\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)'''
    ),
)
OBJECTIVE_COMMAND_RE = re.compile(
    r"(?i)(^|[\\/\s])(pytest|unittest|test|verify|check|lint|build)([\\/\s.:_-]|$)"
)


@dataclass(frozen=True)
class ToolEvent:
    tool_use_id: str
    tool_name: str
    input_summary: str
    output_summary: str
    success: bool | None
    objective_kind: str | None


@dataclass(frozen=True)
class TranscriptTurn:
    user_text: str
    assistant_text: str
    tools: tuple[ToolEvent, ...]
    source_turn_key: str | None = None
    previous_turn_key: str | None = None

    @property
    def turn_hash(self) -> str:
        canonical = "\x1f".join(
            [
                self.user_text,
                self.assistant_text,
                self.source_turn_key or "",
                *(
                    f"{tool.tool_use_id}:{tool.tool_name}:{tool.success}:"
                    f"{tool.input_summary}:{tool.output_summary}"
                    for tool in self.tools
                ),
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def review_text(self, event_ids: list[str]) -> str:
        tool_lines = []
        for event_id, tool in zip(event_ids, self.tools, strict=True):
            tool_lines.append(
                "\n".join(
                    [
                        f"EVENT_ID: {event_id}",
                        f"TOOL: {tool.tool_name}",
                        f"SUCCESS: {tool.success}",
                        f"OBJECTIVE_KIND: {tool.objective_kind or 'none'}",
                        f"INPUT: {tool.input_summary}",
                        f"OUTPUT: {tool.output_summary}",
                    ]
                )
            )
        return (
            f"USER REQUEST:\n{self.user_text}\n\n"
            f"ASSISTANT RESULT:\n{self.assistant_text}\n\n"
            "TOOL EVIDENCE:\n" + ("\n\n".join(tool_lines) or "(none)")
        )[:MAX_TEXT_CHARS]


def redact_text(value: str, limit: int = 3000) -> str:
    value = value.replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            value = pattern.sub(r"\1\2[REDACTED]", value)
        elif pattern.groups == 1:
            value = pattern.sub(r"\1[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value[:limit]


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.search(r'(?i)api.?key|token|password|secret|authorization', str(key))
                else redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return redact_text(value, len(value)) if isinstance(value, str) else value


def _tail_lines(path: Path, max_bytes: int = MAX_TRANSCRIPT_BYTES, end_offset: int | None = None) -> list[str]:
    size = path.stat().st_size
    if end_offset is not None:
        if not 0 <= end_offset <= size:
            raise ValueError('transcript was truncated before queued learning could read it')
        size = end_offset
    with path.open("rb") as stream:
        if size > max_bytes:
            stream.seek(size - max_bytes)
            stream.readline()
        return stream.read(min(max_bytes, size-stream.tell())).decode("utf-8", errors="replace").splitlines()


def _text_content(content: Any, *, include_tool_results: bool = False) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif include_tool_results and block.get("type") == "tool_result":
            raw = block.get("content")
            if isinstance(raw, str):
                parts.append(raw)
    return "\n".join(parts)


def _json_summary(value: Any, limit: int = 2000) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return redact_text(rendered, limit)


def _objective_kind(tool_name: str, tool_input: Any) -> str | None:
    if tool_name not in {"Bash", "Shell", "PowerShell", "exec_command", "shell_command"}:
        return None
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
    return "test" if OBJECTIVE_COMMAND_RE.search(command) else None


def tool_success(output: str, *metadata: dict[str, Any]) -> bool | None:
    """A transport completing is not a command passing. Explicit failures win."""
    if any(m.get("is_error") is True or m.get("interrupted") is True
           or m.get("status") in {"failed", "error"} for m in metadata):
        return False
    if (any(m.get("backgroundTaskId") or m.get("status") in {"running", "pending"}
            for m in metadata) or "Script running with cell ID" in output):
        return None
    codes = []
    for item in metadata:
        for key in ("exit_code", "exitCode"):
            value = item.get(key)
            if type(value) is int or isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                codes.append(int(value))
    # Structured command results may be encoded as a JSON output string.
    try:
        parsed = json.loads(output)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        if parsed.get("session_id") and parsed.get("exit_code") is None:
            return None
        for key in ("exit_code", "exitCode"):
            value = parsed.get(key)
            if type(value) is int:
                codes.append(value)
    # Match command-wrapper metadata only, not arbitrary prose or code.
    codes.extend(int(x) for x in re.findall(
        r"(?im)^\s*(?:exit code:\s*|process exited with code\s+)(-?\d+)\s*$", output))
    if codes:
        return all(code == 0 for code in codes)
    if any(m.get("is_error") is False for m in metadata):
        return True
    return None


def parse_latest_turn(
    transcript_path: str | Path, *, fallback_assistant: str = "", max_bytes: int = MAX_TRANSCRIPT_BYTES,
    end_offset: int | None = None,
) -> TranscriptTurn:
    path = Path(transcript_path)
    if not path.is_file():
        raise ValueError(f"transcript not found: {path}")

    entries: list[dict[str, Any]] = []
    for line in _tail_lines(path, max_bytes, end_offset):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            entries.append(item)

    user_index = -1
    user_text = ""
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        message = entry.get("message")
        if entry.get("type") != "user" or not isinstance(message, dict):
            continue
        candidate = _text_content(message.get("content"))
        if candidate.strip():
            user_index = index
            user_text = candidate
            break
    if user_index < 0:
        raise ValueError("transcript contains no user prompt")

    def source_key(entry):
        if not entry.get('uuid'):
            return None
        return "transcript:" + hashlib.sha256(
            (str(path.resolve()) + "\x1f" + str(entry['uuid'])).encode('utf-8')).hexdigest()

    previous_key = None
    for entry in reversed(entries[:user_index]):
        message = entry.get('message')
        if (entry.get('type') == 'user' and isinstance(message, dict)
                and _text_content(message.get('content')).strip()):
            previous_key = source_key(entry)
            break

    tool_calls: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    tool_results: dict[str, tuple[str, bool | None]] = {}
    assistant_parts: list[str] = []
    for entry in entries[user_index + 1 :]:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") == "assistant":
            text = _text_content(content)
            if text:
                assistant_parts.append(text)
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_id = str(block.get("id") or "")
                    if not tool_id:
                        continue
                    tool_calls[tool_id] = block
                    tool_order.append(tool_id)
        elif message.get("role") == "user" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                if not tool_id:
                    continue
                top_result = entry.get("toolUseResult")
                raw_output = _text_content([block], include_tool_results=True)
                success = tool_success(raw_output, block,
                                       top_result if isinstance(top_result, dict) else {})
                tool_results[tool_id] = (
                    raw_output,
                    success,
                )

    tools: list[ToolEvent] = []
    for tool_id in tool_order:
        call = tool_calls[tool_id]
        tool_name = str(call.get("name") or "unknown")
        raw_result, success = tool_results.get(tool_id, ("", None))
        objective_kind = _objective_kind(tool_name, call.get("input"))
        tools.append(
            ToolEvent(
                tool_use_id=tool_id,
                tool_name=tool_name,
                input_summary=_json_summary(call.get("input")),
                output_summary=redact_text(raw_result, 2000),
                success=success,
                objective_kind=objective_kind,
            )
        )

    assistant_text = "\n".join(assistant_parts).strip() or fallback_assistant
    return TranscriptTurn(
        user_text=redact_text(user_text, 5000),
        assistant_text=redact_text(assistant_text, 5000),
        tools=tuple(tools),
        source_turn_key=source_key(entries[user_index]),
        previous_turn_key=previous_key,
    )

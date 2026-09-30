"""Bounded Responses-style JSONL used by the CodeBuddy/WorkBuddy runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .claude_transcript import (
    MAX_TRANSCRIPT_BYTES, ToolEvent, TranscriptTurn, _json_summary, _objective_kind,
    _tail_lines, redact_text, tool_success,
)
from .codex_transcript import _call_arguments, _content_text


def _metadata(item):
    provider = item.get('providerData')
    return provider if isinstance(provider, dict) else {}


def _is_user(item):
    return (item.get('type') == 'message' and item.get('role') == 'user'
            and not _metadata(item).get('isMeta') and not _metadata(item).get('isCompactInternal')
            and _content_text(item.get('content')).strip())


def parse_latest_codebuddy_turn(
    transcript_path: str | Path, *, fallback_assistant: str = '', max_bytes: int = MAX_TRANSCRIPT_BYTES,
    end_offset: int | None = None,
) -> TranscriptTurn:
    path = Path(transcript_path)
    if not path.is_file():
        raise ValueError('CodeBuddy transcript not found')
    entries = []
    for line in _tail_lines(path, max_bytes, end_offset):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get('type') in {
            'message', 'function_call', 'function_call_output', 'function_call_result',
        }:
            entries.append(item)

    # Follow the latest persisted branch rather than mixing superseded replies into evidence.
    if entries and any('parentId' in item for item in entries):
        by_id = {item['id']: item for item in entries if isinstance(item.get('id'), str)}
        branch, seen = [], set()
        current = entries[-1]
        while current is not None:
            key = current.get('id')
            if key in seen:
                raise ValueError('CodeBuddy transcript branch contains a cycle')
            seen.add(key)
            branch.append(current)
            current = by_id.get(current.get('parentId'))
        entries = list(reversed(branch))
    indices = [index for index, item in enumerate(entries) if _is_user(item)]
    if not indices:
        raise ValueError('CodeBuddy transcript contains no user prompt')
    start = indices[-1]

    def key(item):
        return 'transcript:' + hashlib.sha256(
            (str(path.resolve()) + '\x1f' + str(item['id'])).encode()).hexdigest() if item.get('id') else None

    calls, outputs, assistant = {}, {}, []
    for item in entries[start + 1:]:
        if _metadata(item).get('isMeta'):
            continue
        kind = item.get('type')
        call_id = str(item.get('callId') or item.get('call_id') or '')
        if kind == 'message' and item.get('role') == 'assistant':
            assistant.append(_content_text(item.get('content')))
        elif kind == 'function_call' and call_id:
            calls[call_id] = _call_arguments(item)
        elif kind in {'function_call_output', 'function_call_result'} and call_id:
            raw = item.get('output', '')
            output = _content_text(raw) if isinstance(raw, (str, list)) else str(raw.get('text') or '') if isinstance(raw, dict) else ''
            provider = _metadata(item)
            tool_result = provider.get('toolResult')
            metadata = [item, provider, tool_result if isinstance(tool_result, dict) else {}]
            if isinstance(tool_result, dict) and isinstance(tool_result.get('rawResponse'), dict):
                metadata.append(tool_result['rawResponse'])
            if isinstance(raw, dict):
                metadata.append(raw)
            metadata = [{**m, **({'is_error': m['isError']} if type(m.get('isError')) is bool else {})}
                        for m in metadata]
            # A shell tool finishing is not objective proof of its command's exit status.
            name = calls.get(call_id, ('', {}))[0]
            if name in {'Bash', 'Shell', 'PowerShell', 'exec_command', 'shell_command'}:
                metadata = [{k: v for k, v in m.items() if k != 'is_error' or v is not False} for m in metadata]
            outputs[call_id] = (output, tool_success(output, *metadata))
    tools = []
    for call_id, (name, arguments) in calls.items():
        output, success = outputs.get(call_id, ('', None))
        tools.append(ToolEvent(tool_use_id=call_id, tool_name=name, input_summary=_json_summary(arguments),
            output_summary=redact_text(output, 2000), success=success,
            objective_kind=_objective_kind(name, arguments)))
    return TranscriptTurn(
        user_text=redact_text(_content_text(entries[start].get('content')), 5000),
        assistant_text=redact_text('\n'.join(filter(None, assistant)) or fallback_assistant, 5000),
        tools=tuple(tools), source_turn_key=key(entries[start]),
        previous_turn_key=key(entries[indices[-2]]) if len(indices) > 1 else None,
    )

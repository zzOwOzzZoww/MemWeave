"""Real JSONL parsers and Runtime transport, with no paid model calls."""
import json

import pytest
from fastapi.testclient import TestClient

from agent_knowledge_bridge.claude_transcript import tool_success
from agent_knowledge_bridge.codex_transcript import parse_latest_codex_turn
from agent_knowledge_bridge.daemon import create_app
from agent_knowledge_bridge.experiences import metrics
from agent_knowledge_bridge.runtime_client import MemWeaveRuntimeClient
from test_experiences import COMMAND, make_adapter, reviewer, seed


@pytest.mark.parametrize('output,metadata,expected', [
    ('Exit code: 0\nPASS', {}, True),
    ('Exit code: 01\nFAIL', {'is_error': False}, False),
    ('Process exited with code -1', {'is_error': False}, False),
    ('Exit code: 2', {'exit_code': 0}, False),
    ('', {'exitCode': 1, 'is_error': False}, False),
    ('', {'exit_code': 0}, True),
    ('', {'exit_code': 0, 'interrupted': True}, False),
    ('', {'stderr': ''}, None),
    ('Script completed', {}, None),
    ('Example: exit code: 0', {}, None),
    ('{"exit_code": 1}', {'is_error': False}, False),
    ('ok', {'is_error': False}, True),
    ('Exit code: 0', {'is_error': True}, False),
    ('Script running with cell ID 1', {'is_error': False}, None),
    ('', {'backgroundTaskId': 'bg', 'is_error': False}, None),
    ('{"exit_code": null, "session_id": 10}', {'is_error': False}, None),
])
def test_result_requires_command_evidence(output, metadata, expected):
    assert tool_success(output, metadata) is expected


def claude_prompt(key='prompt-1', text='Build widget for Windows'):
    return {'type': 'user', 'uuid': key, 'message': {'role': 'user', 'content': text}}


def claude_tools(outcomes):
    result = []
    for i, success in enumerate(outcomes):
        result.extend([
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': f'tool-{i}', 'name': 'Bash', 'input': {'command': COMMAND}}]}},
            {'type': 'user', 'toolUseResult': {'exitCode': 0 if success else 1},
             'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': f'tool-{i}', 'is_error': not success,
                 'content': 'PASS' if success else 'FAIL'}]}},
        ])
    result.append({'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Done'}})
    return result


def codex_rows(outcomes):
    result = [{'type': 'event_msg', 'payload': {'type': 'user_message',
               'message': 'Build widget for Windows, not Linux'}}]
    for i, success in enumerate(outcomes):
        result.extend([
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command',
                'call_id': f'call-{i}', 'arguments': json.dumps({'cmd': COMMAND})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output',
                'call_id': f'call-{i}', 'output': f'Exit code: {0 if success else 1}\nresult'}},
        ])
    return result


def write(path, entries):
    path.write_text('\n'.join(json.dumps(x) for x in entries) + '\n', encoding='utf-8')


@pytest.mark.parametrize('source,target', [('codex', 'claude-code'), ('claude-code', 'codex')])
def test_jsonl_learning_to_recall_to_validation_via_runtime(tmp_path, source, target):
    path = tmp_path / 'source.jsonl'
    write(path, codex_rows([False, True]) if source == 'codex' else
          [claude_prompt(text='Build widget for Windows, not Linux'), *claude_tools([False, True])])
    source_adapter = make_adapter(tmp_path, source)
    result = source_adapter.learn({'session_id': 'origin', 'transcript_path': str(path)})
    assert result['promoted'] == 1
    consumer = tmp_path / 'consumer.jsonl'
    consumer_rows = ([claude_prompt()] if target == 'claude-code' else [
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Build widget for Windows'}}])
    write(consumer, consumer_rows)
    app = create_app(database_path=tmp_path / 'db.sqlite', api_token='test-only', reviewer=lambda _: {'proposals': []})
    context = {'agent_id': target, 'project_key': 'p', 'session_id': 'consume'}
    if target == 'codex':
        context['turn_id'] = 'native-codex-turn'
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer test-only'}
        recall = client.post('/v1/learning/recall', headers=headers, json={
            **context, 'prompt': 'Build widget for Windows',
            'transcript_path': str(consumer)})
        assert recall.status_code == 200
        assert COMMAND in recall.json()['hookSpecificOutput']['additionalContext']
        write(consumer, consumer_rows + claude_tools([True]) if target == 'claude-code'
              else [{**x, 'payload': {**x['payload'], 'message': 'Build widget for Windows'}}
                    if x['payload']['type'] == 'user_message' else x for x in codex_rows([True])])
        response = client.post('/v1/learning/turn', headers=headers,
                               json={**context, 'transcript_path': str(consumer)})
        assert response.status_code == 200
    assert metrics(source_adapter.store.knowledge, 'p')['cross_agent_passed'] == 1


def test_repeated_claude_prompt_has_distinct_identity_without_public_ids(tmp_path):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'turn.jsonl'
    rows = []
    for i in range(2):
        rows.append(claude_prompt(key=f'prompt-{i}'))
        write(path, rows)
        hook = {'session_id': 'same', 'transcript_path': str(path), 'prompt': 'Build widget for Windows'}
        adapter.recall(hook)
        # A duplicate callback cannot create another trace for this UUID.
        adapter.recall(hook)
        rows.extend(claude_tools([True]))
        # Distinct real tools have distinct IDs even for identical prompts.
        rows[-2]['message']['content'][0]['tool_use_id'] += f'-{i}'
        rows[-3]['message']['content'][0]['id'] += f'-{i}'
        write(path, rows)
        adapter.learn(hook)
        adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 2
    assert len(adapter.reuse.list('p')) == 2


def test_old_completed_prompt_does_not_bind_new_recall(tmp_path):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'old.jsonl'
    write(path, [claude_prompt(), *claude_tools([True])])
    hook = {'session_id': 'new', 'transcript_path': str(path), 'prompt': 'Build widget for Windows'}
    adapter.recall(hook)
    adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 0


@pytest.mark.parametrize('native_id', [False, True])
def test_prompt_flushed_after_submit_is_verified_by_new_transcript_boundary(tmp_path, native_id):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'delayed.jsonl'
    rows = [claude_prompt('previous', 'Earlier request'), *claude_tools([True])]
    write(path, rows)
    hook = {'session_id': 'delayed', 'transcript_path': str(path), 'prompt': 'Build widget for Windows'}
    if native_id:
        hook['prompt_id'] = 'native-prompt-id'
    adapter.recall(hook)
    write(path, rows + [claude_prompt('new'), *claude_tools([True])])
    adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 1


def test_late_stop_cannot_claim_another_identical_prompt(tmp_path):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'late.jsonl'
    first = [claude_prompt('first')]
    write(path, first)
    hook = {'session_id': 'late', 'transcript_path': str(path), 'prompt': 'Build widget for Windows',
            'prompt_id': 'native-first'}
    adapter.recall(hook)
    write(path, first + claude_tools([True]) + [claude_prompt('second'), *claude_tools([True])])
    adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 0


def test_ambiguous_preflush_callbacks_never_receive_credit(tmp_path):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'ambiguous.jsonl'
    hook = {'session_id': 'ambiguous', 'transcript_path': str(path), 'prompt': 'Build widget for Windows'}
    adapter.recall(hook)
    adapter.recall(hook)
    write(path, [claude_prompt('new'), *claude_tools([True])])
    adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 0


def test_late_preflush_stop_cannot_skip_a_prompt(tmp_path):
    seed(tmp_path, agent='codex')
    adapter = make_adapter(tmp_path, 'claude-code', review=lambda _: {'proposals': []})
    path = tmp_path / 'skipped.jsonl'
    rows = [claude_prompt('previous', 'Earlier request'), *claude_tools([True])]
    write(path, rows)
    hook = {'session_id': 'skipped', 'transcript_path': str(path), 'prompt': 'Build widget for Windows',
            'prompt_id': 'native-first'}
    adapter.recall(hook)
    write(path, rows + [claude_prompt('first'), *claude_tools([True]),
                       claude_prompt('second'), *claude_tools([True])])
    adapter.learn(hook)
    assert metrics(adapter.store.knowledge, 'p')['cross_agent_passed'] == 0


def test_client_forwards_transcript_path(monkeypatch):
    client = MemWeaveRuntimeClient(base_url='http://localhost', token='test-only', agent_id='claude-code', project_key='p')
    captured = {}
    monkeypatch.setattr(client, '_post', lambda endpoint, payload: captured.update(payload) or {})
    client.recall(session_id='s', prompt='hello', transcript_path='turn.jsonl')
    assert captured['transcript_path'] == 'turn.jsonl'


@pytest.mark.parametrize('shape,exit_code,expected', [
    ('assigned', 0, True), ('direct', 1, False), ('assigned', None, None),
    ('dynamic', 0, None), ('batch', 0, None), ('concatenated', 0, None),
])
def test_codex_wrapper_uses_inner_command_result_not_script_completion(tmp_path, shape, exit_code, expected):
    call = 'await tools.exec_command({cmd:' + json.dumps(COMMAND) + ', workdir:"C:/project", max_output_tokens:1000})'
    script = f'const result = {call}; text(result);' if shape == 'assigned' else f'text({call});'
    if shape == 'dynamic':
        script = 'const cmd = "ignored"; text(await tools.exec_command({cmd}));'
    elif shape == 'batch':
        script = f'text({call}); text({call});'
    elif shape == 'concatenated':
        script = f'text({call.replace(json.dumps(COMMAND), json.dumps(COMMAND) + "+suffix")});'
    result = {'exit_code': exit_code, 'output': 'PASS' if exit_code == 0 else 'FAIL'}
    if exit_code is None:
        result['session_id'] = 123
    rows = codex_rows([]) + [
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'wrapper', 'input': script}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'wrapper', 'output': [
            {'type': 'input_text', 'text': 'Script completed\nWall time 1 second\nOutput:\n' + json.dumps(result)}]}},
    ]
    path = tmp_path / 'wrapped.jsonl'
    write(path, rows)
    tool = parse_latest_codex_turn(path).tools[0]
    assert tool.success is expected
    if shape in {'assigned', 'direct'}:
        assert tool.objective_kind == 'test'
        assert json.loads(tool.input_summary)['cmd'] == COMMAND
    else:
        assert tool.objective_kind is None

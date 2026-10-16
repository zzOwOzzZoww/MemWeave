"""Live-pilot findings become development regressions, not a new benchmark."""
import copy
import io
import json
import re

import pytest

from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.experiences import applicable, contract_from_content
from agent_knowledge_bridge.learning_engine import DeepSeekReviewer, LearningEngine
from agent_knowledge_bridge.learning_queue import LearningQueue
from test_experiences import CONTRACT, tool
from test_tiered_admission import learn, proposal


@pytest.mark.parametrize('text,scope', [
    ('以后所有项目的回复默认使用简体中文，代码里的标识符保留英文。', 'user'),
    ('For all future projects, always explain changes in English and keep examples concise.', 'user'),
    ('本仓库后续统一采用 SQLite WAL 模式保存本地记忆，不引入远程向量数据库。', 'project'),
    ('以后回答不要使用 emoji，错误信息保持原文。', 'project'),
    ('以后所有项目默认使用简洁回复，并保持错误原文。', 'user'),
])
def test_explicit_compound_rules_activate_without_borrowing_authority(tmp_path, text, scope):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 1
    assert rows[0]['status'] == 'active' and rows[0]['scope'] == scope
    assert rows[0]['content'] == text


@pytest.mark.parametrize('text', [
    'For all future projects, always explain changes in English. My service uses SQLite.',
    '以后所有项目的回复默认使用简体中文，这次写一份英文报告。',
    '以后所有项目的回复默认使用简体中文，本项目使用 SQLite。',
    '以后所有项目默认使用简洁回复，可能需要改成英文。',
])
def test_compound_rule_cannot_authorize_fact_task_or_guess(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 0
    assert all(row['scope'] == 'project' and row['status'] == 'candidate' for row in rows)


@pytest.mark.parametrize('text', [
    '检查 Windows widget 字段顺序，Linux 不适用。',
    'Please inspect the widget output for this task.',
    '运行一次字段顺序检查。',
])
def test_one_off_command_does_not_become_project_fact(tmp_path, text):
    _, result, rows = learn(tmp_path, text, [proposal(text, kind='fact')])
    assert not rows and result['rejection_reasons'] == {'task_instruction_not_knowledge': 1}


@pytest.mark.parametrize('prefix', ['补充确认：', '再次确认：', 'To confirm: '])
def test_confirmation_prefix_deduplicates_with_both_compilation_sources(tmp_path, prefix):
    body = '我们的搜索服务使用 SQLite FTS5。'
    engine = None
    for i, text in enumerate([body, prefix + body]):
        engine, _, _ = learn(tmp_path, text, [proposal(text, kind='fact', title='Storage ' + str(i))])
    with engine.store.knowledge._connect() as db:
        rows = db.execute('SELECT * FROM knowledge_records').fetchall()
        assert len(rows) == 1 and rows[0]['content'] == body
        assert db.execute('SELECT count(*) FROM knowledge_compilation_links').fetchone()[0] == 2


@pytest.mark.parametrize('different', [
    '仅在测试环境：我们的搜索服务使用 SQLite FTS5。',
    '同事确认：我们的搜索服务使用 SQLite FTS5。',
    '补充确认：我们的搜索服务不使用 SQLite FTS5。',
    '补充确认：我们的搜索服务使用 SQLite FTS4。',
])
def test_qualifiers_negation_and_versions_are_not_confirmation_noise(tmp_path, different):
    body = '我们的搜索服务使用 SQLite FTS5。'
    learn(tmp_path, body, [proposal(body, kind='fact')])
    _, _, rows = learn(tmp_path, different, [proposal(different, kind='fact')])
    assert len(rows) == 2


@pytest.mark.parametrize('query,expected', [
    ('修复 Windows widget 字段顺序问题，Linux 不适用。', True),
    ('Windows 的 widget 字段顺序校验又失败了，应该怎么修？', True),
    ('Windows的widget字段顺序校验失败，Linux不适用。', True),
    ('Linux widget 字段顺序失败，参考：修复 Windows widget 字段顺序问题。', False),
    ('修复 Windows widget 字段顺序问题；同时处理 Linux widget。', False),
    ('修复 Windows widget 字段顺序问题；Linux 不适用吗？', False),
    ('修复 Windows widget 字段顺序问题；不能说 Linux 不适用。', False),
    ('修复 Windows widget 字段顺序问题；Linux is not applicable?', False),
    ('修复 Windows widget 字段顺序问题；Windows 不适用。', False),
    ('修复 Windowsish widget 字段顺序问题', False),
    ('修复 Windows widget 颜色问题', False),
])
def test_long_conditions_and_negative_exclusion_are_executable(query, expected):
    contract = copy.deepcopy(CONTRACT)
    contract.update(applies_when=['修复 Windows widget 字段顺序问题', 'Windows widget 字段顺序'],
                    exclude_when=['Linux 不适用'])
    assert applicable(contract, query) is expected


def test_global_scope_does_not_leak_into_next_durable_sentence(tmp_path):
    text = 'For all future projects, always explain changes in English. Always use SQLite.'
    _, result, rows = learn(tmp_path, text, [proposal(text)])
    assert result['promoted'] == 1 and rows[0]['scope'] == 'project'


def test_conflicting_explicit_rules_leave_second_candidate(tmp_path):
    first = '本项目以后统一使用 PostgreSQL 数据库，不使用 MySQL。'
    second = '本项目以后统一使用 MySQL 数据库，不使用 PostgreSQL。'
    _, result, _ = learn(tmp_path, first, [proposal(first, title='Project database PostgreSQL MySQL')])
    assert result['promoted'] == 1
    _, result, rows = learn(tmp_path, second, [proposal(second, title='Project database MySQL PostgreSQL')])
    assert result['promoted'] == 0
    assert sorted(row['status'] for row in rows) == ['active', 'candidate']


def test_same_turn_alternate_titles_do_not_leave_candidate_after_activation(tmp_path):
    text = '以后回答不要使用 emoji，错误信息保持原文。'
    engine, result, rows = learn(tmp_path, text,
        [proposal(text, title='No emoji'), proposal(text, title='Verbatim errors')])
    assert result['proposals'] == 2 and result['promoted'] == 1
    assert len(rows) == 1 and rows[0]['status'] == 'active'
    with engine.store.knowledge._connect() as db:
        assert {row[0] for row in db.execute('SELECT relation FROM knowledge_compilation_links')} == {'produced', 'deduplicated'}


def test_joint_exact_quotes_cover_one_unconditional_directive(tmp_path):
    text = '以后回答不要使用 emoji，错误信息保持原文。'
    _, result, rows = learn(tmp_path, text,
        [proposal('以后回答不要使用 emoji', title='No emoji'),
         proposal('错误信息保持原文。', title='Verbatim errors')])
    assert result['proposals'] == 2 and result['promoted'] == 1
    assert len(rows) == 1 and rows[0]['content'] == text


@pytest.mark.parametrize('second', ['错误信息保持原', '错误信息保持原文。'])
def test_incomplete_or_mixed_quote_coverage_cannot_borrow_full_authority(tmp_path, second):
    text = '以后回答不要使用 emoji，错误信息保持原文。我们的服务采用 SQLite。'
    _, result, rows = learn(tmp_path, text,
        [proposal('以后回答不要使用 emoji'), proposal(second)])
    assert result['promoted'] == 0
    assert all(row['status'] == 'candidate' for row in rows)


def test_repeated_invalid_responses_exhaust_queue_bound(tmp_path, monkeypatch):
    model, calls = fake_model(monkeypatch, [model_response('not json')])
    engine = LearningEngine(database_path=tmp_path / 'exhaust.db', agent_id='codex', project_key='p',
        transcript_parser=lambda *a, **k: TranscriptTurn('Our service uses SQLite.', '', ()), reviewer=model)
    queue = LearningQueue(tmp_path / 'exhaust.db', engine.learn)
    transcript = tmp_path / 'source.jsonl'
    transcript.write_text('{}\n', encoding='utf-8')
    queue.submit({'agent_id': 'codex', 'project_key': 'p', 'session_id': 's', 'transcript_path': str(transcript)})
    for _ in range(3):
        with engine.store.knowledge._connect() as db:
            db.execute('UPDATE learning_jobs SET ready_at=0')
        assert queue.process_one()
    assert queue.summary() == {'failed': 1} and len(calls) == 3
    assert not queue.process_one()


def model_response(content, **extra):
    return {'choices': [{'message': {'content': content}, **extra}]}


def fake_model(monkeypatch, responses):
    model = DeepSeekReviewer.__new__(DeepSeekReviewer)
    model.api_key, model.base_url, model.model, model.timeout = 'fixture', 'http://invalid', 'fixture', 1
    calls = []
    def respond(request, **kwargs):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps(responses[min(len(calls) - 1, len(responses) - 1)]).encode())
    monkeypatch.setattr('agent_knowledge_bridge.provider.open_request', respond)
    return model, calls


@pytest.mark.parametrize('content', [None, '', ' ', '{"proposals":[', '{"other":{"proposals":[]}',
                                     '{"irrelevant":true}', '{"proposals":null}', 'PRIVATE_JUNK'])
def test_invalid_review_is_visible_failure_not_zero_extraction(monkeypatch, content):
    model, calls = fake_model(monkeypatch, [model_response(content)])
    with pytest.raises(RuntimeError) as exc:
        model('fixture input')
    assert 'PRIVATE_JUNK' not in str(exc.value)
    assert len(calls) == 1 and calls[0]['max_tokens'] <= 8192


def test_truncated_but_parseable_review_is_not_accepted(monkeypatch):
    model, _ = fake_model(monkeypatch, [model_response('{"proposals":[]}', finish_reason='length')])
    with pytest.raises(RuntimeError, match='incomplete'):
        model('fixture input')


def test_queue_recovers_after_empty_response_without_nested_retries(tmp_path, monkeypatch):
    text = 'Our index service uses SQLite FTS5.'
    raw = {'proposals': [proposal(text, kind='fact')]}
    model, calls = fake_model(monkeypatch, [model_response(None), model_response(json.dumps(raw))])
    engine = LearningEngine(database_path=tmp_path / 'queue.db', agent_id='codex', project_key='p',
                            transcript_parser=lambda *a, **k: TranscriptTurn(text, '', ()), reviewer=model)
    queue = LearningQueue(tmp_path / 'queue.db', engine.learn)
    transcript = tmp_path / 'turn.jsonl'
    transcript.write_text('{}\n', encoding='utf-8')
    queue.submit({'agent_id': 'codex', 'project_key': 'p', 'session_id': 's', 'transcript_path': str(transcript)})
    assert queue.process_one() and queue.summary() == {'queued': 1}
    with engine.store.knowledge._connect() as db:
        run = db.execute('SELECT status,error FROM learning_runs').fetchone()
        assert run['status'] == 'failed' and 'empty message content' in run['error']
        assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0] == 0
        db.execute('UPDATE learning_jobs SET ready_at=0')
    assert queue.process_one() and queue.summary() == {'completed': 1}
    assert len(calls) == 2
    with engine.store.knowledge._connect() as db:
        assert db.execute('SELECT attempts FROM learning_runs').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0] == 1


@pytest.mark.parametrize('outcomes', [(False,), (True, False), (False, None)])
@pytest.mark.parametrize('typed,category', [(True, 'uncertain'), (False, 'uncertain'), (False, 'temporary'),
                                         (True, 'durable'), (False, 'durable')])
def test_failed_observation_survives_without_saving_a_guessed_remedy(tmp_path, outcomes, typed, category):
    user = '检查 Windows widget 字段顺序，Linux 不适用。'
    assistant = 'Windows widget 字段顺序失败，没有确认恢复；不能当作已修复。'
    turn = TranscriptTurn(user, assistant, tuple(tool(status, i) for i, status in enumerate(outcomes)))
    def review(text):
        value = proposal(assistant, kind='procedure', source_role='assistant', retention=category,
                         title='Invented causal remedy', evidence_event_ids=re.findall(r'EVENT_ID: (ae_[a-f0-9]+)', text))
        if typed:
            value['experience'] = copy.deepcopy(CONTRACT)
            value['experience']['steps'] = ['Invented causal remedy.']
        return {'proposals': [value]}
    engine = LearningEngine(database_path=tmp_path / 'failure.db', agent_id='codex', project_key='p',
                            transcript_parser=lambda *a, **k: turn, reviewer=review)
    result = engine.learn({'session_id': 's'})
    assert result['proposals'] == 1 and result['promoted'] == 0
    with engine.store.knowledge._connect() as db:
        row = db.execute('SELECT * FROM knowledge_records').fetchone()
        assert row['status'] == 'candidate' and row['scope'] == 'project'
        assert 'Invented' not in row['title'] + row['content']
        contract = contract_from_content(row['content'])
        assert contract and ('未观察到恢复' in contract['reason'] or '未知' in contract['reason'])


@pytest.mark.parametrize('user', ['仅今天临时绕过校验，明天恢复。', '不要保存这次校验结果。'])
def test_failure_projection_cannot_bypass_temporary_or_optout(tmp_path, user):
    assistant = 'Windows widget 字段顺序失败，目前没有确认恢复。'
    def review(text):
        return {'proposals': [proposal(assistant, kind='procedure', source_role='assistant',
            retention='temporary', evidence_event_ids=re.findall(r'EVENT_ID: (ae_[a-f0-9]+)', text))]}
    engine = LearningEngine(database_path=tmp_path / 'negative.db', agent_id='codex', project_key='p',
        transcript_parser=lambda *a, **k: TranscriptTurn(user, assistant, (tool(False),)), reviewer=review)
    assert engine.learn({'session_id': 's'})['proposals'] == 0


def test_index_metadata_has_no_reasoning_notes_or_invented_subjects(tmp_path):
    text = '我们的账单服务使用 PostgreSQL 16。'
    _, result, rows = learn(tmp_path, text, [proposal(text, kind='fact',
        search_terms=['PostgreSQL', '中文别名', '掘地求升? no', 'PostgreSQL'],
        subject_terms=['账单服务', 'PostgreSQL 16', 'billing service'])])
    assert result['proposals'] == 1
    assert rows[0]['search_terms'] == 'PostgreSQL, 中文别名'
    assert json.loads(rows[0]['subject_terms']) == ['账单服务', 'postgresql 16']

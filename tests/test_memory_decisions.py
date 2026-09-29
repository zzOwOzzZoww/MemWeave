"""Decision boundaries, including counterexamples to the 15 diagnosis cases."""
import json
import pytest

from agent_knowledge_bridge.decisions import (
    query_intent, rejection_reason, grounded_user_proposal, obsolete, record_digest,
)
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent
from agent_knowledge_bridge.store import KnowledgeStore
from agent_knowledge_bridge.governance import Governor


@pytest.mark.parametrize('title,content,expected', [
    ('旧配置', '该旧值已被24小时替代。', True),
    ('当前配置', '新值24小时，原72小时已废止。', False),
    ('新策略', 'This policy has been superseded by the replacement.', True),
    ('Active retention policy', 'The old policy was revoked. Current value is 7 days.', False),
    ('旧配置迁移指南', '检查旧配置，然后迁移到新版。', False),
    ('废止规则处理', '如果规则已废止，需要检查替代关系。', False),
    ('审核决定', '不要认为此决定已废止，它依然有效。', False),
    ('审核决定', '如果此决定已废止，联系管理员。', False),
])
def test_obsolescence_requires_self_reference(title, content, expected):
    assert obsolete({'title': title, 'content': content}) == expected


@pytest.mark.parametrize('query,expected', [
    ('解释材料吸热，这与我的页面主题偏好无关。', 'explicitly_excluded'),
    ('不要引用我的页面主题偏好。', 'explicitly_excluded'),
    ('请解释“不要引用我的页面主题偏好”这句话，并告诉我默认主题。', None),
    ('页面主题偏好是什么？', None),
    ('并不是不要引用我的页面主题偏好，告诉我主题是什么。', None),
])
def test_negation_is_not_taken_from_quotes(query, expected):
    assert rejection_reason({'title':'页面主题长期偏好','content':'默认浅色'}, query_intent(query)) == expected


def test_history_remains_inspectable_without_becoming_current():
    row = {'title': '旧审核决定', 'content': '此决定已废止。'}
    assert rejection_reason(row, query_intent('当前审核期限？')) == 'explicitly_obsolete'
    assert rejection_reason(row, query_intent('查看历史决定，以前审核期限多少？')) is None


def test_explicit_topic_switch_changes_retrieval_input():
    query = '背景：候选24小时；现在只解释SQLite WAL。'
    assert query_intent(query).focus == '现在只解释SQLite WAL。'
    assert query_intent('解释“现在只解释SQLite WAL”这句话').focus.startswith('解释')


def test_excluding_one_project_topic_keeps_another_requested_topic():
    intent = query_intent('青岚审核期限是多少？不要引用青岚页面主题。')
    assert rejection_reason({'title':'青岚审核期限', 'content':'24小时'}, intent) is None
    assert rejection_reason({'title':'青岚页面主题', 'content':'浅色'}, intent) == 'explicitly_excluded'


@pytest.mark.parametrize('query,reject',[
    ('为什么浅色衣服吸热少？',True),
    ('简体中文和繁体中文的历史起源是什么？',True),
    ('为什么我的默认页面主题是浅色？',False),
    ('请根据我的偏好设计页面。',False),
    ('设计一个设置页面。',False),
])
def test_general_explanation_does_not_apply_preferences(query,reject):
    row={'title':'页面主题', 'content':'浅色','knowledge_type':'preference','scope':'user'}
    assert bool(rejection_reason(row,query_intent(query))) == reject


def test_redundant_context_can_use_alternate_switch_wording():
    assert query_intent('青岚期限已经知道是24小时，不用重复。请介绍SQLite WAL。').focus == '请介绍SQLite WAL。'


def test_explicit_memory_optout():
    row={'title':'页面主题','content':'浅色'}
    assert rejection_reason(row,query_intent('不要调用任何历史记忆，计算21除以3。')) == 'memory_opt_out'


@pytest.mark.parametrize('query,knowledge,expected', [
    ('另一个项目的审核期限是多少？',
     {'title': '青岚项目候选审核期限', 'content': '24小时', 'scope': 'project'},
     'explicitly_excluded_project_scope'),
    ('今天这个临时页面不用我的常用风格，先做深色版。',
     {'title': '页面主题长期偏好', 'content': '默认浅色', 'scope': 'user', 'knowledge_type': 'preference'},
     'explicitly_excluded_user_preference'),
    ('我说的不是青岚项目的审核规则，是一般产品设计原则。',
     {'title': '青岚项目候选审核期限', 'content': '24小时', 'scope': 'project'},
     'explicitly_excluded_project_scope'),
])
def test_natural_scope_exclusions_survive_candidate_expansion(query, knowledge, expected):
    assert rejection_reason(knowledge, query_intent(query, project_key='qinglan')) == expected


def test_sensitive_storage_is_rejected_by_admission_contract():
    user = '请记住今后默认把 API Key 写在配置文件里。'
    with pytest.raises(ValueError, match='Sensitive credentials'):
        grounded_user_proposal({'source_quotes': [user], 'knowledge_type': 'decision'},
                               TranscriptTurn(user, '知道了', ()))


def test_quoted_attribution_remains_reviewable_not_rejected():
    user = '别人说“请记住：今后默认浅色”，我只是引用。'
    result = grounded_user_proposal({'source_quotes': [user], 'knowledge_type': 'preference'},
                                    TranscriptTurn(user, '知道了', ()))
    assert result['auto_accept'] is False


@pytest.mark.parametrize('user,expected', [
    ('请记住：今后各个项目默认使用浅色主题。', True),
    ('请记住这个项目决策：重试期限正式定为24小时。', True),
    ('你觉得是否应该默认使用浅色主题？请记住。', False),
    ('举个示例：请记住今后各个项目默认浅色。', False),
    ('别人说“请记住：今后默认浅色”，我只是引用。', False),
    ('我比较喜欢浅色主题，这个还需要讨论。', False),
])
def test_narrow_user_acceptance(user, expected):
    result = grounded_user_proposal({'source_quotes':[user], 'knowledge_type':'preference'},
                                    TranscriptTurn(user, '知道了', ()))
    assert result['auto_accept'] == expected
    assert result['content'] == user


def test_invalid_quote_and_explicit_optout_fail_closed():
    for user, quotes in [('请记住我默认用浅色主题。',['默认使用深色主题。']),
                         ('今天用浅色，不要保存为偏好。',['今天用浅色，不要保存为偏好。'])]:
        with pytest.raises(ValueError):
            grounded_user_proposal({'source_quotes':quotes, 'knowledge_type':'preference'}, TranscriptTurn(user, '', ()))


def test_observed_assistant_quote_is_candidate_not_approval():
    output = '运行测试发现路径中含空格时命令需要引号。'
    turn = TranscriptTurn('帮我检查路径问题', output,
        (ToolEvent('tool', 'shell', 'check paths', 'error', False, 'test'),))
    result = grounded_user_proposal({'source_quotes':[output], 'source_role':'assistant', 'knowledge_type':'procedure'}, turn)
    assert result['scope'] == 'project' and not result['auto_accept']


def retired(tmp_path):
    store = KnowledgeStore(tmp_path/'test.db')
    key = store.publish(source_agent='codex',project_key='alpha', title='ledger-svc retry policy',
        content='Use exponential backoff for ledger-svc requests.', knowledge_type='procedure', scope='project',
        evidence_summary='fixture')['knowledge']['id']
    store.feedback(agent_id='human', knowledge_id=key, outcome='verified', evidence_kind='user_approval',
                   evidence_ref='fixture', evidence_summary='fixture')
    store.transit(key, to_status='archived', reason='test', actor='test')
    return store, key


def test_repeated_probe_is_not_independent_demand(tmp_path):
    store, key = retired(tmp_path)
    gov = Governor(store, policy={'lfhv_resurrect_threshold': 2})
    for _ in range(4):
        gov.shadow_probe(project_key='alpha', query='ledger-svc retry policy')
    assert gov.resurrect(project_key='alpha')['restored'] == 0
    with store._connect() as db:
        row = db.execute('SELECT * FROM shadow_hits').fetchone()
        assert row['hit_count'] == 1 and len(json.loads(row['query_hashes'])) == 1
    gov.shadow_probe(project_key='alpha', query='Explain ledger-svc retry policy')
    assert gov.resurrect(project_key='alpha')['restored'] == 1
    row = store.get(requester_agent='codex',knowledge_id=key)['knowledge']
    assert row['hit_count'] == 0


def test_changed_record_invalidates_restoration_evidence(tmp_path):
    store, key = retired(tmp_path)
    gov = Governor(store)
    gov.shadow_probe(project_key='alpha', query='ledger-svc retry policy')
    report = gov.lfhv_report(project_key='alpha')
    assert len(report['false_kills']) == 1
    old_hash = report['false_kills'][0]['content_hash']
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET title=? WHERE id=?',('changed title',key))
    assert gov.resurrect(project_key='alpha')['restored'] == 0
    assert not store.transit(key,to_status='active',reason='race',actor='test',
                            expected_status='archived', restoration_digest=old_hash)['changed']


def test_legacy_shadow_evidence_cannot_restore_under_new_policy(tmp_path):
    store, key = retired(tmp_path)
    gov = Governor(store)
    gov.shadow_probe(project_key='alpha',query='ledger-svc retry policy')
    with store._connect() as db:
        db.execute("UPDATE shadow_hits SET decision_version='' WHERE knowledge_id=?",(key,))
    assert gov.resurrect(project_key='alpha')['restored'] == 0


def test_historical_obsolete_match_never_becomes_restore_evidence(tmp_path):
    store, key = retired(tmp_path)
    with store._connect() as db:
        db.execute('UPDATE knowledge_records SET content=? WHERE id=?',
                   ('This policy has been superseded by a new policy.',key))
    gov = Governor(store)
    assert gov.shadow_probe(project_key='alpha',query='historical policy ledger-svc')['count'] == 0


def test_related_automatic_approvals_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = KnowledgeStore(tmp_path/'concurrent.db')
    keys = [store.publish(source_agent='codex',project_key='alpha',scope='project',
                         title='alpha 审核期限',content=f'请记住：今后审核期限正式定为{hours}小时。',
                         knowledge_type='decision',evidence_summary='source')['knowledge']['id'] for hours in (24,48)]
    def approve(key):
        return store.feedback(agent_id='codex',knowledge_id=key,outcome='verified',evidence_kind='user_approval',
                              evidence_ref='explicit-source',evidence_summary='fixture',require_no_related=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(approve,keys))
    assert sorted(r['knowledge']['status'] for r in results) == ['active','candidate']

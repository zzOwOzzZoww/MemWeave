from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent_knowledge_bridge import cli, provider, runtime_state
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.claude_transcript import TranscriptTurn, ToolEvent, redact_text, redact_value, parse_latest_turn
from agent_knowledge_bridge.learning_queue import LearningQueue
from agent_knowledge_bridge.service import KnowledgeBridgeService


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / '用户 数据'
    monkeypatch.setenv('MEMWEAVE_HOME', str(home))
    for name in ('MW_API_KEY','DEEPSEEK_API_KEY','OPENAI_API_KEY','MW_BASE_URL','OPENAI_BASE_URL',
                 'MW_MODEL','OPENAI_MODEL','MW_PROJECT_KEY','AKB_PROJECT_KEY','MW_DB_PATH','AKB_DB_PATH'):
        monkeypatch.delenv(name, raising=False)
    return home


def test_setup_preserves_database_and_encrypts_key(isolated, monkeypatch, capsys):
    key = 'TEST_ONLY_API_CREDENTIAL_123456789'
    monkeypatch.setenv('SETUP_TEST_KEY',key)
    assert cli.main(['setup','--non-interactive','--base-url','https://example.invalid/v1',
                     '--model','user-model','--api-key-env','SETUP_TEST_KEY','--no-shortcut']) == 0
    first = (isolated/'config.json').read_bytes()
    database = Path(json.loads(first)['database_path'])
    service = KnowledgeBridgeService(agent_id='test',project_key='default',database_path=database)
    service.publish(title='Setup preservation',content='Existing knowledge survives setup',knowledge_type='fact',evidence_summary='fixture')
    assert cli.main(['configure','--non-interactive','--model','second-model','--no-shortcut']) == 0
    assert (isolated/'config.json').read_bytes() == first
    assert service.review_queue()['count'] == 1
    assert provider.settings()['api_key'] == key
    assert provider.settings()['model'] == 'second-model'
    assert key not in capsys.readouterr().out
    if os.name == 'nt':
        assert key not in (isolated/'provider.json').read_text(encoding='utf-8')
        assert provider.public_settings()['storage'] == 'windows-dpapi'
    assert 'api_key' not in provider.public_settings()


def test_url_switch_needs_new_credential(isolated):
    provider.save(base_url='https://a.invalid/v1',model='demo',api_key='test-original')
    with pytest.raises(ValueError,match='重新输入'):
        provider.save(base_url='https://b.invalid/v1',model='demo')
    assert provider.settings()['base_url']=='https://a.invalid/v1'
    for value in ('http://remote.invalid/v1','https://user:key@example.com/v1','https://example.com/v1?key=secret'):
        with pytest.raises(ValueError):
            provider.normalize_url(value)


def test_workspace_identity_and_explicit_mapping(isolated, tmp_path):
    a,b=tmp_path/'alpha',tmp_path/'beta'
    (a/'.git').mkdir(parents=True);b.mkdir()
    (a/'src').mkdir()
    assert runtime_state.project_key(cwd=str(a)) == runtime_state.project_key(cwd=str(a/'src'))
    assert runtime_state.project_key(cwd=str(a)) != runtime_state.project_key(cwd=str(b))
    provider.atomic_json(isolated/'config.json',{'workspace_projects':{str(a):'existing-project'}})
    assert runtime_state.project_key(cwd=str(a/'src')) == 'existing-project'
    assert runtime_state.project_key(cwd=str(b)) != 'existing-project'


def _record(store):
    row=store.publish(source_agent='claude-code',project_key='p',title='widget policy',content='widget policy uses retries',
        knowledge_type='fact',scope='project',evidence_summary='fixture')['knowledge']
    store.feedback(agent_id='human',knowledge_id=row['id'],outcome='verified',evidence_kind='user_approval',evidence_ref='review',evidence_summary='reviewed')
    return row['id']


def test_archive_cannot_leak_through_retry_or_usage(isolated):
    adapter=ClaudeLearningAdapter(database_path=isolated/'test.db',agent_id='codex',project_key='p',reviewer=lambda _: {})
    store=adapter.store.knowledge
    kid=_record(store)
    event={'session_id':'s','turn_id':'t','prompt':'widget policy'}
    assert adapter.recall(event)['hookSpecificOutput']['additionalContext']
    store.transit(kid,to_status='archived',reason='retired',actor='human')
    assert adapter.recall(event)=={}
    store.feedback(agent_id='codex',knowledge_id=kid,outcome='used',evidence_summary='historical usage')
    assert store.get(requester_agent='human',knowledge_id=kid)['knowledge']['status']=='archived'


def test_disabled_agent_blocks_fallback_and_pending_learning(isolated):
    seen=[]
    adapter=ClaudeLearningAdapter(database_path=isolated/'test.db',agent_id='codex',project_key='p',reviewer=lambda x:seen.append(x))
    store=adapter.store.knowledge
    _record(store)
    store.register_agent(agent_id='codex',display_name='Codex',adapter_type='codex-hook')
    event={'session_id':'s','turn_id':'t','prompt':'widget policy'}
    assert adapter.recall(event)
    store.disable_agent('codex')
    assert adapter.recall(event)=={}
    assert adapter.learn({'transcript_path':'not-read'})=={'status':'disabled'}
    assert not seen
    service=KnowledgeBridgeService(database_path=isolated/'test.db',agent_id='codex',project_key='p')
    with pytest.raises(ValueError,match='disabled'):
        service.search('widget')


def test_unrelated_command_cannot_approve_model_inference(isolated):
    import re
    turn=TranscriptTurn('Test arithmetic','ok',(ToolEvent('x','Bash','python -m pytest arithmetic','PASS',True,'test'),))
    def reviewer(text):
        return {'proposals':[{'title':'Production deletion policy','content':'Delete production data after one hour.',
            'knowledge_type':'fact','scope':'user','evidence_event_ids':re.findall(r'EVENT_ID: (ae_\w+)',text)}]}
    adapter=ClaudeLearningAdapter(database_path=isolated/'test.db',agent_id='codex',project_key='p',
        reviewer=reviewer,transcript_parser=lambda *a,**k:turn)
    result=adapter.learn({'session_id':'s'})
    assert result['proposals']==1 and result['promoted']==0
    assert adapter.store.knowledge.search(requester_agent='codex',project_key='p',query='Production deletion',limit=5)['count']==0


def test_failed_learning_can_retry_and_finished_learning_deduplicates(isolated):
    attempts=[]
    def reviewer(_):
        attempts.append(1)
        if len(attempts)==1: raise RuntimeError('temporary failure')
        return {'proposals':[]}
    adapter=ClaudeLearningAdapter(database_path=isolated/'test.db',agent_id='codex',project_key='p',reviewer=reviewer,
        transcript_parser=lambda *a,**k:TranscriptTurn('Remember the project policy','Acknowledged',()))
    with pytest.raises(RuntimeError): adapter.learn({'session_id':'s'})
    assert adapter.learn({'session_id':'s'})['status']=='completed'
    assert adapter.learn({'session_id':'s'})['status']=='duplicate_turn'
    assert len(attempts)==2


def test_json_and_nested_credentials_are_redacted():
    secret='TEST_ONLY_NOT_A_REAL_SECRET_123456'
    for text in (json.dumps({'api_key':secret}), "password='"+secret+"'", 'api_key='+secret):
        assert secret not in redact_text(text)
    assert secret not in json.dumps(redact_value({'nested':[{'access_token':secret}]}))


def test_queue_persists_turn_boundary_and_retries(isolated):
    isolated.mkdir()
    transcript=isolated/'turn.jsonl'
    transcript.write_text(json.dumps({'type':'user','message':{'role':'user','content':'first turn'}})+'\n',encoding='utf-8')
    seen=[]
    def handler(payload):
        seen.append(parse_latest_turn(payload['transcript_path'],end_offset=payload['transcript_end']).user_text)
        if len(seen)==1: raise RuntimeError('network unavailable')
        return {'status':'completed'}
    queue=LearningQueue(isolated/'test.db',handler)
    job={'agent_id':'codex','project_key':'p','session_id':'s','turn_id':'one','transcript_path':str(transcript)}
    assert queue.submit(job)['status']=='queued'
    assert not seen
    with transcript.open('a',encoding='utf-8') as f:
        f.write(json.dumps({'type':'user','message':{'role':'user','content':'second turn'}})+'\n')
    assert queue.submit(job)['duplicate']
    assert queue.process_one()
    assert queue.summary()=={'queued':1}
    with queue.store.knowledge._connect() as db: db.execute('UPDATE learning_jobs SET ready_at=0')
    reopened=LearningQueue(isolated/'test.db',handler)
    assert reopened.process_one()
    assert seen==['first turn','first turn']
    assert reopened.summary()=={'completed':1}
    with reopened.store.knowledge._connect() as db:
        payload=db.execute('SELECT payload FROM learning_jobs').fetchone()[0]
    assert 'first turn' not in payload and 'second turn' not in payload


@pytest.mark.skipif(os.name!='nt',reason='Windows shortcut')
def test_shortcut_uses_install_interpreter_and_custom_home(isolated, tmp_path):
    from agent_knowledge_bridge.desktop import create_shortcut, _powershell, _ps
    shortcut=create_shortcut(tmp_path/'desktop')
    data=json.loads(_powershell('[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); '
        '$s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut('+_ps(str(shortcut))+'); '
        '@{target=$l.TargetPath;arguments=$l.Arguments;icon=$l.IconLocation} | ConvertTo-Json -Compress'))
    assert Path(data['target']) == Path(sys.executable).with_name('pythonw.exe')
    assert str(isolated) in data['arguments']
    assert 'token' not in data['arguments'].lower()
    assert Path(data['icon'].rsplit(',',1)[0]).is_file()


def test_provider_api_never_returns_key_or_validation_input(isolated):
    from fastapi.testclient import TestClient
    from agent_knowledge_bridge.daemon import create_app
    key='TEST_PRIVATE_VALUE_23456789'
    with TestClient(create_app(database_path=isolated/'test.db',api_token='local-test')) as client:
        assert client.get('/v1/settings/provider').status_code == 401
        headers={'Authorization':'Bearer local-test'}
        result=client.post('/v1/settings/provider',headers=headers,
            json={'base_url':'https://example.invalid/v1','model':'test-model','api_key':key})
        assert result.status_code==200
        assert key not in result.text
        assert key not in client.get('/v1/settings/provider',headers=headers).text
        malformed=client.post('/v1/settings/provider',headers=headers,json={'model':{'secret':key}})
        assert malformed.status_code==422
        assert key not in malformed.text


def test_provider_redirect_does_not_forward_credentials(isolated):
    import urllib.request
    request=urllib.request.Request('https://a.invalid',headers={'Authorization':'Bearer test'})
    assert provider._NoRedirect().redirect_request(request,None,302,'redirect',{},'https://b.invalid') is None


def test_api_settings_do_not_participate_in_recall(isolated):
    isolated.mkdir()
    (isolated/'provider.json').write_text('corrupt',encoding='utf-8')
    adapter=ClaudeLearningAdapter(database_path=isolated/'test.db',agent_id='codex',project_key='p')
    assert adapter.recall({'prompt':'unrelated'}) == {}


def test_runtime_launch_lock_serializes_concurrent_open(isolated):
    import threading
    from agent_knowledge_bridge.process_lock import startup_lock
    ready=threading.Event()
    release=threading.Event()
    def hold():
        with startup_lock():
            ready.set()
            release.wait(3)
    worker=threading.Thread(target=hold)
    worker.start()
    assert ready.wait(2)
    try:
        with pytest.raises(RuntimeError,match='正在启动'):
            with startup_lock(timeout=.15): pass
    finally:
        release.set();worker.join(3)
    with startup_lock(timeout=.15): pass

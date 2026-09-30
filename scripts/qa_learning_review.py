"""Isolated UI checks for batch review and cross-process live synchronization."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_knowledge_bridge.learning import LearningStore


def make_run(store, name, agent='codex'):
    return store.begin_run(agent_id=agent, project_key='ui-batch', session_id=name,
                          turn_hash=name, input_chars=100)


def make_record(store, title, content=None, agent='codex', project='ui-batch', kind='fact'):
    return store.knowledge.publish(source_agent=agent, project_key=project, scope='project',
        title=title, content=content or f'Synthetic browser fixture: {title}.', knowledge_type=kind,
        evidence_summary='Synthetic fixture evidence, not private conversations.')['knowledge']['id']


def approve(store, key):
    store.knowledge.feedback(agent_id='human-review', knowledge_id=key, project_key='ui-batch',
        outcome='verified', evidence_kind='user_approval', evidence_ref='qa://approval', evidence_summary='QA approval')


def fixture(database):
    store = LearningStore(database)
    for agent, name in [('codex','Codex'), ('claude-code','Claude Code'), ('page-agent','分页测试'), ('empty-agent','空批次'), ('workbuddy','WorkBuddy'), ('disabled-agent','已停用测试')]:
        store.knowledge.register_agent(agent_id=agent, display_name=name, adapter_type='runtime-api', installed=True)
    store.knowledge.disable_agent('disabled-agent')
    make_record(store, '当前工作区独立知识', project='ui-home')
    old = make_run(store, 'older')
    old_key = make_record(store, '上一轮候选')
    store.link_compilation(old, old_key, 'produced')
    store.finish_run(old, status='completed', proposal_count=1)
    foreign = make_run(store, 'foreign', agent='claude-code')
    foreign_key = make_record(store, '另一个 Agent 的候选', agent='claude-code')
    store.link_compilation(foreign, foreign_key, 'produced')
    store.finish_run(foreign, status='completed', proposal_count=1)
    old_version = make_record(store, '已采纳期限', content='审核期限正式定为24小时。', kind='decision')
    approve(store, old_version)
    batch = make_run(store, 'current')
    keys = {}
    for name, title in [('a','编译核对流程'), ('b','日志脱敏约定'), ('c','其他窗口审核'), ('d','并发审核保护'),
                        ('e','长内容与转义'), ('f','冲突审核设置'), ('g','批量正常候选'), ('active','本轮自动晋升')]:
        content = '审核期限正式定为48小时。' if name == 'f' else '<img src=x onerror="window.qaInjected=true">\n' + ('long-path/' * 70) if name == 'e' else None
        key = make_record(store, title, content=content, kind='decision' if name == 'f' else 'fact')
        keys[name] = key
        store.link_compilation(batch, key, 'produced')
        if name == 'active': approve(store, key)
    store.finish_run(batch, status='completed', proposal_count=8, promoted_count=1)
    paging = make_run(store, 'paging', agent='page-agent')
    for index in range(23):
        key = make_record(store, f'分页候选 {index + 1:02}', agent='page-agent')
        store.link_compilation(paging, key, 'produced')
    store.finish_run(paging, status='completed', proposal_count=23)
    empty = make_run(store, 'empty', agent='empty-agent')
    store.finish_run(empty, status='completed', proposal_count=0)
    return {'keys':keys, 'batch':batch}


def mutate(database):
    store = LearningStore(database)
    batch = make_run(store, 'hook-new')
    key = make_record(store, '后台新增学习候选')
    store.link_compilation(batch, key, 'produced')
    store.finish_run(batch, status='completed', proposal_count=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--mutate', type=Path)
    args = parser.parse_args()
    if args.mutate:
        mutate(args.mutate)
        return
    if not args.output:
        parser.error('--output is required')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='memweave-learning-ui-') as folder:
        temp = Path(folder)
        database = temp / 'qa.db'
        data = fixture(database)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(24)
        url = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'PYTHONUTF8':'1', 'PYTHONPATH':str(ROOT/'src'), 'MW_DB_PATH':str(database),
            'MEMWEAVE_HOME':str(temp/'home'), 'MW_DAEMON_TOKEN':token, 'MW_DAEMON_URL':url,
            'MW_QA_URL':url, 'MW_QA_TOKEN':token, 'MW_QA_OUTPUT':str(out),
            'MW_QA_DATABASE':str(database), 'MW_QA_PYTHON':sys.executable, 'MW_QA_FIXTURE':json.dumps(data)}
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with (out/'runtime.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, '-m', 'agent_knowledge_bridge.daemon', '--port', str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
            try:
                for _ in range(100):
                    try:
                        request = urllib.request.Request(url+'/v1/health', headers={'Authorization':'Bearer '+token})
                        with urllib.request.urlopen(request, timeout=1) as response:
                            if json.load(response)['status'] == 'ok': break
                    except OSError: time.sleep(.1)
                else: raise RuntimeError('QA runtime health timeout')
                result = subprocess.run(['node', str(ROOT/'scripts/qa_learning_review.cjs')], cwd=ROOT,
                    env=env, capture_output=True, text=True, encoding='utf-8', timeout=150, creationflags=flags)
                (out/'browser.log').write_text(result.stdout+result.stderr, encoding='utf-8')
                print(result.stdout+result.stderr)
                result.check_returncode()
                store = LearningStore(database)
                batch = store.run_records(requester_agent='human-review', project_key='ui-batch', source_agent='codex', run_id=data['batch'])
                assert batch['pending_count'] == 0 and batch['run']['proposal_count'] == 8
                assert store.knowledge.get(requester_agent='human-review', knowledge_id=data['keys']['d'])['knowledge']['rejected_count'] == 0
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    main()

"""Exercise destructive UI actions only against a disposable runtime/database."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent_knowledge_bridge.store import KnowledgeStore


def main():
    out=ROOT/'outputs'/'controlled-knowledge-20260924'
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='memweave-controlled-ui-') as directory:
        temp=Path(directory); database=temp/'qa.db'; store=KnowledgeStore(database)
        for i in range(27):
            result=store.publish(source_agent='claude-code' if i%2 else 'codex',project_key='ui-test',
                title=f'测试知识 {i+1:02d} · 项目校验与知识治理',content=f'验证用例 {i}，仅用于隔离测试。',
                knowledge_type='procedure',evidence_summary='synthetic QA fixture',scope='project')
            if i<25:
                store.feedback(agent_id='human-review',knowledge_id=result['knowledge']['id'],
                    outcome='verified',evidence_summary='QA approval',evidence_kind='user_approval',evidence_ref='qa://review')
        common={'model':'qa-model','model_config_hash':'a'*64,'prompt_hash':'b'*64,'base_context_hash':'c'*64,
            'environment_hash':'d'*64,'timing_source':'codex.task_complete','cache_policy':'qa-only',
            'turn_id':'t','response_total_ms':9000}
        pair={'project_key':'ui-test','experiment_id':'synthetic-ui-only','pair_id':'one','agent_id':'codex',
            'baseline_mode':'framework_absent','execution_order':'off_on','conditions_confirmed':True,
            'on':{**common,'session_id':'on','ttft_ms':1500},'off':{**common,'session_id':'off','ttft_ms':1000}}
        (temp/'pair.json').write_text(json.dumps(pair),encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        token=secrets.token_hex(24); url=f'http://127.0.0.1:{port}'
        env={**os.environ,'PYTHONUTF8':'1','PYTHONPATH':str(ROOT/'src'),'MW_DB_PATH':str(database),
            'MEMWEAVE_HOME':str(temp/'home'),'MW_DAEMON_TOKEN':token,'MW_DAEMON_URL':url,'MW_PROJECT_KEY':'ui-test',
            'MW_QA_URL':url,'MW_QA_TOKEN':token,'MW_QA_OUTPUT':str(out),'MW_QA_PAIR':str(temp/'pair.json')}
        if os.environ.get('NODE_PATH'):
            env['NODE_PATH']=os.environ['NODE_PATH']
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        with (out/'isolated-runtime.log').open('wb') as log:
            process=subprocess.Popen([sys.executable,'-m','agent_knowledge_bridge.daemon','--port',str(port)],
                cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=flags)
            try:
                for _ in range(100):
                    try:
                        request=urllib.request.Request(url+'/v1/health',headers={'Authorization':'Bearer '+token})
                        with urllib.request.urlopen(request,timeout=1) as response:
                            if json.load(response).get('status')=='ok':break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('QA runtime health timeout')
                result=subprocess.run(['node',str(ROOT/'scripts'/'qa_controlled_knowledge.cjs')],
                    env=env,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=150,creationflags=flags)
                print(result.stdout);print(result.stderr)
                if result.returncode:raise RuntimeError('browser checks failed')
                with closing(sqlite3.connect(database)) as db:
                    assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0]==22
                    assert not db.execute('PRAGMA foreign_key_check').fetchall()
            finally:
                process.terminate();process.wait(timeout=10)


if __name__=='__main__':main()

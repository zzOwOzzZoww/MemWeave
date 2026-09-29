"""Isolated browser review of conflicts; never operates on the personal store."""
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent_knowledge_bridge.store import KnowledgeStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); out=args.output.resolve(); out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='memweave-version-ui-') as folder:
        temp=Path(folder); database=temp/'qa.db'; store=KnowledgeStore(database)
        ids=[]
        for title,hours in [('审核期限旧版',24),('审核期限新版',48)]:
            key=store.publish(source_agent='claude-code',project_key='ui-test',scope='project',
                title=title,content=f'审核期限正式定为{hours}小时。',knowledge_type='decision',
                evidence_summary='Synthetic browser fixture')['knowledge']['id']
            ids.append(key)
        store.feedback(agent_id='human-review',knowledge_id=ids[0],outcome='verified',
            evidence_kind='user_approval',evidence_ref='qa://approved',evidence_summary='QA approval')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        token=secrets.token_hex(24); url=f'http://127.0.0.1:{port}'
        env={**os.environ,'PYTHONUTF8':'1','PYTHONPATH':str(ROOT/'src'),'MW_DB_PATH':str(database),
            'MEMWEAVE_HOME':str(temp/'home'),'MW_DAEMON_TOKEN':token,'MW_DAEMON_URL':url,
            'MW_PROJECT_KEY':'ui-test','MW_QA_URL':url,'MW_QA_TOKEN':token,'MW_QA_OUTPUT':str(out)}
        if os.environ.get('NODE_PATH'):
            env['NODE_PATH']=os.environ['NODE_PATH']
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        with (out/'runtime.log').open('wb') as log:
            process=subprocess.Popen([sys.executable,'-m','agent_knowledge_bridge.daemon','--port',str(port)],
                cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=flags)
            try:
                for _ in range(100):
                    try:
                        request=urllib.request.Request(url+'/v1/health',headers={'Authorization':'Bearer '+token})
                        with urllib.request.urlopen(request,timeout=1) as response:
                            if json.load(response).get('status')=='ok':break
                    except OSError:time.sleep(.1)
                else: raise RuntimeError('QA health timeout')
                result=subprocess.run(['node',str(ROOT/'scripts/qa_knowledge_versions.cjs')],env=env,
                    cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=120,creationflags=flags)
                (out/'browser.log').write_text(result.stdout+result.stderr,encoding='utf-8')
                print(result.stdout+result.stderr)
                result.check_returncode()
                old=store.get(requester_agent='codex',knowledge_id=ids[0])['knowledge']
                assert old['status']=='archived' and old['superseded_by']==ids[1]
            finally:
                process.terminate();process.wait(timeout=10)


if __name__=='__main__':main()

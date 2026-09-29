"""Isolated wheel installation acceptance. No real credentials or Agent homes."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import venv
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs' / 'productization-20260924'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    report = {}
    with tempfile.TemporaryDirectory(prefix='memweave-install-') as directory:
        root=Path(directory)
        stage=root/'source'
        stage.mkdir()
        for name in ('pyproject.toml','README.md','MANIFEST.in'):
            shutil.copy2(ROOT/name,stage/name)
        shutil.copytree(ROOT/'src',stage/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc','*.bak','*.egg-info'))
        environment={k:v for k,v in os.environ.items() if not k.startswith(('MW_','AKB_','MEMWEAVE','OPENAI_','DEEPSEEK_'))}
        environment.pop('PYTHONPATH',None)
        environment.update({'PYTHONUTF8':'1','PYTHONNOUSERSITE':'1','MEMWEAVE_HOME':str(root/'用户 数据'),
                            'CODEX_HOME':str(root/'codex'),'TEST_PROVIDER_KEY':'SYNTHETIC_INSTALL_KEY_123456'})
        def run(argv, **kwargs):
            result=subprocess.run(argv,cwd=root,env=environment,text=True,encoding='utf-8',capture_output=True,**kwargs)
            if result.returncode:
                raise RuntimeError(result.stderr[-2000:] or result.stdout[-2000:])
            return result.stdout
        wheel_dir=OUT/'wheel'
        wheel_dir.mkdir(exist_ok=True)
        run([sys.executable,'-m','pip','wheel','--no-deps',str(stage),'-w',str(wheel_dir)],timeout=120)
        wheel=wheel_dir/'memweave_runtime-0.5.0a1-py3-none-any.whl'
        with zipfile.ZipFile(wheel) as archive:
            names=archive.namelist()
            assert any(n.endswith('hooks/claude_learning_hook.py') for n in names)
            assert any(n.endswith('hooks/codex_learning_hook.py') for n in names)
            assert any(n.endswith('assets/memweave-icon.ico') for n in names)
            assert not any(n.endswith(('.db','.bak')) or '/outputs/' in n or '/data/' in n for n in names)
            user_profile_path = re.compile(rb"[A-Z]:\\\\Users\\\\[^\\\\]+", re.IGNORECASE)
            assert not any(user_profile_path.search(archive.read(n)) for n in names if n.endswith(('.py','.html','METADATA')))
        report['wheel']={'filename':wheel.name,'bytes':wheel.stat().st_size,'hooks_and_icon':True,'private_files':False}
        venv.EnvBuilder(with_pip=True).create(root/'venv with space')
        python=root/'venv with space'/'Scripts'/'python.exe' if os.name=='nt' else root/'venv with space'/'bin'/'python'
        run([str(python),'-m','pip','install',str(wheel)],timeout=180)
        entry=python.parent/('memweave.exe' if os.name=='nt' else 'memweave')
        assert run([str(entry),'--version'],timeout=15).strip()=='0.5.0a1'
        report['clean_install']=True

        calls=[]
        class FakeModel(BaseHTTPRequestHandler):
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append({'path':self.path,'model':data['model']})
                body=json.dumps({'choices':[{'message':{'content':'{"proposals":[]}'}}]}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),FakeModel)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}/v1'
        state=None
        try:
            args=[str(python),'-m','agent_knowledge_bridge.cli']
            setup=run(args+['setup','--non-interactive','--base-url',base,'--model','fixture-model',
                '--api-key-env','TEST_PROVIDER_KEY','--desktop',str(root/'desktop'),'--check'],timeout=40)
            assert environment['TEST_PROVIDER_KEY'] not in setup
            report['setup']=True
            report['desktop_shortcut']=(root/'desktop'/'MemWeave知识管理.lnk').is_file() if os.name=='nt' else None
            run(args+['ui','--no-open'],timeout=30)
            home=Path(environment['MEMWEAVE_HOME'])
            state=json.loads((home/'runtime-state.json').read_text(encoding='utf-8'))
            def request(path, data=None):
                req=urllib.request.Request(state['url']+path,
                    data=None if data is None else json.dumps(data).encode(),
                    headers={'Authorization':'Bearer '+state['token'],'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=10) as response:
                    return response.read()
            health=json.loads(request('/v1/health'))
            assert health['pid']==state['pid']
            report['runtime_started']=True
            html=request('/knowledge').decode()
            assert 'providerDialog' in html and 'knowledgeModule' in html
            report['dashboard_and_provider_settings']=True
            if os.getenv('MEMWEAVE_TEST_NODE'):
                ui_environment = dict(environment)
                for name in ('NODE_PATH','MEMWEAVE_TEST_CHROME'):
                    if os.getenv(name):
                        ui_environment[name]=os.environ[name]
                ui_environment['MEMWEAVE_TEST_OUTPUT']=str(OUT)
                checked=subprocess.run([os.environ['MEMWEAVE_TEST_NODE'],str(ROOT/'scripts/qa_product_setup.cjs')],
                    cwd=root,env=ui_environment,text=True,encoding='utf-8',capture_output=True,timeout=50)
                if checked.returncode:
                    raise RuntimeError(checked.stderr[-1500:])
                report['browser_settings']=json.loads(checked.stdout)
                request('/v1/settings/provider',{'base_url':base,'model':'fixture-model','api_key':None})
            settings=json.loads(request('/v1/settings/provider'))
            assert settings['model']=='fixture-model' and 'api_key' not in settings
            # Install hooks only to temporary paths, with both homes patched.
            helper=root/'verify_hook.py'
            helper.write_text('''import json, os, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from agent_knowledge_bridge import agent_registry
from agent_knowledge_bridge.paths import memweave_home
from agent_knowledge_bridge.learning import LearningStore
home=memweave_home()
store=LearningStore(home/'data/knowledge.db')
results=[]
for agent in ('codex','claude-code'):
    store.knowledge.register_agent(agent_id=agent,display_name=agent,adapter_type='runtime-api')
    config=home/(agent+'-hooks.json')
    with patch.object(agent_registry,'_expand',side_effect=lambda s:home/s[2:]):
        agent_registry.install_native_hook(agent,config_path=config)
    hooks=json.loads(config.read_text(encoding='utf-8'))['hooks']
    command=hooks['UserPromptSubmit'][0]['hooks'][0]['command']
    result=subprocess.run(command,input=json.dumps({'hook_event_name':'UserPromptSubmit','session_id':'install-test',
        'turn_id':agent+'-turn','cwd':str(home),'prompt':'unrelated smoke query'}),text=True,encoding='utf-8',
        capture_output=True,timeout=15)
    assert result.returncode==0, result.stderr
    assert json.loads(result.stdout)=={}
    with store.knowledge._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recall_events WHERE agent_id=?',(agent,)).fetchone()[0]==1
    results.append(agent)
print(json.dumps(results))
''',encoding='utf-8')
            report['native_hooks_installed_and_executed']=json.loads(run([str(python),str(helper)],timeout=35))
            transcript=root/'fixture.jsonl'
            transcript.write_text(json.dumps({'type':'user','message':{'role':'user','content':'Remember fixture project policy'}})+'\n',encoding='utf-8')
            result=json.loads(request('/v1/learning/queue',{'agent_id':'claude-code','project_key':'default',
                'session_id':'install-learning','turn_id':'fixture-turn','cwd':str(root),'transcript_path':str(transcript)}))
            assert result['status']=='queued'
            for _ in range(60):
                counts=json.loads(request('/v1/learning/queue'))['counts']
                if counts.get('completed'):break
                time.sleep(.2)
            assert counts.get('completed')==1,counts
            report['background_learning']=True
            report['mock_api_calls']=len(calls)
            report['paid_api_calls']=0
            report['installed_import_path']=run([str(python),'-c','import agent_knowledge_bridge; print(agent_knowledge_bridge.__file__)']).strip().replace(str(root),'<isolated>')
            assert 'site-packages' in report['installed_import_path']
            run(args+['ui','--no-open'],timeout=30)
            assert json.loads((home/'runtime-state.json').read_text(encoding='utf-8'))['pid']==state['pid']
            report['reopen_reuses_runtime']=True
        finally:
            if state:
                # Process identity was checked through its authenticated health endpoint.
                import signal
                try: os.kill(state['pid'],signal.SIGTERM)
                except OSError: pass
                time.sleep(.5)
            server.shutdown();server.server_close()
    (OUT/'install-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

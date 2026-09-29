"""Time the real local HTTP and hook subprocess paths on private DB copies."""
import argparse
import contextlib
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

from benchmark_recall_frequency import ROOT, clone, timing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stages-compare', action='store_true',
                        help='Compare the frozen pre-stage pipeline against the current pipeline')
    args = parser.parse_args()
    report = {'measurement': 'Separate local runtime processes and databases; hook time includes Python startup, health request, HTTP recall and audit log. No model calls.', 'variants': {}}
    report['comparison'] = 'retrieval-stages' if args.stages_compare else 'frequency-counts'
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='memweave-hook-perf-') as temp:
        root = Path(temp)
        snapshot = root / 'snapshot.db'
        clone(args.live_db, snapshot)
        with contextlib.closing(sqlite3.connect(snapshot)) as db:
            queries = [r[0][:500] for r in db.execute(
                "SELECT title FROM knowledge_records WHERE status='active' "
                "AND (scope='user' OR project_key='claude-codex-mvp') ORDER BY id LIMIT 10")]
        signatures = {}
        for variant in ('before', 'after'):
            database = root / f'{variant}.db'
            clone(snapshot, database)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = secrets.token_hex(24)
            url = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'PYTHONUTF8':'1', 'MW_DB_PATH': str(database),
                   'MW_DAEMON_URL': url, 'MW_DAEMON_TOKEN':token, 'MW_PROJECT_KEY':'claude-codex-mvp',
                   'MEMWEAVE_HOME':str(root / variant), 'MW_RUNTIME_MODE':'runtime',
                   'PYTHONPATH': os.pathsep.join(str(ROOT / p) for p in ('scripts', 'src', 'tests'))}
            boot = ("from benchmark_recall_frequency import legacy_counts; "
                    "import agent_knowledge_bridge.store as s; "
                    + ("s.bounded_document_frequencies=legacy_counts; " if variant == 'before' else '')
                    + "from agent_knowledge_bridge.daemon import main; main()")
            if args.stages_compare:
                boot = (("from fixtures.retrieval_before_stages import LegacyKnowledgeStore; "
                         "from fixtures.reuse_before_stages import LegacyReuseStore; "
                         "import agent_knowledge_bridge.store as s; "
                         "import agent_knowledge_bridge.reuse as r; "
                         "[setattr(s.KnowledgeStore,n,getattr(LegacyKnowledgeStore,n)) "
                         "for n in ('search','_pin_discriminative','_expand_siblings_flagged',"
                         "'_expand_anchored','_expand_vocabulary')]; "
                         "r.ReuseStore.start=LegacyReuseStore.start; "
                         if variant == 'before' else '')
                        + "from agent_knowledge_bridge.daemon import main; main()")
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(route, data=None):
                payload = None if data is None else json.dumps(data).encode('utf-8')
                req = urllib.request.Request(url + route, data=payload, headers={
                    'Authorization': 'Bearer ' + token, 'Content-Type':'application/json'})
                with opener.open(req, timeout=10) as response:
                    return json.load(response)
            with (root / f'{variant}.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-c', boot, '--port', str(port)],
                    cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
                try:
                    for _ in range(100):
                        if process.poll() is not None: raise RuntimeError('Isolated benchmark runtime exited')
                        try:
                            if request('/v1/health').get('status') == 'ok': break
                        except OSError: time.sleep(.1)
                    else: raise RuntimeError('Isolated runtime health timeout')
                    if args.stages_compare:
                        probe = request('/v1/knowledge/search', {'agent_id':'codex',
                            'project_key':'claude-codex-mvp', 'query':'benchmark-version-probe', 'limit':3})
                        if ('retrieval_diagnostics' in probe) != (variant == 'after'):
                            raise RuntimeError('Benchmark variant was not applied')
                    measures = {}
                    for agent, script in [('codex', 'codex_learning_hook.py'), ('claude-code', 'claude_learning_hook.py')]:
                        for transport in ('http', 'hook_process'):
                            elapsed, has_context = [], 0
                            for i, query in enumerate(queries):
                                turn = f'{agent}-{transport}-{i}'
                                payload = {'agent_id':agent, 'project_key':'claude-codex-mvp',
                                           'session_id':'isolated-perf', 'turn_id':turn, 'prompt':query,
                                           'hook_event_name':'UserPromptSubmit'}
                                start = time.perf_counter()
                                if transport == 'http':
                                    output = request('/v1/learning/recall', {k:v for k,v in payload.items() if k != 'hook_event_name'})
                                else:
                                    result = subprocess.run([sys.executable, str(ROOT / 'scripts' / script)],
                                        input=json.dumps(payload), text=True, encoding='utf-8', capture_output=True,
                                        env={**env,'MW_AGENT_ID':agent}, cwd=ROOT, timeout=20, creationflags=flags)
                                    if result.returncode: raise RuntimeError('Hook subprocess failed')
                                    output = json.loads(result.stdout)
                                elapsed.append((time.perf_counter() - start) * 1000)
                                has_context += bool((output.get('hookSpecificOutput') or {}).get('additionalContext'))
                            measures[f'{agent}_{transport}'] = {**timing(elapsed), 'with_context':has_context}
                    with contextlib.closing(sqlite3.connect(database)) as db:
                        traces = db.execute("SELECT agent_id,turn_id,items_json FROM reuse_traces WHERE session_id='isolated-perf' ORDER BY agent_id,turn_id").fetchall()
                    signatures[variant] = [(a,t,[(i['knowledge_id'],i['origin'],i['emitted']) for i in json.loads(items)]) for a,t,items in traces]
                    report['variants'][variant] = measures
                    print(variant + ' real HTTP and hook subprocess calls complete', flush=True)
                finally:
                    process.terminate()
                    process.wait(timeout=10)
        report['same_trace_results_and_emissions'] = signatures['before'] == signatures['after']
    (args.output / 'runtime-hook-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(not report['same_trace_results_and_emissions'])


if __name__ == '__main__':
    raise SystemExit(main())

"""Run frozen diagnostic cases on isolated databases, with optional live extraction.

No native client is launched and no production knowledge is read or written.
Live calls send only the synthetic dialogues and use the current production reviewer.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter, DeepSeekReviewer
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.governance import Governor
from agent_knowledge_bridge.store import KnowledgeStore
from agent_knowledge_bridge.reuse import percentile
from agent_knowledge_bridge import provider


def save(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def source_hash():
    paths=sorted(p for p in (ROOT/'src').rglob('*') if p.suffix in {'.py','.html'})
    return hashlib.sha256(''.join(str(p.relative_to(ROOT))+hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths).encode()).hexdigest()


def adapter(agent, database, reviewer=None, project='qinglan'):
    cls=CodexLearningAdapter if agent=='codex' else ClaudeLearningAdapter
    return cls(database_path=database,agent_id=agent,project_key=project,reviewer=reviewer)


def transcript(path, agent, case):
    if agent=='codex':
        rows=[{'type':'event_msg','payload':{'type':'user_message','message':case['user']}},
              {'type':'response_item','payload':{'type':'message','role':'assistant',
               'content':[{'type':'output_text','text':case['assistant']}]}}]
    else:
        rows=[{'type':'user','message':{'role':'user','content':case['user']}},
              {'type':'assistant','message':{'role':'assistant','content':case['assistant']}}]
    path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows),encoding='utf-8')


def records(store):
    with store._connect() as db:
        return [dict(row) for row in db.execute('SELECT title,content,knowledge_type,scope,status,search_terms FROM knowledge_records ORDER BY rowid')]


def admission(case, agent, repeat, out, live):
    folder=out/f"{case['id']}-{agent}-r{repeat}"
    folder.mkdir()
    path=folder/'synthetic-transcript.jsonl';transcript(path,agent,case)
    base={'case':case['id'],'layer':'admission','agent':agent,'repeat':repeat,'expected':case['expected']}
    if not live:return {**base,'execution':'not_run','reason':'Live reviewer explicitly disabled'}
    calls=[]
    reviewer=DeepSeekReviewer()
    def observed(text):
        start=time.perf_counter()
        try:
            result=reviewer(text)
            calls.append({'input':text,'output':result,'elapsed_ms':round((time.perf_counter()-start)*1000,3)})
            return result
        except Exception as exc:
            calls.append({'input':text,'error_type':type(exc).__name__,'elapsed_ms':round((time.perf_counter()-start)*1000,3)})
            raise
    a=adapter(agent,folder/'evaluation.db',observed)
    parsed=a.transcript_parser(path)
    assert parsed.user_text==case['user'] and parsed.assistant_text==case['assistant'] and not parsed.tools
    start=time.perf_counter()
    try:
        result=a.learn({'session_id':'synthetic-evaluation','turn_id':'learn-turn','transcript_path':str(path)})
        stored=records(a.store.knowledge)
        target=case['desired_action']
        decision=('auto_accept' if any(r['status']=='active' for r in stored) else 'candidate' if stored else 'skip')
        if target=='skip':
            verdict='pass' if not stored else 'mismatch'
        elif not stored:verdict='mismatch'
        elif any(r['scope']!=case['expected_scope'] for r in stored):verdict='mismatch'
        else:verdict='unsupported' if decision=='candidate' else 'needs_semantic_review'
        base.update(execution='completed',actual_action=decision,verdict=verdict,
                    records=stored,proposals=result['proposals'],promoted=result['promoted'],
                    note='Presence/scope/status checks only; proposal meaning still requires the report author to inspect.')
    except Exception as exc:
        base.update(execution='error',verdict='not_evaluable',error_type=type(exc).__name__)
    base['elapsed_ms']=round((time.perf_counter()-start)*1000,3)
    base['model_calls']=len(calls)
    save(folder/'reviewer-observations.json',calls)
    save(folder/'result.json',base)
    return base


THEME={'title':'页面设计长期偏好','content':'用户明确的长期偏好：设计页面默认采用浅色主题，适用于各个项目。',
       'scope':'user','knowledge_type':'preference','search_terms':'页面 设计 浅色 深色 主题 偏好 light theme page design preference'}
CURRENT={'title':'青岚项目当前候选审核期限','content':'2026年9月26日生效：青岚项目的知识候选审核期限正式定为创建后24小时。原72小时决定已废止。',
         'scope':'project','knowledge_type':'decision','search_terms':'青岚 项目 候选 审核 期限 24小时 review deadline current'}
OLD={'title':'青岚项目旧候选审核期限','content':'2026年9月20日的旧决定：青岚项目候选审核期限为72小时。该旧值已于9月26日废止，被24小时替代，不应作为当前配置。',
     'scope':'project','knowledge_type':'decision','search_terms':'青岚 项目 候选 审核 期限 72小时 review deadline old'}


def seed(store, spec, source, status='active', days_old=0):
    if days_old:
        stamp=(datetime.now(timezone.utc)-timedelta(days=days_old)).isoformat(timespec='seconds')
        original_clock=store.clock;store.clock=lambda:stamp
    try:
        row=store.publish(source_agent=source,project_key='qinglan',**spec,
            evidence_summary='Synthetic pre-approved fixture, not output of the live extraction stage')['knowledge']
        if status=='quarantined':
            store.feedback(agent_id='human-fixture',knowledge_id=row['id'],outcome='rejected',
                evidence_kind='user_approval',evidence_ref='synthetic://rejection',evidence_summary='Fixture is explicitly rejected')
        else:
            store.feedback(agent_id='human-fixture',knowledge_id=row['id'],outcome='verified',
                evidence_kind='user_approval',evidence_ref='synthetic://approval',evidence_summary='Pre-approved synthetic fact')
            if status=='archived':store.transit(row['id'],to_status='archived',reason='Synthetic capacity retirement',actor='fixture',expected_status='active')
        return row['id']
    finally:
        if days_old:store.clock=original_clock


def recall(a, query, turn, aliases, folder):
    start=time.perf_counter()
    output=a.recall({'session_id':'synthetic-recall','turn_id':turn,'prompt':query,'cwd':str(folder)})
    elapsed=(time.perf_counter()-start)*1000
    trace=a.reuse.existing(a.agent_id,a.project_key,'synthetic-recall',turn)
    if trace is None:raise RuntimeError('Missing isolated reuse trace')
    items=json.loads(trace['items_json'])
    return {'retrieved':[aliases[item['knowledge_id']] for item in items],
            'emitted':[aliases[item['knowledge_id']] for item in items if item['emitted']],
            'origin':[item['origin'] for item in items],
            'retrieval_ms':trace['retrieval_ms'],'adapter_ms':round(elapsed,3),
            'has_context':bool(output.get('hookSpecificOutput',{}).get('additionalContext'))}


def local_case(case, target, out):
    source='claude-code' if target=='codex' else 'codex'
    folder=out/f"{case['id']}-{target}"
    folder.mkdir()
    store=KnowledgeStore(folder/'evaluation.db')
    ids={}
    kind=case['fixture']
    if kind=='preference':ids['theme']=seed(store,THEME,source)
    elif kind in ('decision','other_project'):ids['current']=seed(store,CURRENT,source)
    elif kind=='conflict':
        ids['old']=seed(store,OLD,source);ids['current']=seed(store,CURRENT,source)
    elif kind=='valid_archive':ids['retired']=seed(store,CURRENT,source,'archived')
    elif kind=='irrelevant_archive':ids['retired']=seed(store,THEME,source,'archived')
    elif kind=='superseded_archive':
        ids['old']=seed(store,OLD,source,'archived');ids['current']=seed(store,CURRENT,source)
    elif kind=='rejected':ids['rejected']=seed(store,OLD,source,'quarantined')
    elif kind=='unobserved_archive':ids['retired']=seed(store,CURRENT,source,'archived',365)
    aliases={value:key for key,value in ids.items()}
    a=adapter(target,store.database_path,lambda _: {'proposals':[]},project='another-project' if kind=='other_project' else 'qinglan')
    result={'case':case['id'],'layer':case['layer'],'source_agent':source,'target_agent':target,'expected':case['expected'],'execution':'completed'}
    if case['layer']=='injection':
        observed=recall(a,case['query'],'query',aliases,folder)
        result.update(observed,verdict='pass' if set(observed['emitted'])==set(case['expected_aliases']) else 'mismatch')
    else:
        governor=Governor(store)
        if kind=='unobserved_archive':
            sweep=governor.sweep(project_key='qinglan')
            result.update(verdict='pass',capability='archive_retention_policy_not_supported',
                          note='No explicit deletion policy exists; preserving unobserved knowledge is not a correctness failure.',
                          sweep_counts={k:v for k,v in sweep.items() if isinstance(v,(int,float,bool))})
        else:
            if case.get('probe_repeats'):
                for _ in range(case['probe_repeats']):governor.shadow_probe(project_key='qinglan',query=case['query'])
            else:
                result['before_recovery']=recall(a,case['query'],'before',aliases,folder)
            prior=governor.lfhv_report(project_key='qinglan')
            result['shadow']={'probe_count':prior['shadow_probes_recorded'],
                'candidates':[{'alias':aliases[r['knowledge_id']],'hits':r['shadow_hits'],'best_rank':r['best_rank']} for r in prior['false_kills']]}
            restored=governor.resurrect(project_key='qinglan')
            actual=[aliases[r['knowledge_id']] for r in restored['records']]
            result.update(restored=actual,verdict='pass' if set(actual)==set(case['expected_restored']) else 'mismatch')
            result['after_recovery']=recall(a,case['query'],'after',aliases,folder)
            if kind=='valid_archive' and 'retired' not in result['after_recovery']['emitted']:result['verdict']='mismatch'
        with store._connect() as db:
            result['final_records']=[{'alias':aliases[r['id']],'status':r['status'],'hit_count':r['hit_count']}
                for r in db.execute('SELECT id,status,hit_count FROM knowledge_records ORDER BY rowid')]
    save(folder/'result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--admission-repeats',type=int,default=2,choices=(1,2))
    args=parser.parse_args()
    out=args.output.resolve()
    if out.exists() and any(out.iterdir()):raise RuntimeError('Use an empty output directory; preserved results must not be overwritten')
    out.mkdir(parents=True,exist_ok=True)
    fixture_path=ROOT/'evaluations/memory_decisions_15.json'
    fixture=fixture_path.read_bytes();dataset=json.loads(fixture)
    (out/'cases-frozen.json').write_bytes(fixture)
    config=provider.public_settings()
    if args.live and (not config['configured'] or config['model']!='deepseek-flash'):
        raise RuntimeError('Expected the configured deepseek-flash provider; no silent substitution')
    metadata={'started_at':datetime.now(timezone.utc).isoformat(),'source_sha256':source_hash(),
              'case_sha256':hashlib.sha256(fixture).hexdigest(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'python':sys.version,'sqlite':sqlite3.sqlite_version,'platform':platform.platform(),
              'effective_recall_limit':os.getenv('MW_RECALL_LIMIT','3'),'effective_lfhv_probe':os.getenv('MW_LFHV_PROBE','1'),
              'provider':config,'live':args.live,'admission_repeats':args.admission_repeats,
              'native_clients_launched':False,'production_database_accessed':False,
              'semantic_labels':'assistant_authored_before_run_not_independently_validated'}
    save(out/'manifest.json',metadata)
    results=[]
    for case in dataset['cases']:
        if case['layer']=='admission':continue
        for target in ('codex','claude-code'):
            result=local_case(case,target,out);results.append(result)
            print(json.dumps({'case':result['case'],'agent':target,'verdict':result['verdict']},ensure_ascii=False),flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[pool.submit(admission,case,agent,repeat,out,args.live)
              for case in dataset['cases'] if case['layer']=='admission'
              for agent in ('codex','claude-code') for repeat in range(1,args.admission_repeats+1)]
        for future in as_completed(jobs):
            result=future.result();results.append(result)
            print(json.dumps({key:result.get(key) for key in ('case','agent','repeat','execution','verdict')},ensure_ascii=False),flush=True)
    results.sort(key=lambda r:(r['case'],r.get('agent',r.get('target_agent')),r.get('repeat',0)))
    save(out/'results.json',results)
    metadata.update(finished_at=datetime.now(timezone.utc).isoformat(),source_unchanged=source_hash()==metadata['source_sha256'],
                    total_observations=len(results),model_calls=sum(r.get('model_calls',0) for r in results))
    save(out/'manifest.json',metadata)
    print(json.dumps({k:metadata[k] for k in ('total_observations','model_calls','source_unchanged')},ensure_ascii=False),flush=True)
    return int(any(r.get('execution')=='error' for r in results))


if __name__=='__main__':raise SystemExit(main())

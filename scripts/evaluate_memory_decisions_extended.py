"""Frozen synthetic decision matrix, isolated stores, optional real extraction.

120 scenario combinations are not 120 independent behavior types: the dataset
contains repeated intent templates across subjects. No end-to-end client claims.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

def build_cases():
    topics = [
        ('页面主题', '默认浅色', '界面配色', 'preference'),
        ('编辑器字体', '等宽字体', '代码显示字体', 'preference'),
        ('通知方式', '静默提醒', '消息提醒', 'preference'),
        ('回答语言', '简体中文', '回复用语', 'preference'),
        ('日期格式', '年-月-日', '时间日期展示', 'preference'),
        ('重试策略', '退避三次', '失败重试', 'decision'),
        ('审核期限', '24小时', '候选审批时限', 'decision'),
        ('日志保留', '七天', '日志保存时间', 'decision'),
        ('构建命令', 'python build.py', '打包构建步骤', 'decision'),
        ('缓存容量', '128MB', '缓存大小上限', 'decision'),
    ]
    cases=[]
    for i,(subject,value,alias,kind) in enumerate(topics):
        fixture={'title':f'青岚{subject}', 'content':f'青岚{subject}当前正式设置：{value}。',
                 'search_terms':f'{subject} {alias} {value}', 'knowledge_type':kind,
                 'scope':'user' if kind=='preference' else 'project'}
        scenarios=[
            ('related',f'青岚{subject}怎么设置？',['memory']),
            ('excluded',f'解释计算机内存，不要引用青岚{subject}。',[]),
            ('quoted_exclusion',f'有人说“不要引用青岚{subject}”，但我想知道青岚{subject}怎么设置。',['memory']),
            ('topic_switch',f'背景：青岚{subject}是{value}；现在只解释太阳辐射的物理原理。',[]),
            ('alias',f'我的{alias}应该如何配置？',['memory']),
            ('historical',f'查看历史决定：青岚{subject}以前是什么？',['memory']),
            ('obsolete',f'青岚{subject}当前设置是什么？',[]),
            ('other_project',f'另外项目的{subject}怎么设置？', ['memory'] if kind=='preference' else []),
        ]
        for name,query,expected in scenarios:
            spec=dict(fixture)
            if name in {'historical','obsolete'}:
                spec['title']='旧'+spec['title']
                spec['content'] += '此决定已废止，被新决定替代。'
            cases.append(dict(id=f'R{i+1:02}-{name}',layer='injection',pattern=name,
                              fixture=spec,query=query,expected=expected,
                              target_project='other-project' if name=='other_project' else 'qinglan'))
    for i,(subject,value,alias,kind) in enumerate(topics[5:9]):
        fixture={'title':f'青岚{subject}', 'content':f'青岚{subject}当前设置为{value}。',
                 'search_terms':f'{subject} {alias}', 'knowledge_type':kind,'scope':'project'}
        for pattern in ('valid','obsolete','rejected','duplicate','changed'):
            spec=dict(fixture)
            if pattern=='obsolete':
                spec.update(title='旧'+spec['title'],content=spec['content']+'此决定已废止。')
            cases.append(dict(id=f'G{i+1:02}-{pattern}',layer='archive',pattern=pattern,
                              fixture=spec,query=f'青岚{subject}是什么？',expected=['memory'] if pattern=='valid' else []))
    for i,(subject,value,alias,kind) in enumerate(topics):
        user=(f'请记住：今后各个项目的{subject}默认采用{value}。' if kind=='preference'
              else f'请记住这个项目决策：青岚项目的{subject}正式定为{value}。')
        cases.append(dict(id=f'A{i+1:02}',layer='admission',pattern='explicit',user=user,
                          assistant='明白，将遵循你明确的设置。',expected='active',
                          expected_scope='user' if kind=='preference' else 'project'))
    negatives=[
        ('只在今天演示里使用深色主题，不要记住。','本次演示用深色。'),
        ('这次报告用英文，不要记录成长期偏好。','仅本次用英文。'),
        ('今天临时设置缓存为64MB，不用保存。','这是临时安排。'),
        ('这次重试五次，以后仍按原配置，不要保存本次安排。','本次临时重试五次。'),
        ('只为本次调试保留日志，不要记录新的保留政策。','没有更改长期政策。'),
        ('要不要把候选自动批准？我还没有决定。','可以先讨论风险。'),
        ('你觉得默认深色好不好？只是问意见，不要记住。','这取决于需求，目前没有决定。'),
        ('是否把审核期改为48小时？尚未决定，不要保存。','目前不形成新决定。'),
        ('青岚服务为什么超时？','可能网络拥堵，但没有日志也没有证据，只是猜测。'),
        ('构建为什么失败？','可能是内存不足，但没有验证，没有证据。'),
    ]
    for i,(user,assistant) in enumerate(negatives):
        cases.append(dict(id=f'A{i+11:02}',layer='admission',pattern='skip',user=user,
                          assistant=assistant,expected='skip'))
    assert len(cases)==120
    return {'version':'decision-matrix-120-v1','label_author':'assistant; not independently adjudicated',
            'sampling':'80 injection template/subject combinations, 20 archive combinations, 20 dialogues; developmental synthetic set',
            'cases':cases}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--write-cases',type=Path)
    p.add_argument('--cases',type=Path,default=ROOT/'evaluations/memory_decisions_120.json')
    p.add_argument('--output',type=Path)
    p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--live',action='store_true')
    args=p.parse_args()
    if args.write_cases:
        if args.write_cases.exists(): raise ValueError('Refuse to overwrite frozen cases')
        save(args.write_cases,build_cases()); return
    if not args.output: p.error('--output required')
    out=args.output.resolve()
    if out.exists() and any(out.iterdir()): raise ValueError('Output must be empty')
    out.mkdir(parents=True,exist_ok=True)
    raw=args.cases.read_bytes(); dataset=json.loads(raw)
    (out/'cases-frozen.json').write_bytes(raw)
    sys.path.insert(0,str(args.source_root.resolve()/'src'))
    from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter, DeepSeekReviewer
    from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
    from agent_knowledge_bridge.governance import Governor
    from agent_knowledge_bridge.store import KnowledgeStore
    from agent_knowledge_bridge import provider
    source_files=sorted((args.source_root/'src').rglob('*.py'))
    manifest={'case_sha256':hashlib.sha256(raw).hexdigest(),
        'source_hashes':{str(f.relative_to(args.source_root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in source_files},
        'live':args.live,'provider':provider.public_settings() if args.live else None,
        'production_database_accessed':False,'native_clients_launched':False}
    save(out/'manifest.json',manifest)
    if args.live and (manifest['provider']['model']!='deepseek-flash' or not manifest['provider']['configured']):
        raise ValueError('Expected configured deepseek-flash')

    def run(case,target):
        folder=out/(case['id']+'-'+target); folder.mkdir()
        cls=CodexLearningAdapter if target=='codex' else ClaudeLearningAdapter
        source='claude-code' if target=='codex' else 'codex'
        a=cls(database_path=folder/'evaluation.db',agent_id=target,
              project_key=case.get('target_project','qinglan'),reviewer=lambda _: {'proposals':[]})
        store=a.store.knowledge
        result={'case':case['id'],'layer':case['layer'],'pattern':case['pattern'],'target':target,'expected':case['expected']}
        ids={}
        def recall(query,turn):
            start=time.perf_counter()
            a.recall({'session_id':'synthetic','turn_id':turn,'prompt':query,'cwd':str(folder)})
            elapsed=(time.perf_counter()-start)*1000
            trace=a.reuse.existing(target,a.project_key,'synthetic',turn)
            items=json.loads(trace['items_json'])
            return [ids[item['knowledge_id']] for item in items if item['emitted']],round(elapsed,3)
        if case['layer']=='admission':
            if not args.live:
                return {**result,'execution':'not_run'}
            calls=[]; reviewer=DeepSeekReviewer()
            def observed(text):
                start=time.perf_counter()
                response=reviewer(text)
                calls.append({'input':text,'output':response,'elapsed_ms':(time.perf_counter()-start)*1000})
                return response
            a.reviewer=observed
            if target=='codex':
                entries=[{'type':'event_msg','payload':{'type':'user_message','message':case['user']}},
                         {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':case['assistant']}]}}]
            else:
                entries=[{'type':'user','message':{'role':'user','content':case['user']}},
                         {'type':'assistant','message':{'role':'assistant','content':case['assistant']}}]
            transcript=folder/'synthetic-transcript.jsonl'
            transcript.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in entries),encoding='utf-8')
            learned=a.learn({'session_id':'synthetic','turn_id':'learn','transcript_path':str(transcript)})
            with store._connect() as db:
                records=[dict(r) for r in db.execute('SELECT title,content,scope,status FROM knowledge_records')]
            action='active' if any(r['status']=='active' for r in records) else 'candidate' if records else 'skip'
            passed=action==case['expected']
            if records and case.get('expected_scope'):
                passed=passed and all(r['scope']==case['expected_scope'] and r['content']==case['user'] for r in records)
            result.update(actual=action,records=records,passed=passed,model_calls=len(calls),execution=learned['status'])
            save(folder/'reviewer-observations.json',calls)
        else:
            key=store.publish(source_agent=source,project_key='qinglan',**case['fixture'],evidence_summary='Synthetic pre-approved fixture')['knowledge']['id']
            ids[key]='memory'
            store.feedback(agent_id='human',knowledge_id=key,outcome='verified',evidence_kind='user_approval',evidence_ref='synthetic-fixture',evidence_summary='fixture')
            if case['layer']=='injection':
                actual,elapsed=recall(case['query'],'query')
                result.update(actual=actual,adapter_ms=elapsed,passed=actual==case['expected'],execution='completed')
            else:
                state='quarantined' if case['pattern']=='rejected' else 'archived'
                store.transit(key,to_status=state,reason='fixture',actor='fixture')
                gov=Governor(store,policy={'lfhv_resurrect_threshold':2} if case['pattern']=='duplicate' else None)
                for _ in range(3 if case['pattern']=='duplicate' else 1):
                    gov.shadow_probe(project_key='qinglan',query=case['query'])
                if case['pattern']=='changed':
                    with store._connect() as db:
                        db.execute('UPDATE knowledge_records SET content=? WHERE id=?',('内容已重新编辑，需要重新验证。',key))
                restored=gov.resurrect(project_key='qinglan')
                actual=[ids[r['knowledge_id']] for r in restored['records']]
                result.update(actual=actual,passed=actual==case['expected'],execution='completed')
                with store._connect() as db:
                    result['state']=dict(db.execute('SELECT status,hit_count FROM knowledge_records').fetchone())
        save(folder/'result.json',result)
        return result

    results=[]
    for case in dataset['cases']:
        if case['layer']=='admission':continue
        for target in ('codex','claude-code'):
            results.append(run(case,target))
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[pool.submit(run,c,t) for c in dataset['cases'] if c['layer']=='admission' for t in ('codex','claude-code')]
        for job in as_completed(jobs):
            result=job.result(); results.append(result)
            print(json.dumps({k:result.get(k) for k in ('case','target','actual','passed','execution')},ensure_ascii=False),flush=True)
    results.sort(key=lambda r:(r['case'],r['target']))
    save(out/'results.json',results)
    manifest['source_unchanged'] = all(hashlib.sha256((args.source_root/name).read_bytes()).hexdigest()==value
                                       for name,value in manifest['source_hashes'].items())
    save(out/'manifest.json',manifest)
    summary={'scenario_combinations':len(dataset['cases']),'executed_observations':sum(r['execution']!='not_run' for r in results),
             'model_calls':sum(r.get('model_calls',0) for r in results),
             'passed':sum(r.get('passed',False) for r in results),
             'failed':[{k:r[k] for k in ('case','target','actual','expected')} for r in results if r.get('passed') is False],
             'by_layer':{layer:dict(Counter('not_run' if r['execution']=='not_run' else 'pass' if r['passed'] else 'fail'
                                            for r in results if r['layer']==layer)) for layer in ('injection','archive','admission')}}
    save(out/'summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='failed'},ensure_ascii=False,indent=2))
    print('Failed observations:',len(summary['failed']),flush=True)

if __name__=='__main__':main()

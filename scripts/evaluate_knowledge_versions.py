"""40 synthetic version lifecycles through both adapters; no model calls.

Ten subjects x two scopes x two directions are combinations of one lifecycle,
not 40 independent task types. User stores and credentials are never used.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent_knowledge_bridge.store import KnowledgeStore
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.governance import Governor

SUBJECTS=[('审核期限','24小时','48小时'),('日志保留期限','7天','14天'),
    ('失败重试次数','3次','5次'),('缓存容量','128MB','256MB'),
    ('页面主题','浅色','深色'),('回答语言','简体中文','英文'),
    ('连接超时','30秒','60秒'),('批处理间隔','5分钟','10分钟'),
    ('上传容量','10MB','20MB'),('备份间隔','1天','2天')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(); out=args.output
    if out.exists() and any(out.iterdir()): raise ValueError('Use an empty output directory')
    out.mkdir(parents=True,exist_ok=True)
    sources={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((ROOT/'src').rglob('*.py'))}
    rows=[]; lifecycles=[]
    for index,(subject,old_value,new_value) in enumerate(SUBJECTS):
        for scope in ('project','user'):
            for target,cls in [('codex',CodexLearningAdapter),('claude-code',ClaudeLearningAdapter)]:
                name=f'{index+1:02}-{scope}-{target}'
                path=out/(name+'.db'); store=KnowledgeStore(path)
                source='claude-code' if target=='codex' else 'codex'
                def publish(value,title):
                    return store.publish(source_agent=source,project_key='alpha',scope=scope,title=title,
                        content=f'{subject}正式定为{value}。',knowledge_type='decision',
                        evidence_summary='Synthetic version fixture')['knowledge']['id']
                def approve(key,**kwargs):
                    return store.feedback(agent_id='human-review',knowledge_id=key,outcome='verified',
                        evidence_kind='user_approval',evidence_ref='fixture://approval',
                        evidence_summary='Synthetic approval',**kwargs)
                old=publish(old_value,subject); approve(old)
                new=publish(new_value,subject+'设置'); names={old:'old',new:'new'}
                project='beta' if scope=='user' else 'alpha'
                adapter=cls(database_path=path,agent_id=target,project_key=project,reviewer=lambda _: {'proposals':[]})
                def recall(phase,query,expected):
                    adapter.recall({'session_id':'versions','turn_id':phase,'prompt':query})
                    trace=adapter.reuse.existing(target,project,'versions',phase)
                    got={names[item['knowledge_id']] for item in json.loads(trace['items_json']) if item['emitted']}
                    rows.append(dict(case=name,phase=phase,query=query,expected=sorted(expected),
                                     emitted=sorted(got),passed=got==set(expected)))
                recall('pending',f'{subject}是什么？',['old'])
                blocked=False
                try: approve(new)
                except ValueError: blocked=True
                approve(new,supersedes=[old])
                recall('current',f'{subject}是什么？',['new'])
                recall('history',f'查看历史{subject}',['old','new'])
                recall('excluded',f'不要引用{subject}，请解释太阳辐射。',[])
                store.remove_many(agent_id='human-review',project_key=project,knowledge_ids=[new])
                recall('successor_removed',f'{subject}是什么？',[])
                gov=Governor(store,policy={'lfhv_resurrect_threshold':2})
                for q in (subject,f'查看历史{subject}',f'请解释{subject}'):
                    gov.shadow_probe(project_key=project,query=q)
                restored=gov.resurrect(project_key=project)['restored']
                bypass=store.transit(old,to_status='active',reason='test bypass',actor='test')['changed']
                lifecycles.append(dict(case=name,blocked_implicit_replacement=blocked,
                    restored=restored,transit_bypass=bypass,passed=blocked and restored==0 and not bypass))
    report={'synthetic':True,'model_calls':0,'lifecycle_combinations':len(lifecycles),
        'lifecycle_passed':sum(r['passed'] for r in lifecycles),'recall_observations':len(rows),
        'recall_passed':sum(r['passed'] for r in rows),'source_hashes':sources,
        'source_unchanged':all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value for name,value in sources.items()),
        'limitations':'Template combinations; not live desktop E2E, LLM extraction quality, task success, or TTFT.'}
    for name,value in [('report.json',report),('results.json',rows),('lifecycles.json',lifecycles),('subjects.json',SUBJECTS)]:
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_hashes'},ensure_ascii=False,indent=2))
    if not report['source_unchanged'] or not all(r['passed'] for r in rows+lifecycles): sys.exit(1)


if __name__=='__main__': main()

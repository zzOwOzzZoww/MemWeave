"""Mixed-library challenge set: required/forbidden items, no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=ROOT)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('Use empty output')
    args.output.mkdir(parents=True,exist_ok=True)
    raw=(ROOT/'evaluations/memory_decisions_mixed.json').read_bytes()
    (args.output/'cases-frozen.json').write_bytes(raw)
    cases=json.loads(raw)
    sys.path.insert(0,str(args.source_root.resolve()/'src'))
    from agent_knowledge_bridge.store import KnowledgeStore
    from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
    from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
    results=[]
    for target,cls in [('codex',CodexLearningAdapter),('claude-code',ClaudeLearningAdapter)]:
        path=args.output/(target+'.db');store=KnowledgeStore(path);names={}
        for alias,spec in cases['records'].items():
            key=store.publish(source_agent='claude-code' if target=='codex' else 'codex',project_key='qinglan',
                **spec,evidence_summary='synthetic fixture')['knowledge']['id']
            names[key]=alias
            store.feedback(agent_id='human',knowledge_id=key,outcome='verified',evidence_kind='user_approval',
                           evidence_ref='fixture',evidence_summary='fixture')
        for case in cases['cases']:
            a=cls(database_path=path,agent_id=target,project_key=case.get('project','qinglan'),reviewer=lambda _: {'proposals':[]})
            a.recall({'session_id':'mixed','turn_id':case['id'],'prompt':case['query']})
            trace=a.reuse.existing(target,a.project_key,'mixed',case['id'])
            emitted=[names[i['knowledge_id']] for i in json.loads(trace['items_json']) if i['emitted']]
            missing=set(case['required'])-set(emitted)
            unwanted=set(case['forbidden'])&set(emitted)
            results.append({'case':case['id'],'target':target,'emitted':emitted,'missing':sorted(missing),
                            'unwanted':sorted(unwanted),'passed':not missing and not unwanted})
    report={'observations':len(results),'passed':sum(r['passed'] for r in results),
            'failed':[r for r in results if not r['passed']], 'case_sha256':hashlib.sha256(raw).hexdigest(),
            'source_hashes':{str(p.relative_to(args.source_root)):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted((args.source_root/'src').rglob('*.py'))}}
    for name,value in [('results.json',results),('report.json',report)]:
        (args.output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_hashes'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

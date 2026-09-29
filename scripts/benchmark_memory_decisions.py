"""Existing 100-case retrieval benchmark plus local scaling; no model requests."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--noise',type=int,default=1000)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('Use empty output')
    args.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(args.source_root.resolve()/'src'))
    # Load the selected package first: helper scripts cannot silently change
    # which baseline implementation the benchmark exercises.
    from agent_knowledge_bridge.store import KnowledgeStore
    from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
    from agent_knowledge_bridge.live_benchmark import build_live_benchmark
    from agent_knowledge_bridge.reuse import percentile
    from evaluate_memweave_live_100 import seed_knowledge
    dataset=build_live_benchmark()
    database=args.output/'benchmark.db'
    ids,_=seed_knowledge(database,dataset)
    aliases={v:k for k,v in ids.items()}
    store=KnowledgeStore(database)
    for i in range(args.noise):
        key=store.publish(source_agent='claude-code',project_key='benchmark-main',scope='project',
            title=f'distractor-{i:04d} inventory',content=f'Inventory marker distractor-{i:04d}: reference manual entry.',
            knowledge_type='fact',evidence_summary='Synthetic distractor')['knowledge']['id']
        aliases[key]=f'distractor-{i:04d}'
        store.feedback(agent_id='fixture',knowledge_id=key,outcome='verified',evidence_kind='test',
                       evidence_ref='synthetic',evidence_summary='fixture')
    adapters={}
    rows=[]
    for repeat in range(args.repeats):
        for case in dataset['cases']:
            config=(case['requester_agent'],case['project_key'])
            if config not in adapters:
                adapters[config]=ClaudeLearningAdapter(database_path=database,agent_id=config[0],
                    project_key=config[1],reviewer=lambda _: {'proposals':[]})
            a=adapters[config]
            turn=f"{repeat}-{case['id']}"
            start=time.perf_counter()
            a.recall({'session_id':'performance','turn_id':turn,'prompt':case['query'],'cwd':str(args.output)})
            ms=(time.perf_counter()-start)*1000
            trace=a.reuse.existing(config[0],config[1],'performance',turn)
            items=json.loads(trace['items_json'])
            got=[aliases[item['knowledge_id']] for item in items if item['emitted']]
            expected=case['expected_key']
            rows.append({'case':case['id'],'repeat':repeat,'category':case['category'],
                'emitted':got,'expected':expected,'hit':expected in got if expected else not got,
                'adapter_ms':ms,'retrieval_ms':trace['retrieval_ms']})
    positive=[r for r in rows if r['expected']]
    negative=[r for r in rows if not r['expected']]
    result={'cases':len(dataset['cases']),'observations':len(rows),'noise_records':args.noise,
        'total_knowledge_records':len(aliases),'repeats':args.repeats,
        'positive_recall':sum(r['hit'] for r in positive)/len(positive),
        'negative_injection_rate':sum(not r['hit'] for r in negative)/len(negative),
        'positive_observations':len(positive),'negative_observations':len(negative),
        'adapter_p50_ms':percentile([r['adapter_ms'] for r in rows],.5),
        'adapter_p95_ms':percentile([r['adapter_ms'] for r in rows],.95),
        'retrieval_p50_ms':percentile([r['retrieval_ms'] for r in rows],.5),
        'retrieval_p95_ms':percentile([r['retrieval_ms'] for r in rows],.95),
        'by_category':{},'model_calls':0,
        'scope':'Local Adapter only; excludes native hooks, provider inference and TTFT. Ordered process runs are not a randomized causal latency experiment.',
        'source_hashes':{str(p.relative_to(args.source_root)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted((args.source_root/'src').rglob('*.py'))}}
    for category in sorted({r['category'] for r in rows}):
        selected=[r for r in rows if r['category']==category]
        result['by_category'][category]={'observations':len(selected),'hits':sum(r['hit'] for r in selected)}
    for filename,content in [('report.json',result),('observations.json',rows)]:
        (args.output/filename).write_text(json.dumps(content,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='source_hashes'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()

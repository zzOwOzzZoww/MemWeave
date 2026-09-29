"""Run naturalistic injection and admission boundaries through the local Core.

The cases are hand-authored conversational paraphrases, not user telemetry.
No model/API calls are made. The evaluator uses the same FTS, decision guards,
ReuseStore and grounded admission function used by the adapters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from agent_knowledge_bridge.claude_transcript import TranscriptTurn
from agent_knowledge_bridge.codex_learning_adapter import CodexLearningAdapter
from agent_knowledge_bridge.claude_learning_adapter import ClaudeLearningAdapter
from agent_knowledge_bridge.decisions import grounded_user_proposal
from agent_knowledge_bridge.store import KnowledgeStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(); out=args.output
    if out.exists() and any(out.iterdir()): raise ValueError('Use an empty output directory')
    out.mkdir(parents=True,exist_ok=True)
    cases=json.loads((ROOT/'evaluations/memory_decisions_natural_20260926.json').read_text(encoding='utf-8'))
    source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted((ROOT/'src').rglob('*.py'))}

    with tempfile.TemporaryDirectory(prefix='memweave-natural-') as folder:
        database=Path(folder)/'natural.db'; store=KnowledgeStore(database)
        aliases={}
        for item in cases['knowledge']:
            project='chiyu' if item['alias']=='project-other' else 'qinglan'
            key=store.publish(source_agent='claude-code',project_key=project,
                title=item['title'],content=item['content'],knowledge_type=item['knowledge_type'],
                scope=item['scope'],search_terms=item['search_terms'],evidence_summary='natural fixture')['knowledge']['id']
            store.feedback(agent_id='human-review',knowledge_id=key,outcome='verified',
                evidence_kind='user_approval',evidence_ref='fixture://natural',evidence_summary='fixture')
            aliases[key]=item['alias']

        injection=[]
        for target, adapter_class in [('codex',CodexLearningAdapter),('claude-code',ClaudeLearningAdapter)]:
            adapter_cache={}
            for case in cases['cases']:
                project=case['project']
                adapter=adapter_cache.setdefault(project,adapter_class(database_path=database,agent_id=target,
                    project_key=project,reviewer=lambda _: {'proposals':[]}))
                turn=f"{target}-{case['id']}"
                adapter.recall({'session_id':'natural','turn_id':turn,'prompt':case['query']})
                trace=adapter.reuse.existing(target,project,'natural',turn)
                items=json.loads(trace['items_json']) if trace else []
                emitted=[aliases[item['knowledge_id']] for item in items if item['emitted']]
                expected=case['expected']
                passed=set(emitted)==set(expected)
                injection.append({'target':target,**case,'emitted':emitted,'passed':passed,
                    'omitted':[{'alias':aliases.get(item['knowledge_id']), 'reason':item.get('omitted_reason')}
                               for item in items if not item['emitted']]})

        admission=[]
        for case in cases['admission']:
            turn=TranscriptTurn(case['user'],'知道了',())
            try:
                result=grounded_user_proposal({'source_quotes':[case['user']],
                    'knowledge_type':case['knowledge_type']},turn)
                actual='auto_accept' if result['auto_accept'] else 'review'
                error=None; scope=result['scope']
            except ValueError as exc:
                actual='reject'; scope=None; error=str(exc)
            admission.append({**case,'actual':actual,'actual_scope':scope,'error':error,
                'passed':actual==case['expected'] and (scope is None or scope==case['scope'])})

    report={
        'kind':'natural_boundaries_local', 'model_calls':0,
        'injection_cases':len(injection),'injection_observations':len(injection),
        'injection_passed':sum(row['passed'] for row in injection),
        'injection_rate':round(sum(row['passed'] for row in injection)/len(injection),3),
        'admission_cases':len(admission),'admission_passed':sum(row['passed'] for row in admission),
        'admission_rate':round(sum(row['passed'] for row in admission)/len(admission),3),
        'admission_actual_counts':{value:sum(row['actual']==value for row in admission)
                                   for value in ('auto_accept','review','reject')},
        'source_hashes':source_hashes,
        'source_unchanged':all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value
                                for name,value in source_hashes.items()),
        'limitations':'Synthetic hand-authored Chinese paraphrases; no live desktop transcript, model extraction, task success, or TTFT.'
    }
    for name,value in [('report.json',report),('injection.json',injection),('admission.json',admission)]:
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='source_hashes'},ensure_ascii=False,indent=2))


if __name__=='__main__': main()

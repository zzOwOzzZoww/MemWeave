"""Local cost of experience gates and feedback ranking (no model calls)."""
import argparse
import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from agent_knowledge_bridge import store as module, retrieval_pipeline as pipeline
from agent_knowledge_bridge.experiences import encode_contract
from agent_knowledge_bridge.service import KnowledgeBridgeService
from agent_knowledge_bridge.retrieval_stats import FREQUENCIES
from benchmark_recall_frequency import timing

@contextlib.contextmanager
def gates(enabled):
    original_filter, original_stage_filter, original_rank = module.filter_rows, pipeline.filter_rows, pipeline.rank_equivalent
    if not enabled:
        module.filter_rows = pipeline.filter_rows = lambda rows, query: rows
        pipeline.rank_equivalent = lambda db, rows, **kw: (rows, 0)
    try:
        yield
    finally:
        module.filter_rows, pipeline.filter_rows, pipeline.rank_equivalent = original_filter, original_stage_filter, original_rank

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/experience-loop-20260924')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix='memweave-exp-cost-') as folder:
        for kind in ('ordinary', 'contracts'):
            service = KnowledgeBridgeService(agent_id='claude-code', project_key='cost',
                                             database_path=Path(folder)/(kind+'.db'))
            for index in range(100):
                contract = {"version":1,"applies_when":["widget", "Windows"],"exclude_when":["Linux"],
                    "steps":["Read the widget schema.","Keep field order."],"avoid":[],
                    "reason":"The field-order verifier previously failed then passed.",
                    "verifier":{"kind":"observed_command","command":"python verify_widget.py"}}
                content = (encode_contract(contract) if kind == 'contracts' else 'Preserve widget field order on Windows.') + (' ' * index)
                # Trailing whitespace is stripped by publish; title differentiates records.
                created = service.publish(title=f'widget procedure {index}',content=content,
                    knowledge_type='procedure',evidence_summary='benchmark',search_terms='widget Windows')
                key = created['knowledge']['id']
                service.store.feedback(agent_id='claude-code',knowledge_id=key,outcome='verified',
                    evidence_summary='test fixture',evidence_kind='test',evidence_ref='fixture')
            times = {'disabled':[], 'enabled':[]}
            signatures = {}
            args_search = dict(requester_agent='codex',project_key='cost',query='widget Windows',limit=3)
            for repeat in range(30):
                for enabled in ((False,True) if repeat%2 == 0 else (True,False)):
                    label='enabled' if enabled else 'disabled'
                    with gates(enabled):
                        service.store.search(**args_search)
                        start=time.perf_counter()
                        result=service.store.search(**args_search)
                        times[label].append((time.perf_counter()-start)*1000)
                        signatures[label]=[(r['id'],r['origin']) for r in result['results']]
            results[kind]={'rows':100,'warm_search':{k:timing(v) for k,v in times.items()},
                           'same_result_for_applicable_queries':signatures['enabled']==signatures['disabled']}
    report={'measurement':'Same current pipeline with experience gates/ranking enabled vs disabled, 30 alternating warmed repeats. No models or task-success claims.',
            'corpora':results,'cache_budget':FREQUENCIES.info()}
    (args.output/'experience-cost.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return int(any(not r['same_result_for_applicable_queries'] for r in results.values()))

if __name__=='__main__':
    raise SystemExit(main())


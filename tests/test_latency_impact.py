import json
from contextlib import closing
import sqlite3

import pytest

from agent_knowledge_bridge.latency_impact import initialize, record_pair, metrics


def pair(pair_id='pair1', **changes):
    common = {'model':'model-a', 'model_config_hash':'a'*64, 'prompt_hash':'b'*64,
        'base_context_hash':'c'*64, 'environment_hash':'d'*64, 'cache_policy':'controlled-warm',
        'timing_source':'codex.task_complete','turn_id':'t','response_total_ms':9000}
    args = dict(project_key='p', experiment_id='exp', pair_id=pair_id, agent_id='codex',
        baseline_mode='framework_absent', execution_order='off_on', conditions_confirmed=True,
        on={**common,'session_id':pair_id+'on','ttft_ms':1500},
        off={**common,'session_id':pair_id+'off','ttft_ms':1000})
    args.update(changes)
    return args


def report(path):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize(db)
        return metrics(db, 'p')


def test_empty_is_unknown_not_zero(tmp_path):
    r = report(tmp_path / 'k.db')
    assert r['pairs'] == 0 and r['ttft_delta_ms']['p50'] is None


def test_pair_delta_not_subtraction_of_percentiles(tmp_path):
    path = tmp_path / 'k.db'
    for n, (on, off) in enumerate([(1500,1000),(100,2000),(2200,200)]):
        p = pair(str(n)); p['on']['ttft_ms']=on; p['off']['ttft_ms']=off
        record_pair(path, **p)
    r = report(path)
    assert r['ttft_delta_ms'] == {'p50':500,'p95':2000}
    assert r['ttft_delta_ms']['p95'] != r['on_ttft_ms']['p95'] - r['off_ttft_ms']['p95']
    assert r['status'] == 'limited_sample' and not r['order_balanced']


@pytest.mark.parametrize('field,value', [('model','different'),('base_context_hash','e'*64),
    ('prompt_hash','f'*64), ('environment_hash','0'*64), ('model_config_hash','1'*64),
    ('ttft_ms',True),('ttft_ms',-1),('response_total_ms',100)])
def test_reject_incomparable_or_invalid_samples(tmp_path, field, value):
    p=pair(); p['off'][field]=value
    with pytest.raises(ValueError):record_pair(tmp_path/'k.db', **p)


def test_immutable_pairs_and_native_turn_cannot_be_recounted(tmp_path):
    path=tmp_path/'k.db'; p=pair()
    record_pair(path,**p)
    assert record_pair(path,**p)['status']=='already_recorded'
    with pytest.raises(ValueError,match='already used'):
        record_pair(path,**{**p,'pair_id':'duplicate'})
    p['on']['ttft_ms']+=1
    with pytest.raises(ValueError,match='different evidence'):record_pair(path,**p)


def test_different_experiments_are_not_pooled(tmp_path):
    path=tmp_path/'k.db'
    record_pair(path,**pair())
    p=pair('pair2',experiment_id='new',baseline_mode='hook_bypass')
    record_pair(path,**p)
    assert report(path)['pairs']==1 and report(path)['baseline_mode']=='hook_bypass'
    p=pair('pair3',baseline_mode='hook_bypass')
    with pytest.raises(ValueError,match='conditions changed'):record_pair(path,**p)


def test_telemetry_has_no_corpus_revision_or_knowledge_side_effects(tmp_path):
    from agent_knowledge_bridge.store import KnowledgeStore
    path=tmp_path/'k.db'; store=KnowledgeStore(path)
    with store._connect() as db:before=tuple(db.execute('SELECT * FROM retrieval_revision').fetchone())
    record_pair(path,**pair())
    with store._connect() as db:
        assert tuple(db.execute('SELECT * FROM retrieval_revision').fetchone())==before
        assert db.execute('SELECT count(*) FROM knowledge_records').fetchone()[0]==0


def test_importer_reads_native_prompt_model_and_timing(tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('latency_importer',Path(__file__).resolve().parents[1]/'scripts'/'import_latency_pair.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    path=tmp_path/'rollout.jsonl'
    events=[{'type':'session_meta','payload':{'id':'s','model_provider':'test'}},
        {'type':'event_msg','payload':{'type':'task_started','turn_id':'t'}},
        {'type':'turn_context','payload':{'turn_id':'t','model':'model-a','effort':'high'}},
        {'type':'response_item','payload':{'type':'message','role':'user',
                                         'content':[{'type':'input_text','text':'test task'}]}},
        {'type':'event_msg','payload':{'type':'task_complete','turn_id':'t',
            'time_to_first_token_ms':1500,'duration_ms':3000}}]
    path.write_text('\n'.join(json.dumps(e) for e in events),encoding='utf-8')
    sample, config=module.native_sample({'transcript_path':str(path),'session_id':'s','turn_id':'t'},pair()['on'])
    assert sample['ttft_ms']==1500 and config==('test','high')
    assert sample['prompt_hash']!=pair()['on']['prompt_hash']  # derived, not trusted input
    with pytest.raises(ValueError,match='native turn'):
        module.native_sample({'transcript_path':str(path),'session_id':'s','turn_id':'t'},
                             {**pair()['on'],'model':'wrong'})


def test_explicit_hook_bypass_does_not_touch_runtime_or_learning(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('baseline_hook',Path(__file__).resolve().parents[1]/'scripts'/'codex_learning_hook.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setenv('MW_LATENCY_BASELINE','hook_bypass')
    def unexpected(*args,**kwargs):raise AssertionError('baseline touched normal processing')
    for name in ('selected_runtime','append_audit','resolve_transcript_path'):
        monkeypatch.setattr(module,name,unexpected)
    assert module.run_hook_input({'hook_event_name':'UserPromptSubmit'})==0
    assert capsys.readouterr().out.strip()=='{}'

"""Ordered first counterexample and bounded work in pooled candidate audits."""
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import segment_candidates as mod


def setup(monkeypatch,results):
    jobs=[('full','scene','anchor',[0]*64) for _ in range(5)]
    monkeypatch.setattr(mod.Observation,'model_validate',lambda x:x)
    monkeypatch.setattr(mod,'make_blocks',lambda *args:jobs)
    calls=[]
    def run(batch,**kw):
        calls.append(kw)
        item=results.pop(0)
        if isinstance(item,Exception):raise item
        return item
    pool=SimpleNamespace(worker_count=2,map=run)
    service=SimpleNamespace(_scene=lambda *args:'scene')
    args=({'rows':[], 'skill':'transfer_above_hole','request_id':'r'},
          dict(full_reference=[],held_reference=[],observation={}),service,None)
    return args,pool,calls


def clear(count=64):return dict(samples=count,failures=[])
def bad(count,stamp):return dict(samples=count,failures=[dict(virtual_ns=stamp,fraction=.5)],stopped_on_first_failure=True)


def test_first_ordered_rejection_discards_later_speculative_work(monkeypatch):
    args,pool,calls=setup(monkeypatch,[[clear(),clear()],[bad(3,100),bad(1,200)]])
    result=mod.audit_candidate(*args,pool=pool,context_binding='a'*64)[0]
    assert len(calls)==2 and result['samples']==131 and result['computed_samples']==132
    assert result['discarded_after_first_failure']==1 and result['failures'][0]['virtual_ns']==100
    assert not result['complete'] and not result['passed']
    assert all(c['stop_on_failure'] and c['context_binding']=='a'*64 for c in calls)


def test_clear_candidate_requires_all_waves(monkeypatch):
    args,pool,calls=setup(monkeypatch,[[clear(),clear()],[clear(),clear()],[clear()]])
    result=mod.audit_candidate(*args,pool=pool,context_binding='b'*64)[0]
    assert len(calls)==3 and result['samples']==320 and result['computed_samples']==320
    assert result['complete'] and result['passed']


def test_worker_error_after_clear_wave_propagates_without_recommendation(monkeypatch):
    args,pool,calls=setup(monkeypatch,[[clear(),clear()],RuntimeError('worker gone')])
    with pytest.raises(RuntimeError,match='worker gone'):
        mod.audit_candidate(*args,pool=pool,context_binding='c'*64)
    assert len(calls)==2


def test_missing_state_binding_never_submits_job(monkeypatch):
    args,pool,calls=setup(monkeypatch,[])
    with pytest.raises(ValueError,match='state_binding'):
        mod.audit_candidate(*args,pool=pool)
    assert not calls

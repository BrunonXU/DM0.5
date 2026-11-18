"""Advice affects scheduling only; current checks still decide every candidate."""
import pytest

from f1_harness.candidate_ranking import order_variants
from f1_harness.contracts import HarnessError
from test_planning_lessons import planning


@pytest.mark.parametrize('preference',[[],[0,1,2],[0,1,2,2],[0,1,2,4],
                                     [False,1,2,3],[0.,1,2,3],'0123',{'0':0}])
def test_bad_advice_does_not_interrupt_default_solver(planning,caplog,preference):
    service,kwargs,calls=planning
    service.fixture['candidate_order']={'approach_grasp':preference}
    result=service.dispatch('plan_skill',kwargs)
    assert calls==[0,1,2] and result['candidate_variant']==2
    rows=result['geometry_trace'][0]['candidate_ranking']
    assert all(r['ordering_warning']=='candidate_order_permutation_invalid' for r in rows)
    assert 'using default candidate ranking' in caplog.text


@pytest.mark.parametrize('preferences',[[],True,'not-a-map'])
def test_invalid_mapping_also_falls_back(planning,preferences):
    service,kwargs,calls=planning
    service.fixture['candidate_order']=preferences
    result=service.dispatch('plan_skill',kwargs)
    assert calls==[0,1,2]
    assert result['geometry_trace'][0]['candidate_ranking'][0]['ordering_warning']=='candidate_order_mapping_invalid'


def test_complete_preference_skips_overridden_heuristic_but_not_solve(planning,monkeypatch):
    import f1_harness.candidate_ranking as ranking
    service,kwargs,calls=planning
    service.fixture['candidate_order']={'approach_grasp':[1,2,3,0]}
    def forbidden(*a,**kw):
        raise AssertionError('discarded heuristic must not run')
    monkeypatch.setattr(ranking,'rank_variants',forbidden)
    result=service.dispatch('plan_skill',kwargs)
    # Preferred candidate is actually rejected by the current solver; next runs.
    assert calls==[1,2] and result['candidate_variant']==2
    assert result['geometry_trace'][0]['candidate_evaluation']['attempts'][0]['outcome']=='rejected'


def test_invalid_advice_preserves_original_heuristic_order(monkeypatch):
    import f1_harness.candidate_ranking as ranking
    monkeypatch.setattr(ranking,'rank_variants',lambda *a,**k:([3,1],[{'variant':3,'score':.1},{'variant':1,'score':2.}]))
    order,rows=order_variants('approach_grasp',None,[1,3],None,preferences={'approach_grasp':[0]})
    assert order==[3,1] and [r['score'] for r in rows]==[.1,2.]


def test_start_and_exclusions_never_reenabled(planning):
    service,kwargs,calls=planning
    service.fixture['candidate_order']={'approach_grasp':[0,1,2,3]}
    kwargs['intent'].update(candidate_start=1,excluded_candidates=[1,2])
    result=service.dispatch('plan_skill',kwargs)
    assert calls==[3] and result['candidate_variant']==3


@pytest.mark.parametrize('preferences',[{'approach_grasp':[2,0,1,3]},{'approach_grasp':[0]}])
def test_advice_never_swallows_numerical_or_authority_fault(planning,monkeypatch,preferences):
    service,kwargs,_=planning
    service.fixture['candidate_order']=preferences
    def fault(*a,deadline_ns=None): raise HarnessError('numerical_feedback_identity_mismatch')
    monkeypatch.setattr(service,'_solve_candidate',fault)
    with pytest.raises(HarnessError,match='numerical_feedback_identity_mismatch'):
        service.dispatch('plan_skill',kwargs)

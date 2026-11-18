"""A request's remaining time follows every stage, route and advisory."""
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import numerical_failure_detail
from test_grasp_selection_planning import setup


def test_stages_receive_remaining_request_budget(monkeypatch):
    service,obs,intent,clock=setup()
    service.fixture['grasp_pitch_offsets_deg']=[]
    intent=intent.model_copy(update={'budget_ms':2000,'skill':'grasp'})
    budgets=[];native=service.numerics.call
    def counted(method,**kwargs):
        if method=='solve_path':
            budgets.append(kwargs['scene']['planning_budget_s'])
            clock[0]+=500_000_000
        return native(method,**kwargs)
    monkeypatch.setattr(service.numerics,'call',counted)
    result=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    assert result['targets'] and len(budgets)>=2
    assert budgets==[2.-.5*i for i in range(len(budgets))]


def test_expired_request_never_starts_another_stage_or_route(monkeypatch):
    service,obs,intent,clock=setup()
    service.fixture['grasp_pitch_offsets_deg']=[]
    intent=intent.model_copy(update={'budget_ms':2000,'skill':'grasp'})
    budgets=[];native=service.numerics.call
    def counted(method,**kwargs):
        if method=='solve_path':
            budgets.append(kwargs['scene']['planning_budget_s'])
            clock[0]+=2_000_000_000
        return native(method,**kwargs)
    monkeypatch.setattr(service.numerics,'call',counted)
    with pytest.raises(HarnessError) as caught:
        service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            full22=[0.]*22,held16=list(obs.state16)))
    assert numerical_failure_detail(caught.value)['code']=='planning_budget_exhausted'
    assert budgets==[2.]
    assert not service.candidate_failures.entries  # Timing is not infeasibility.


def test_expired_prediction_preserves_completed_approach(monkeypatch):
    service,obs,intent,clock=setup();service.fixture['native_grasp_lookahead']=True
    deadline=clock[0]+intent.budget_ms*1_000_000;calls=[]
    def solve(request,scene,held,full,variant,*,deadline_ns=None):
        calls.append(request.skill)
        assert deadline_ns==deadline
        if request.skill=='grasp':raise HarnessError('registered_candidate_planning_budget_exhausted')
        clock[0]=deadline
        return dict(targets=[list(held)],geometry_trace=[{}])
    monkeypatch.setattr(service,'_solve_candidate',solve)
    result=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        full22=[0.]*22,held16=list(obs.state16)))
    advice=result['geometry_trace'][0]['native_grasp_lookahead']
    assert calls==['approach_grasp','grasp']
    assert advice['feasible'] is None and not advice['checked'] and advice['advisory_only']
    assert not advice['motion_authorized']

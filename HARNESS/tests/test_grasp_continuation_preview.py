"""Pregrasp selection must account for the native contact/lift continuation."""
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.adapters import _planning_feedback
from test_grasp_selection_planning import setup


def test_reachable_approach_keeps_predicted_grasp_failure_as_advice(monkeypatch):
    service,obs,intent,_=setup()
    service.fixture['native_grasp_lookahead']=True
    monkeypatch.setattr('f1_harness.planning_lessons.planning_scope',lambda *args:dict(synthetic_test=True))
    calls=[]
    def solve(i,scene,held,full,variant,*,deadline_ns=None): 
        pitch=scene.grasp_candidate_descriptor['pitch_offset_deg']
        calls.append((i.skill,pitch,scene.grasp_candidate_variant))
        if i.skill=='grasp':
            assert i.parameters.supplied()=={} and variant==0
            if pitch==0:raise HarnessError('bounded_IK_did_not_reach_goal:{"position_error_m":0.00071}')
        return dict(targets=[list(held)],geometry_trace=[{}])
    monkeypatch.setattr(service,'_solve_candidate',solve)
    result=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        held16=obs.state16,full22=[0.]*22))
    assert calls==[('approach_grasp',0.,0),('grasp',0.,0)]
    proof=result['geometry_trace'][0]['native_grasp_lookahead']
    assert proof['checked'] and not proof['motion_authorized'] and proof['actual_grasp_requires_new_measurements']
    assert proof['advisory_only'] and proof['feasible'] is False
    assert proof['diagnostic']['code']=='bounded_IK_did_not_reach_goal'
    assert proof['diagnostic']['metrics']['position_error_m']==.00071
    assert result['geometry_trace'][0]['candidate_evaluation']['attempts'][0]['outcome']=='planned'


def test_continuation_transport_failure_is_not_hidden_by_candidate_search(monkeypatch):
    service,obs,intent,_=setup();service.fixture['native_grasp_lookahead']=True
    calls=[]
    def solve(i,scene,held,full,variant,*,deadline_ns=None): 
        calls.append(i.skill)
        if i.skill=='grasp':raise HarnessError('connection_lost')
        return dict(targets=[list(held)],geometry_trace=[{}])
    monkeypatch.setattr(service,'_solve_candidate',solve)
    with pytest.raises(HarnessError,match='connection_lost'):
        service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
            held16=obs.state16,full22=[0.]*22))
    assert calls==['approach_grasp','grasp']


def test_missing_opt_in_preserves_previous_single_stage_planning(monkeypatch):
    service,obs,intent,_=setup();calls=[]
    def solve(i,*args,deadline_ns=None): calls.append(i.skill);return dict(geometry_trace=[{}])
    monkeypatch.setattr(service,'_solve_candidate',solve)
    service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),
        held16=obs.state16,full22=[0.]*22))
    assert calls==['approach_grasp']


def test_model_diagnostic_names_failed_continuation_not_just_generic_ik():
    value=_planning_feedback(numerical_failure_detail(HarnessError(
        'grasp_continuation_unreachable:{"continuation_skill":"grasp",'
        '"continuation_code":"bounded_IK_did_not_reach_goal","position_error_m":0.00071}')))
    assert value['code']=='grasp_continuation_unreachable'
    assert value['metrics']['continuation_skill']=='grasp'
    assert value['metrics']['continuation_code']=='bounded_IK_did_not_reach_goal'

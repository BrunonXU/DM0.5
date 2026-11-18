"""Transfer predictions are selection advice, not future execution authority."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError,MotionParameters
from f1_harness.placement_advisory import placement_advisory
from f1_harness.transport_advisory import native_transport_advisory
from test_grasp_selection_planning import setup
from test_full_development_skills import scene


def fixture(outcome='feasible',deadline=1,search=False,time_per_attempt_ns=0):
    _,_,intent,_=setup()
    calls=[];clock=[0]
    contact=[0.]*15+[100.];lift=list(contact);lift[7]=.1
    def forward(method,**kwargs):
        assert method=='forward_pose'
        pose=np.eye(4);pose[2,3]=kwargs['held16'][7];return pose
    def solve(i,s,held,full,variant,*,joint_search=False,deadline_ns=None): 
        clock[0]+=time_per_attempt_ns
        calls.append(('joint',variant) if joint_search else variant)
        assert i.skill=='transfer_above_hole' and i.parameters==MotionParameters()
        assert s.object_held and np.allclose(s.base_object,s.base_eef@s.eef_object)
        assert held==lift
        if outcome=='unknown':raise HarnessError('connection_lost')
        if outcome=='timeout':raise HarnessError('bounded_IK_wall_time_exceeded')
        if outcome=='search' and not joint_search:raise HarnessError('bounded_IK_did_not_reach_goal')
        if outcome=='blocked' or variant==0:raise HarnessError('bounded_IK_did_not_reach_goal')
        return dict(targets=[held])
    service=SimpleNamespace(clock=lambda:clock[0],numerics=SimpleNamespace(call=forward),_solve_candidate=solve,
                            fixture=dict(joint_space_transport=search))
    grasp=dict(targets=[contact,lift],geometry_trace=[dict(semantic_stage='grasp_close',end_index=1)])
    report=native_transport_advisory(service,intent,scene(),grasp,[0.]*22,deadline)
    return report,calls


def test_transfer_stops_after_first_native_feasible_route_and_remains_prediction():
    report,calls=fixture()
    assert report['status']=='predicted_feasible' and calls==[0,1]
    assert report['feasible_variant']==1 and not report['motion_authorized']
    assert report['actual_transfer_requires_new_measurements']


@pytest.mark.parametrize('outcome,expected,count',[
    ('blocked','no_registered_route',4),('unknown','incomplete',1),('timeout','incomplete',1)])
def test_unknown_solver_or_transport_failure_is_not_infeasibility(outcome,expected,count):
    report,calls=fixture(outcome)
    assert report['status']==expected and len(calls)==count


def test_expired_advisory_budget_does_not_search_or_claim_blocked():
    report,calls=fixture(deadline=0)
    assert report['status']=='incomplete' and not calls


def test_predicted_transfer_uses_same_optional_search_as_actual_planning():
    report,calls=fixture('search',deadline=20_000_000_000,search=True)
    assert calls==[0,1,2,3,('joint',2)]
    assert report['status']=='predicted_feasible' and not report['motion_authorized']
    assert report['checked_variants'][-1]['planner']=='joint_space_search'


def test_predicted_search_without_time_is_unknown_not_a_new_grasp_veto():
    report,calls=fixture('search',deadline=4,search=True,time_per_attempt_ns=1)
    assert calls==[0,1,2,3]
    assert report['status']=='incomplete' and not report['motion_authorized']


def test_geometric_clearance_does_not_hide_native_transfer_failure():
    report,_=fixture('blocked')
    result=placement_advisory([dict(native_grasp_lookahead=dict(transport=report),
        grasp_route_lookahead=dict(precision_placement_preview=dict(candidates=[
            dict(transfer_variant=0,outcome='geometrically_clear')])) )])
    assert result['status']=='direct_transfer_unresolved' and result['geometry_status']=='predicted_clear'
    assert result['transport_numerics']['recovery_checked'] is False
    assert result['transport_numerics']['scope']=='registered_routes_from_predicted_grasp_endpoint'
    assert result['blocked_skill']=='transfer_above_hole'
    assert len(result['transport_numerics']['failures'])==4
    assert not result['numerical_feasibility_checked'] and not result['motion_authorized']


def test_blocked_future_transfer_does_not_reject_current_approach(monkeypatch):
    service,obs,intent,_=setup();service.fixture['native_grasp_lookahead']=True
    calls=[]
    def solve(i,s,held,full,variant,*,deadline_ns=None): 
        calls.append(i.skill)
        return dict(targets=[list(held)],geometry_trace=[dict(semantic_stage='grasp_close',end_index=1)])
    monkeypatch.setattr(service,'_solve_candidate',solve)
    monkeypatch.setattr('f1_harness.transport_advisory.native_transport_advisory',
        lambda *args:dict(status='no_registered_route'))
    kwargs=dict(intent=intent.model_dump(),observation=obs.model_dump(),held16=obs.state16,full22=[0.]*22)
    ordinary=service.dispatch('plan_skill',kwargs)
    default_preview=service.dispatch('plan_skill',dict(kwargs,preview=True))
    assert 'transport' not in default_preview['geometry_trace'][0]['native_grasp_lookahead']
    service.fixture['native_transport_lookahead_on_preview']=True
    predicted=service.dispatch('plan_skill',dict(kwargs,preview=True))
    assert ordinary['targets']==predicted['targets']
    assert 'transport' not in ordinary['geometry_trace'][0]['native_grasp_lookahead']
    assert predicted['geometry_trace'][0]['native_grasp_lookahead']['transport']['status']=='no_registered_route'
    assert calls==['approach_grasp','grasp']*2
    assert default_preview['geometry_trace'][0]['reused_numerical_candidate']['fresh_revalidation_required']

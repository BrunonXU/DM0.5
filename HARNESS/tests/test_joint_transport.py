"""The optional search changes path generation, not request or skill authority."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.transport import joint_transport_goal,uses_joint_transport,joint_transport_bounds
from f1_harness.skills import propose
from test_registered_transport import loaded,template
from test_plan_revalidation import scenario


def test_joint_goal_keeps_loaded_endpoint_without_requiring_template_path():
    value=replace(loaded(),success_trajectory=template())
    goal=joint_transport_goal(value)['stages'][0]
    old=propose('transfer_above_hole',value,variant=2)['stages'][0]
    np.testing.assert_allclose(goal['goals'][-1],old['goals'][-1])
    assert len(goal['goals'])==1 and goal['gripper'] is None
    assert goal['route_family']=='joint_space_transport'
    import json
    json.dumps(joint_transport_bounds(value),allow_nan=False)
    with pytest.raises(HarnessError,match='measured_in_hand'):
        joint_transport_goal(replace(value,object_held=None))


@pytest.mark.parametrize('enabled,variant,budget,expected',[
    (True,None,30000,True),(False,None,30000,False),
    (True,2,30000,False),(True,None,1,True)])
def test_search_follows_two_ranked_connections_with_remaining_budget(scenario,enabled,variant,budget,expected):
    service,args,clock,original,latest,intent=scenario
    held,full=args['held16'],args['full22']
    clock[0]=original.received_ns
    value=loaded();service._scene=lambda *a,**kw:value
    service.fixture['joint_space_transport']=enabled
    intent=intent.model_copy(update=dict(skill='transfer_above_hole',budget_ms=budget,
        parameters=intent.parameters.model_copy(update=dict(variant=variant))))
    attempts=[]
    def solve(intent,scene,held,full,variant,*,joint_search=False,deadline_ns=None): 
        attempts.append((variant,joint_search))
        if not joint_search:raise HarnessError('bounded_IK_did_not_reach_goal')
        return dict(geometry_trace=[dict(route_family='joint_space_transport')])
    service._solve_candidate=solve
    kwargs=dict(intent=intent.model_dump(),observation=original.model_dump(),held16=held,full22=full)
    if expected:
        result=service.dispatch('plan_skill',kwargs)
        assert len(result['geometry_trace'][0]['preceding_candidate_failures'])==2
        assert attempts==[(0,False),(1,False),(2,True)]
    else:
        with pytest.raises(HarnessError,match='registered_candidates_exhausted'):
            service.dispatch('plan_skill',kwargs)
        assert not any(search for _,search in attempts)


@pytest.mark.parametrize('failure',['numerical_joint_search_wall_time_exceeded','actual_IK_candidate_collision_rejected'])
def test_failed_joint_search_keeps_remaining_registered_candidates(scenario,failure):
    service,args,clock,original,latest,intent=scenario
    clock[0]=original.received_ns;service._scene=lambda *a,**kw:loaded()
    service.fixture['joint_space_transport']=True
    import json
    service.fixture=json.loads(json.dumps(service.fixture,default=lambda value:value.tolist()))
    intent=intent.model_copy(update=dict(skill='transfer_above_hole',budget_ms=30000))
    attempts=[]
    def solve(intent,scene,held,full,variant,*,joint_search=False,deadline_ns=None): 
        attempts.append((variant,joint_search))
        if joint_search:raise HarnessError(failure)
        if variant in (0,1):raise HarnessError('bounded_IK_did_not_reach_goal')
        return dict(geometry_trace=[dict(route_family='orientation_preserving_transport')])
    service._solve_candidate=solve
    result=service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        held16=args['held16'],full22=args['full22']))
    assert attempts==[(0,False),(1,False),(2,True),(2,False)]
    logged=result['geometry_trace'][0]['candidate_evaluation']['attempts']
    assert logged[2]['diagnostic']['code']==failure and logged[-1]['outcome']=='planned'


def test_second_ranked_connection_avoids_unnecessary_joint_search(scenario):
    service,args,clock,original,_,intent=scenario
    clock[0]=original.received_ns;service._scene=lambda *a,**kw:loaded()
    service.fixture['joint_space_transport']=True
    intent=intent.model_copy(update=dict(skill='transfer_above_hole',budget_ms=30000))
    attempts=[]
    def solve(i,s,h,f,variant,*,joint_search=False,deadline_ns=None):
        attempts.append((variant,joint_search))
        assert not joint_search
        if variant==0:raise HarnessError('bounded_IK_did_not_reach_goal')
        return dict(geometry_trace=[{}])
    service._solve_candidate=solve
    service.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
        held16=args['held16'],full22=args['full22']))
    assert attempts==[(0,False),(1,False)]


def test_revalidation_cannot_reinterpret_search_without_its_fixture_binding():
    proposal=SimpleNamespace(geometry_trace=[dict(route_family='joint_space_transport')],candidate_variant=2)
    intent=SimpleNamespace(skill='transfer_above_hole')
    assert uses_joint_transport({'joint_space_transport':True},intent,proposal)
    with pytest.raises(HarnessError,match='contract_changed'):
        uses_joint_transport({},intent,proposal)
    with pytest.raises(HarnessError,match='contract_changed'):
        uses_joint_transport({'joint_space_transport':True},SimpleNamespace(skill='insert'),proposal)

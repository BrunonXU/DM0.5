"""The optional search changes path generation, not request or skill authority."""
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.transport import joint_transport_goal, joint_transport_offsets, uses_joint_transport, joint_transport_bounds
from f1_harness.skills import propose
from test_full_development_skills import scene
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


def test_transport_region_uses_first_clear_offset_in_measured_hole_plane(scenario):
    from scipy.spatial.transform import Rotation
    service, *_ = scenario
    hole = scene().base_hole.copy()
    hole[:3, :3] = Rotation.from_euler('z', 90, degrees=True).as_matrix()
    wall = dict(min=[.499, -.032, .494], max=[.501, -.030, .496], role='bucket', name='rim')
    value = replace(scene(), base_hole=hole, obstacles=(wall,))
    with pytest.raises(HarnessError, match='scene_sweep_blocked'):
        joint_transport_goal(value, joint_transport_offsets(service.fixture))
    service.fixture['revalidation']['free_space_goal_tolerance_m'] = .015
    offsets = joint_transport_offsets(service.fixture)
    assert offsets == [[0., 0.], [.015, 0.], [-.015, 0.], [0., .015], [0., -.015]]
    selected = joint_transport_goal(value, offsets)['stages'][0]
    assert selected['goal_region_offset_m'] == [.015, 0.]
    obj = np.asarray(selected['goals'][0]) @ value.eef_object
    np.testing.assert_allclose((np.linalg.inv(hole) @ obj)[:2, 3], [.015, 0.], atol=1e-12)


@pytest.mark.parametrize('failure,attempts', [('scene_sweep_blocked', 5), ('local_observation_space_unknown', 1)])
def test_transport_region_preserves_last_sweep_failure_and_propagates_other_errors(
        scenario, monkeypatch, failure, attempts):
    from unittest.mock import Mock
    from f1_harness import transport
    service, *_ = scenario
    service.fixture['revalidation']['free_space_goal_tolerance_m'] = .015
    offsets = joint_transport_offsets(service.fixture)
    check = Mock(side_effect=[HarnessError(f'{failure}:{i}') for i in range(1, 6)])
    monkeypatch.setattr(transport, '_check_route', check)
    with pytest.raises(HarnessError, match=f'^{failure}:{attempts}$'):
        joint_transport_goal(scene(), offsets)
    assert check.call_count == attempts


def test_fresh_proof_rechecks_fixed_region_endpoint_when_new_scene_clears_center(scenario):
    from f1_harness.revalidation import revalidate_candidate
    service, args, _, original, latest, intent = scenario
    value = scene()
    wall = dict(min=[.468, -.001, .494], max=[.470, .001, .496], role='bucket', name='rim')
    service.fixture.update(joint_space_transport=True, transfer_policy='registered_transport')
    service.fixture['revalidation']['free_space_goal_tolerance_m'] = .015
    intent = intent.model_copy(update={'skill': 'transfer_above_hole'})
    def held_observation(obs):
        block = obs.objects[0].model_copy(update=dict(held=True,
            pose=tuple((value.base_eef @ value.eef_object).ravel()),
            eef_object_pose=tuple(value.eef_object.ravel())))
        return obs.model_copy(update=dict(objects=(block, obs.objects[1])))
    original, latest = map(held_observation, (original, latest))
    initial = replace(value, obstacles=(wall,))
    service._scene = lambda obs, *a, **kw: initial if obs.observation_id == original.observation_id else value
    candidate = service._solve_candidate(intent, initial, args['held16'], args['full22'], 2, joint_search=True)
    entry = candidate['geometry_trace'][0]
    assert entry['goal_region_offset_m'] == [.015, 0.]
    assert joint_transport_goal(value)['stages'][0]['goal_region_offset_m'] == [0., 0.]
    args.update(intent=intent.model_dump(), proposal=candidate,
                original_observation=original.model_dump(), observation=latest.model_dump())
    proof = revalidate_candidate(service, **args)
    assert proof['geometry_checks']['swept_segments'] > 0
    assert proof['geometry_checks']['endpoint_tolerances']['goal_position_m'] == .015
    # A new obstruction at the selected point requires replanning.
    right_wall = dict(min=[.542, -.001, .494], max=[.544, .001, .496], role='bucket', name='rim')
    value = replace(value, obstacles=(right_wall,))
    with pytest.raises(HarnessError, match='revalidation_actual_path_sweep_blocked'):
        revalidate_candidate(service, **args)
    entry['goal_region_offset_m'] = [.04, 0.]
    with pytest.raises(HarnessError, match='revalidation_joint_transport_contract_changed'):
        revalidate_candidate(service, **args)


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

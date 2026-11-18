"""Generic connections preserve model goals, gripper barriers and path proofs."""
from dataclasses import replace
import numpy as np
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.joint_motion import eligible, propose_joint_motion, uses_joint_motion
from f1_harness.owner_bridge import JointProposal
from f1_harness.revalidation import revalidate_candidate
from f1_harness.skills import check_motion_route
from test_full_development_skills import scene, pose
from test_plan_revalidation import scenario


def free_scene():
    return replace(scene(), object_held=False, eef_object=None)


def test_approach_search_keeps_clear_endpoint_without_requiring_straight_connection():
    value = replace(free_scene(), obstacles=({'min': [.08, -.1, .25], 'max': [.12, .1, .6]},))
    result = propose_joint_motion(value, 'approach_grasp')
    opening, motion = result['stages']
    assert opening['goals'] == [] and opening['gripper'] == value.release_opening
    assert motion['gripper'] is None and not motion['allow_target_contact']
    assert motion['route_family'] == 'joint_space_approach'
    goal = np.asarray(motion['goals'][0])
    with pytest.raises(HarnessError, match='scene_sweep_blocked'):
        check_motion_route(value, [goal])
    check_motion_route(value, [goal], start=goal)
    np.testing.assert_allclose(goal[:3, 3], [.2, 0, .38])


@pytest.mark.parametrize('loaded', [False, True])
def test_tcp_search_preserves_target_and_actual_attachment(loaded):
    value = scene() if loaded else free_scene()
    goal = pose(.4, .1, .6).tolist()
    value = replace(value, cartesian_request={'goals': [goal], 'support_contact': False})
    assert eligible({'joint_space_free_motion': True}, 'move_tcp', value)
    proposal = propose_joint_motion(value, 'move_tcp')
    stage = proposal['stages'][0]
    assert stage['goals'] == [goal] and stage['gripper'] is None
    if loaded:
        np.testing.assert_array_equal(stage['eef_object'], value.eef_object)
    else:
        assert stage['eef_object'] is None


@pytest.mark.parametrize('cartesian_request', [
    {'goals': []}, {'goals': [pose().tolist(), pose().tolist()]},
    {'goals': [pose().tolist()], 'support_contact': True},
])
def test_tcp_search_never_discards_via_or_invents_contact_permission(cartesian_request):
    value = replace(free_scene(), cartesian_request=cartesian_request)
    assert not eligible({'joint_space_free_motion': True}, 'move_tcp', value)
    with pytest.raises(HarnessError, match='single_free_space_goal'):
        propose_joint_motion(value, 'move_tcp')


def test_approach_search_cannot_open_a_measured_payload():
    assert not eligible({'joint_space_free_motion': True}, 'approach_grasp', scene())
    with pytest.raises(HarnessError, match='unattached_tool'):
        propose_joint_motion(scene(), 'approach_grasp')


@pytest.mark.parametrize('enabled, budget, endpoint_clear, expected', [
    (True, 30000, True, True), (False, 30000, True, False),
    (True, 10000, True, True), (True, 30000, False, False),
])
def test_free_search_follows_connection_failure_with_same_selected_goal(
        scenario, enabled, budget, endpoint_clear, expected):
    import json
    service, args, clock, original, _, intent = scenario
    clock[0] = original.received_ns
    service.fixture['joint_space_free_motion'] = enabled
    service._scene = lambda *a, **kw: free_scene()
    # An explicit variant chooses a pose, not a restriction to one path topology.
    intent = intent.model_copy(update=dict(budget_ms=budget,
        parameters=intent.parameters.model_copy(update={'variant': 1})))
    attempts = []
    def solve(intent, value, held, full, variant, *, joint_search=False,deadline_ns=None): 
        attempts.append((variant, joint_search))
        if not joint_search:
            raise HarnessError('scene_sweep_blocked:'+json.dumps(dict(diagnostic=dict(
                code='scene_sweep_blocked', metrics=dict(end_pose_clear=endpoint_clear)))))
        return dict(geometry_trace=[dict(route_family='joint_space_approach')])
    service._solve_candidate = solve
    kw = dict(intent=intent.model_dump(), observation=original.model_dump(),
              full22=args['full22'], held16=args['held16'])
    if expected:
        result = service.dispatch('plan_skill', kw)
        assert result['geometry_trace'][0]['preceding_candidate_failures']
        assert attempts == [(1, False), (1, True)]
    else:
        with pytest.raises(HarnessError, match='registered_candidates_exhausted'):
            service.dispatch('plan_skill', kw)
        assert attempts == [(1, False)]


def test_joint_approach_opens_before_search_and_revalidates_only_search_segment(scenario):
    service, args, _, original, latest, intent = scenario
    service.fixture.update(joint_space_free_motion=True, execution_mode='timed_reference',
                           reference_velocity_deg_s=10., free_space_tracking_tolerance_deg=1.)
    held = list(args['held16']); held[15] = 9.
    original = original.model_copy(update={'state16': tuple(held)})
    latest = latest.model_copy(update={'state16': tuple(held)})
    geometry = service._scene(original, args['full22'], held, skill='approach_grasp')
    calls = []
    native = service.numerics.call
    def call(method, **kw):
        if method == 'solve_path':
            calls.append(kw)
            if 'joint_path_to_validate' in kw['scene']:
                return kw['scene']['joint_path_to_validate']
        return native(method, **kw)
    service.numerics.call = call
    candidate = service._solve_candidate(intent, geometry, held, args['full22'], 0, joint_search=True)
    first, search = calls
    assert not first['scene'].get('joint_space_search') and first['goals'] == []
    assert first['gripper'] == geometry.release_opening
    assert search['scene']['joint_space_search'] and search['scene']['free_space_motion']
    assert search['held16'][15] == geometry.release_opening and search['gripper'] is None
    open_end = candidate['geometry_trace'][0]['end_index'] - 1
    assert open_end in candidate['trajectory']['stops']
    assert all(q[15] == geometry.release_opening for q in candidate['targets'])
    np.testing.assert_array_equal(candidate['targets'][0][:15], held[:15])
    args.update(proposal=candidate, held16=held, original_observation=original.model_dump(),
                observation=latest.model_dump())
    proof = revalidate_candidate(service, **args)
    assert proof['geometry_checks']['stages'] == 2
    validation = calls[-1]
    assert validation['scene']['joint_path_to_validate'] == [list(q) for q in candidate['targets'][open_end+1:]]
    assert validation['held16'][15] == geometry.release_opening
    assert uses_joint_motion(service.fixture, intent, JointProposal(**candidate))
    tracked = service.check_waypoint(intent=intent.model_dump(), proposal=candidate,
        observation=original.model_dump(), index=len(candidate['targets'])-1,
        full22=args['full22'], measured16=candidate['targets'][-1])
    assert tracked['accepted']
    with pytest.raises(HarnessError, match='joint_motion_contract_changed'):
        uses_joint_motion({}, intent, JointProposal(**candidate))


def test_joint_approach_rejects_a_numerical_opening_that_moves_arm(scenario):
    service, args, _, original, _, intent = scenario
    service.fixture['joint_space_free_motion'] = True
    native = service.numerics.call
    def moving_open(method, **kw):
        result = native(method, **kw)
        if method == 'solve_path' and not kw['goals']:
            result[0][7] += .1
        return result
    service.numerics.call = moving_open
    geometry = service._scene(original, args['full22'], args['held16'])
    with pytest.raises(HarnessError, match='pregrasp_open_must_hold_arm'):
        service._solve_candidate(intent, geometry, args['held16'], args['full22'], 0, joint_search=True)


def test_requested_tcp_goal_is_reconstructed_for_joint_path_revalidation(scenario):
    from f1_harness.contracts import MotionParameters, digest
    from f1_harness.motion_goals import apply_request
    service, args, _, original, latest, intent = scenario
    service.task = service.task.model_copy(update={'allowed_skills': (*service.task.allowed_skills, 'move_tcp')})
    service.fixture.update(joint_space_free_motion=True, cartesian_motion=True)
    target = pose(.3, .1, .45)
    intent = intent.model_copy(update={'skill': 'move_tcp', 'task_hash': digest(service.task),
        'parameters': MotionParameters(target_tcp_pose=tuple(target.ravel()))})
    native = service.numerics.call
    validation_calls = []
    def call(method, **kw):
        if method == 'solve_path' and 'joint_path_to_validate' in kw['scene']:
            validation_calls.append(kw)
            return kw['scene']['joint_path_to_validate']
        return native(method, **kw)
    service.numerics.call = call
    geometry = apply_request(service, service._scene(original, args['full22'], args['held16']), intent)
    candidate = service._solve_candidate(intent, geometry, args['held16'], args['full22'], 0, joint_search=True)
    args.update(intent=intent.model_dump(), proposal=candidate)
    proof = revalidate_candidate(service, **args)
    assert proof['geometry_checks']['swept_segments'] > 0
    np.testing.assert_allclose(np.asarray(validation_calls[0]['goals'][0]) @ geometry.eef_tip_reference, target)
    assert candidate['geometry_trace'][0]['route_family'] == 'joint_space_tcp'
    # Changing the semantic target cannot reinterpret an already computed path.
    changed = target.copy(); changed[0, 3] += .01
    args['intent'] = intent.model_copy(update={'parameters': MotionParameters(
        target_tcp_pose=tuple(changed.ravel()))}).model_dump()
    with pytest.raises(HarnessError, match='stage_binding_mismatch'):
        revalidate_candidate(service, **args)

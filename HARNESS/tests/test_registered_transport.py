"""Transport must reach the bucket without imposing the insertion orientation."""
from dataclasses import asdict, replace
import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from f1_harness.contracts import HarnessError
from f1_harness.skills import propose
from f1_harness.transport import ROUTE_FAMILIES
from test_full_development_skills import pose, scene


def loaded():
    original = scene()
    attachment = pose(.04, -.02, -.1)
    attachment[:3, :3] = Rotation.from_euler('xy', [25, 12], degrees=True).as_matrix()
    return replace(original, transfer_policy='registered_transport', eef_object=attachment,
                   base_object=original.base_eef @ attachment)


def points(value, variant=0):
    return np.asarray(propose('transfer_above_hole', value, variant=variant)['stages'][0]['goals'])


def template():
    start, middle, end = pose(0, 0, .5), pose(.25, .03, .55), pose(.5, 0, .5)
    end[:3, :3] = Rotation.from_euler('y', 20, degrees=True).as_matrix()
    return dict(schema='f1-success-trajectory-v1', source_run='measured-demo', source_sha256='a' * 64,
                measurement_kind='measured_feedback', segments=dict(transfer=dict(
                    eef_poses=[p.tolist() for p in (start, middle, end)], times_s=[0, 1, 2],
                    arm_deg=[[1] * 7, [2] * 7, [3] * 7])))


def test_coarse_transport_centres_the_held_object_without_aligning_its_orientation():
    value = loaded()
    end = points(value, 1)[-1]
    obj = end @ value.eef_object
    local = np.linalg.inv(value.base_hole) @ obj
    assert np.allclose(local[:2, 3], 0, atol=1e-12)
    assert np.allclose(end[:3, :3], value.base_eef[:3, :3])
    assert not np.allclose(obj[:3, :3], value.base_hole[:3, :3])
    assert not np.allclose(end[:2, 3], value.base_hole[:2, 3])
    corners = np.array([[x, y, z, 1] for x in (-.015, .015) for y in (-.015, .015) for z in (0, .06)])
    assert np.min((corners @ local.T)[:, 2]) >= value.transfer_clearance_m
    assert all(np.allclose(p[:3, :3], value.base_eef[:3, :3]) for p in points(value, 1))


def test_future_insertion_obstruction_does_not_reject_transport_or_alignment():
    from f1_harness.transport import rank_alignment_feeds
    obstruction = dict(min=[.42, -.2, .25], max=[.65, .2, .34], role='bucket', name='closed_aperture')
    value = replace(scene(), transfer_policy='registered_transport', obstacles=(obstruction,))
    end = points(value, 1)[-1]
    value = replace(value, base_eef=end, base_object=end @ value.eef_object)
    order, rows = rank_alignment_feeds(value, [1, 0, 3, 2], [])
    assert order == [1, 0, 3, 2]
    for row in rows:
        feed = row['insertion_preview']
        assert feed['diagnostic']['metrics']['route_family'] == 'aligned_insertion_preview'
        assert feed['requires_fresh_observation'] and not feed['motion_authorized']
    alignment = propose('align', value, variant=order[0])
    aligned = np.array(alignment['stages'][-1]['goals'][-1])
    with pytest.raises(HarnessError, match='no_clear_square_symmetry_route'):
        propose('insert', replace(value, base_eef=aligned, base_object=aligned @ value.eef_object))


def test_feed_priority_preserves_ties_and_current_alignment_collisions(monkeypatch):
    import f1_harness.transport as transport
    value = loaded()
    check = transport._check_route
    def checked(scene, goals, variant, **kwargs):
        if kwargs.get('family') == 'aligned_insertion_preview' and variant in (0, 2):
            raise HarnessError('scene_sweep_blocked:{"diagnostic":{"code":"scene_sweep_blocked","metrics":{}}}')
        return check(scene, goals, variant, **kwargs)
    monkeypatch.setattr(transport, '_check_route', checked)
    order, rows = transport.rank_alignment_feeds(value, [2, 3, 1, 0], [])
    assert order == [3, 1, 2, 0]
    assert [r['insertion_preview']['status'] for r in rows] == ['predicted_clear']*2 + ['predicted_blocked']*2
    center = value.base_object[:3, 3]
    obstacle = dict(min=(center-.005).tolist(), max=(center+.005).tolist(), role='bucket', name='current_blocker')
    with pytest.raises(HarnessError, match='scene_sweep_blocked:') as error:
        propose('align', replace(value, obstacles=(obstacle,)), variant=order[0])
    assert json.loads(str(error.value).split(':', 1)[1])['diagnostic']['metrics']['route_family'] == 'overhead_square_alignment'


def test_align_rotates_in_overhead_space_before_descending_to_the_precise_goal():
    value = loaded()
    above = points(value, 1)[-1]
    value = replace(value, base_eef=above, base_object=above @ value.eef_object)
    result = propose('align', value)
    lift, oriented, centred, aligned = map(np.asarray, result['stages'][0]['goals'])
    assert np.allclose((lift @ value.eef_object)[:3, 3], (oriented @ value.eef_object)[:3, 3])
    assert not np.allclose(lift[:3, :3], oriented[:3, :3])
    assert np.allclose(oriented[:3, :3], centred[:3, :3])
    assert np.allclose(centred[:3, :3], aligned[:3, :3])
    endpoint = aligned @ value.eef_object
    assert np.allclose(endpoint[:3, 3], value.base_hole[:3, 3] + value.base_hole[:3, 2] * value.transfer_clearance_m)
    assert np.allclose(endpoint[:3, 2], value.base_hole[:3, 2])
    assert np.dot((centred @ value.eef_object)[:3, 3] - endpoint[:3, 3], value.base_hole[:3, 2]) >= 0


def test_demonstration_is_retargeted_and_transport_has_distinct_route_families():
    value = replace(loaded(), success_trajectory=template())
    results = [propose('transfer_above_hole', value, variant=i) for i in range(4)]
    assert [r['stages'][0]['route_family'] for r in results] == list(ROUTE_FAMILIES)
    demo = results[0]['stages'][0]
    assert demo['trajectory_source'] == 'measured-demo'
    assert demo['posture_references_deg'] == [[1] * 7, [2] * 7, [3] * 7]
    assert np.allclose(np.asarray(demo['goals'])[-1, :3, :3], np.asarray(template()['segments']['transfer']['eef_poses'])[-1, :3, :3])
    assert len(results[1]['stages'][0]['goals']) == 2
    reorient = np.asarray(results[2]['stages'][0]['goals'])
    assert results[2]['stages'][0]['route_family'] == 'outside_bucket_reorientation_transport'
    assert len(reorient) == 18
    assert results[2]['stages'][0]['posture_reference_policy'] == 'measured_stage_start'
    assert results[2]['stages'][0]['orientation_prior_source'] == 'measured-demo'
    assert 'posture_references_deg' not in results[2]['stages'][0]
    original_object = (value.base_eef @ value.eef_object)[:3, 3]
    for goal in reorient[:-2]:
        assert np.allclose((goal @ value.eef_object)[:3, 3], original_object)
    assert len(results[3]['stages'][0]['goals']) == 4
    # Initial lift keeps the measured wrist orientation, even with the previous
    # coordinated-turn tuning present in the deployed fixture.
    changed = points(replace(value, transfer_lift_rotation_fraction=.33))
    assert np.array_equal(changed, points(value))
    assert np.allclose(changed[0, :3, :3], value.base_eef[:3, :3])


def test_reorientation_route_does_not_lift_old_posture_to_whole_rotation_corridor_height():
    current=pose(0,0,.5)
    current[:3,:3]=Rotation.from_euler('x',60,degrees=True).as_matrix()
    attachment=pose(0,-.25,-.06)
    demo=template();demo['segments']['transfer']['eef_poses'][-1]=pose(.5,0,.5).tolist()
    value=replace(scene(),base_eef=current,eef_object=attachment,base_object=current@attachment,
                  transfer_policy='registered_transport',success_trajectory=demo)
    old=points(value,0);alternate=points(value,2)
    assert old[:,2,3].max()-alternate[:,2,3].max()>.15
    object_start=(current@attachment)[:3,3]
    for goal in alternate[:-2]:
        assert np.allclose((goal@attachment)[:3,3],object_start)
    assert np.allclose(alternate[-2][:3,:3],alternate[-1][:3,:3])
    assert np.allclose((alternate[-1]@attachment)[:2,3],value.base_hole[:2,3])
    # A real obstruction at the outside reorientation still rejects the route.
    obstacle=dict(min=(object_start-.005).tolist(),max=(object_start+.005).tolist(),
                  role='bucket',name='outside_fixture')
    with pytest.raises(HarnessError,match='scene_sweep_blocked:') as error:
        points(replace(value,obstacles=(obstacle,)),2)
    detail=json.loads(str(error.value).split(':',1)[1])['diagnostic']['metrics']
    assert detail['route_family']=='outside_bucket_reorientation_transport'
    assert detail['segment_index']==0 and detail['obstacle_name']=='outside_fixture'


@pytest.mark.parametrize('label', ['name', 'fixture_part'])
def test_transit_still_checks_tool_and_payload_and_identifies_the_failed_segment(label):
    obstruction = dict(min=[.23, -.03, .36], max=[.30, .03, .55], role='bucket', **{label: 'wall'})
    value = replace(scene(), transfer_policy='registered_transport', obstacles=(obstruction,))
    with pytest.raises(HarnessError, match='scene_sweep_blocked:') as error:
        propose('transfer_above_hole', value, variant=1)
    detail = json.loads(str(error.value).split(':', 1)[1])['diagnostic']
    assert detail['code'] == 'scene_sweep_blocked'
    assert detail['metrics']['segment_index'] == 1
    assert detail['metrics']['route_family'] == 'orientation_preserving_transport'
    assert detail['metrics']['obstacle_name'] == 'wall'
    assert detail['metrics']['collision_body'] in ('tool', 'attached_object')


def test_missing_measured_attachment_never_admits_loaded_transit():
    for changes in (dict(object_held=False), dict(object_held=None), dict(eef_object=None)):
        with pytest.raises(HarnessError, match='measured_in_hand_transform_required'):
            propose('transfer_above_hole', replace(loaded(), **changes))


def test_pregrasp_lookahead_searches_the_independent_alignment_symmetries(monkeypatch):
    import f1_harness.skills as module
    value = replace(scene(), transfer_policy='registered_transport', object_held=False, eef_object=None)
    grasp = propose('grasp', value)
    actual = module.propose
    attempted = []
    def selective(skill, geometry, *, variant=0, **kwargs):
        if skill == 'align':
            attempted.append(variant)
            if variant != 2:
                raise HarnessError('scene_sweep_blocked:{"diagnostic":{"code":"scene_sweep_blocked","metrics":{}}}')
        return actual(skill, geometry, variant=variant, **kwargs)
    monkeypatch.setattr(module, 'propose', selective)
    report = module.grasp_route_lookahead(value, grasp)
    assert report['transfer_candidate_variants']
    assert attempted[:3] == [0, 1, 2]
    assert report['numerical_feasibility_checked'] is False


def test_plan_service_loads_explicit_policy_and_keeps_legacy_replay_available():
    from f1_harness.contracts import Task, Observation, SceneObject
    from f1_harness.frames import FrameChain
    from f1_harness.skill_services import PlanService
    value = loaded()
    service = object.__new__(PlanService)
    service.task = Task(task_id='t', object_id='block', hole_id='hole', calibration_id='cal', admission_profile='development_coarse')
    service.frames = FrameChain(pose(), pose(), 'native', 'world')
    service.fixture = asdict(value) | dict(native_frame='native')
    obs = Observation(observation_id='o', acquisition_instance='a', received_ns=100, clock_domain='clock', objects=(
        SceneObject(object_id='block', shape='square', pose=tuple(value.base_object.ravel()), frame_id='native',
                    held=True, eef_object_pose=tuple(value.eef_object.ravel())),
        SceneObject(object_id='hole', shape='square', pose=tuple(value.base_hole.ravel()), frame_id='native')))
    assert service._scene(obs, [0.] * 22, [0.] * 16, base_eef=value.base_eef).transfer_policy == 'registered_transport'
    del service.fixture['transfer_policy']
    assert service._scene(obs, [0.] * 22, [0.] * 16, base_eef=value.base_eef).transfer_policy == 'hole_aligned'


@pytest.mark.parametrize('failure,expected_attempts', [
    ('actual_IK_candidate_collision_rejected', [0, 1]),
    ('numerical_transport_disconnected', [0]),
])
def test_only_known_pure_planning_collision_rejections_try_another_route(monkeypatch, failure, expected_attempts):
    from f1_harness.contracts import Task, SkillIntent, digest
    from test_plan_revalidation import observation, service
    clock = [1_000_000_000]
    task = Task(task_id='t', object_id='block', hole_id='hole', calibration_id='coarse', admission_profile='development_coarse')
    planner = service(clock, task)
    planner.fixture.update(transfer_policy='registered_transport', candidate_order={'transfer_above_hole': [0, 1, 2, 3]})
    planner.fixture = json.loads(json.dumps(planner.fixture, default=lambda value: value.tolist()))
    obs = observation(clock[0])
    obj = obs.objects[0].model_copy(update=dict(held=True, eef_object_pose=tuple(scene().eef_object.ravel())))
    obs = obs.model_copy(update={'objects': (obj, obs.objects[1])})
    intent = SkillIntent(request_id='r', session_id='s', owner_instance_id='o', task_id='t', task_hash=digest(task),
                         epoch=1, observation_id=obs.observation_id, scene_version=obs.scene_version,
                         calibration_id='coarse', skill='transfer_above_hole')
    attempts = []
    def solve(intent, scene, held, full, variant,*,deadline_ns=None): 
        attempts.append(variant)
        if variant == 0:
            raise HarnessError(failure + ':' + json.dumps(dict(diagnostic=dict(code=failure, metrics=dict(
                clearance_m=-.001, collision_pair=['arm_R_Link4', 'body_Link5'])))))
        return dict(geometry_trace=[dict(route_family=ROUTE_FAMILIES[variant])])
    monkeypatch.setattr(planner, '_solve_candidate', solve)
    kwargs = dict(intent=intent.model_dump(), observation=obs.model_dump(), full22=[0.] * 22, held16=obs.state16)
    if len(expected_attempts) == 1:
        with pytest.raises(HarnessError, match=failure):
            planner.dispatch('plan_skill', kwargs)
    else:
        result = planner.dispatch('plan_skill', kwargs)
        assert result['geometry_trace'][0]['route_family'] == ROUTE_FAMILIES[1]
        rejected = result['geometry_trace'][0]['candidate_evaluation']['attempts'][0]['diagnostic']
        assert rejected['metrics']['route_family'] == ROUTE_FAMILIES[0]
        assert rejected['metrics']['clearance_m'] == -.001
    assert attempts == expected_attempts

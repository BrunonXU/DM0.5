import numpy as np
import pytest
from f1_harness.contracts import HarnessError
from f1_harness.return_route import propose_return, corners


def fixture():
    current=np.eye(4);current[:3,3]=[0.,0.,.55]
    home=np.eye(4);home[:3,3]=[-.5,-.5,.3]
    hole=np.eye(4);hole[2,3]=.5
    return dict(current=current,home=home,hole=hole,
        obstacles=[dict(role='bucket',min=[-.2,-.2,0.],max=[.2,.2,.5])],
        tool_boxes=[dict(min=[-.025,-.025,-.025],max=[.025,.025,.025])],
        tip=np.eye(4),radius_m=.05,right_axis=[0.,-1.,0.],clearance_m=.02)


def test_right_is_robot_right_and_home_is_only_after_bucket_clearance():
    cfg=fixture();route=propose_return(**cfg)
    assert [s['name'] for s in route['stages']][:2]==['leave_hole','right_of_bucket']
    assert route['stages'][-1]['name']=='ready'
    poses=[np.array(s['goals'][0]) for s in route['stages']]
    assert poses[1][1,3]<-.2-.025-.019
    assert route['evidence']['variant']=='direct_from_right'
    np.testing.assert_array_equal(poses[-1],cfg['home'])


@pytest.mark.parametrize('variant',range(4))
def test_route_follows_moved_bucket_and_checks_neighbour_not_only_destination(variant):
    cfg=fixture();cfg['obstacles'].append(dict(role='neighbour',min=[-.05,-.4,.51],max=[.05,-.15,.65]))
    with pytest.raises(HarnessError,match='return_route_sweep_blocked:right_of_bucket'):
        propose_return(**cfg,variant=variant)
    cfg=fixture();offset=np.array([.2,.3,.1])
    for key in ('current','home','hole'):cfg[key][:3,3]+=offset
    for box in cfg['obstacles']:
        box['min']=(np.array(box['min'])+offset).tolist();box['max']=(np.array(box['max'])+offset).tolist()
    poses=[np.array(s['goals'][0]) for s in propose_return(**cfg,variant=variant)['stages']]
    assert poses[1][1,3]<.3-.2-.025-.019


def test_no_silent_axis_guess_or_omitted_bucket():
    cfg=fixture();cfg['right_axis']=[0,0,1]
    with pytest.raises(HarnessError,match='not_lateral'):propose_return(**cfg)
    cfg=fixture();cfg['obstacles']=[]
    with pytest.raises(HarnessError,match='bucket_geometry'):propose_return(**cfg)


@pytest.mark.parametrize('variant',[1,2,3])
def test_registered_bend_keeps_departure_empty_hand_and_final_ready(variant):
    cfg=fixture();old=propose_return(**cfg);new=propose_return(**cfg,variant=variant)
    assert new['stages'][:2]==old['stages'][:2]
    assert [s['name'] for s in new['stages']]==['leave_hole','right_of_bucket','return_bend','ready']
    np.testing.assert_array_equal(new['stages'][-1]['goals'][0],cfg['home'])
    assert all(s['gripper'] is None and s['eef_object'] is None for s in new['stages'])
    # Block only the new bend. It cannot silently switch to the old route.
    bend=np.array(new['stages'][2]['goals'][0])[:3,3]
    cfg['obstacles'].append(dict(role='neighbour',min=(bend-.005).tolist(),max=(bend+.005).tolist()))
    with pytest.raises(HarnessError,match='return_route_sweep_blocked'):
        propose_return(**cfg,variant=variant)


@pytest.mark.parametrize('variant',[True,-1,4,1.5])
def test_only_registered_return_bends_are_accepted(variant):
    with pytest.raises(HarnessError,match='unregistered_candidate_variant'):
        propose_return(**fixture(),variant=variant)


def test_h54_withdraw_reset_over_real_unix_ipc_serializes_eef_tip_reference(tmp_path):
    """H-54: withdraw_reset must send a JSON-serializable eef_tip_reference over real UnixServer/UnixClient."""
    from f1_harness.contracts import (
        Frame, Observation, Pairing, SceneObject, SkillIntent, Task, digest,
    )
    from f1_harness.ipc import UnixServer
    from f1_harness.skill_services import PlanService

    now = 10_000_000_000
    current_eef = np.eye(4); current_eef[:3, 3] = [0.0, 0.0, 0.55]
    home_eef = np.eye(4); home_eef[:3, 3] = [-0.5, -0.5, 0.30]
    hole_pose = np.eye(4); hole_pose[2, 3] = 0.50
    home_q = [-70.0, -88.0, 90.0, -85.0, 0.0, 0.0, 0.0]

    seen_bounds = []

    def numerics_dispatch(method, kwargs):
        if method == 'forward_pose':
            held = kwargs['held16']
            if np.allclose(held[7:14], home_q):
                return home_eef.tolist()
            return current_eef.tolist()
        if method == 'solve_path':
            seen_bounds.append(kwargs.get('scene'))
            q = list(kwargs['held16'])
            return [q]
        raise HarnessError('unexpected_method:' + method)

    sock = tmp_path / 'numerics.sock'
    server = UnixServer(sock, numerics_dispatch).start()
    try:
        task = Task(
            task_id='t-h54', shape='square', object_id='square-1', hole_id='square-hole-1',
            calibration_id='cal-h54', goal='placed', admission_profile='development_coarse',
            allowed_skills=('approach_grasp', 'grasp', 'verify_grasp', 'inspect_in_hand',
                            'transfer_above_hole', 'align', 'insert', 'release',
                            'verify_placed', 'withdraw_reset'),
        )
        fixture_cfg = dict(
            native_frame='base',
            frame_chain=dict(
                accepted=True, synthetic=False, evidence_ids=['e1'], holdout_hash='0' * 64,
                native_frame='base', ik_world_frame='ik_world',
                native_eef_frame='eef', ik_eef_frame='ik_eef',
                T_ik_world_native=np.eye(4).ravel().tolist(),
                T_ik_eef_native_eef=np.eye(4).ravel().tolist(),
            ),
            approach_clearance_m=0.05, transfer_clearance_m=0.04, insertion_depth_m=0.012,
            attached_radius_m=0.04, effective_clearance_m=0.02, planning_margin_m=0.02,
            grasp_opening=55.0, release_opening=0.0, gripper_tolerance=5.0,
            joint_tolerance_deg=1.0, settle_ms=50,
            eef_tip_reference=np.eye(4).ravel().tolist(),
            tool_local_boxes=[dict(min=[-0.025, -0.025, -0.025], max=[0.025, 0.025, 0.025])],
            fixture_reference_hole=hole_pose.ravel().tolist(),
            obstacles=[dict(role='bucket', pose=np.eye(4).ravel().tolist(),
                            min=[-0.2, -0.2, 0.0], max=[0.2, 0.2, 0.5])],
            return_route=dict(
                schema='f1-empty-tool-return-v1',
                home_right_deg=home_q,
                right_axis_ik_world=[0.0, -1.0, 0.0],
                source='test', source_sha256='0' * 64,
            ),
        )
        service = PlanService(task, fixture_cfg, sock, clock=lambda: now)
        frames = (
            Frame(camera='head', stream='rgb', frame_id='head_color', sequence=1,
                  source_ns=now - 10_000_000, received_ns=now - 5_000_000, clock_domain='test',
                  width=640, height=480, encoding='rgb8', blob_sha256='a' * 64),
            Frame(camera='head', stream='depth', frame_id='head_depth', sequence=1,
                  source_ns=now - 10_000_000, received_ns=now - 5_000_000, clock_domain='test',
                  width=640, height=480, encoding='16UC1', blob_sha256='b' * 64),
        )
        pairings = (Pairing(camera='head', rgb_sequence=1, depth_sequence=1,
                            skew_ns=0, quality='source_time_bounded'),)
        hole_obj = SceneObject(
            object_id='square-hole-1', shape='square', pose=tuple(hole_pose.ravel()),
            frame_id='base', planning_margin_m=0.02, evidence_ids=('a' * 64, 'b' * 64),
            visible=True, stationary=True,
        )
        state16 = (0.0,) * 16
        obs = Observation(
            observation_id='obs-h54', acquisition_instance='acq-1', clock_domain='test',
            received_ns=now, state_received_ns=now,
            state16=state16, state_valid_mask=(True,) * 16,
            frames=frames, pairings=pairings, objects=(hole_obj,),
            stationary=True, calibration_id='cal-h54',
            geometry_scope='coarse_fixture_return', geometry_camera='head',
            scene_version='v-h54',
        )
        intent = SkillIntent(
            request_id='req-h54', session_id='s1', owner_instance_id='o1',
            task_id=task.task_id, task_hash=digest(task),
            observation_id=obs.observation_id, epoch=1,
            scene_version=obs.scene_version, calibration_id=task.calibration_id,
            skill='withdraw_reset', parameters={}, budget_ms=10_000,
        )
        proposal = service.dispatch('plan_skill', dict(
            intent=intent.model_dump(), observation=obs.model_dump(),
            full22=[0.0] * 22, held16=list(state16),
        ))
        assert proposal['request_id'] == intent.request_id
        assert len(proposal['targets']) >= 3
        assert len(seen_bounds) >= 3
        assert all(isinstance(b['eef_tip_reference'], list) for b in seen_bounds)
    finally:
        server.close()

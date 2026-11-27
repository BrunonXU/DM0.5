from types import SimpleNamespace

import numpy as np

from f1_harness.aperture_view import ApertureViewpoint, planner_view_geometry


def pose(x=0, y=0, z=0):
    value = np.eye(4)
    value[:3, 3] = (x, y, z)
    return value


def setup():
    geometry = dict(object_id='block', hole_id='aperture', calibration_id='cal')
    config = dict(frame_chain={'frame': 'test'}, geometry=geometry)
    task = dict(geometry, allowed_skills=['move_tcp', 'reobserve'])
    footprint = dict(min=[-.03,-.03,-.03], max=[.03,.03,.03])
    plan = dict(task=task, fixture=dict(frame_chain=config['frame_chain'], cartesian_motion=True,
        eef_tip_reference=pose().tolist(), object_local_box=footprint, tool_local_boxes=[footprint],
        hole_inner_dimensions_m=[.04, .04], revalidation=dict(free_space_goal_tolerance_m=.015),
        obstacles=[dict(type='halfspace', role='table', normal=[0,0,1])]))
    tool = planner_view_geometry(plan,config)
    tracker = ApertureViewpoint(tool, SimpleNamespace(world_native=pose()), geometry)
    context = dict(owner_instance_id='owner', session_id='session', task_id='task', task_hash='hash',
                   task_generation=1, epoch=1, authorized=True, grasp_contact=dict(request_id='close'))
    return tracker, context, plan, config


def sample(name, x, *, z=0, aperture=False, held=True):
    object_pose = pose(x, 0, z).ravel().tolist()
    obj = SimpleNamespace(object_id='block', visible=True, pose=object_pose, held=held,
                          eef_object_pose=pose().ravel().tolist(), evidence_ids=['wr'])
    hole = SimpleNamespace(object_id='aperture', pose=pose().ravel().tolist(), visible=True, evidence_ids=['hr'])
    return SimpleNamespace(observation_id=name, acquisition_instance='acq', host_boot_id='boot',
                           calibration_id='cal', synthetic=False, objects=[obj,hole] if aperture else [obj],
                           frames=[SimpleNamespace(camera=c,blob_sha256=h) for c,h in [('head','hr'),('right','wr')]])


def test_view_uses_registered_envelopes_and_keeps_height_and_rotation():
    tracker, context, *_ = setup()
    tracker.update(sample('clear', -.4, aperture=True),context,pose(-.4))
    tracker.update(sample('blocked', 0, z=.2),context,pose(z=.2))
    move = tracker.move
    np.testing.assert_allclose(np.reshape(move['target_tcp_pose'], (4,4)), pose(-.05,0,.2))
    assert move['source_observation_id']=='clear' and move['current_observation_id']=='blocked'
    assert move['source_frame_hashes']==['hr']
    assert 'hole_pose' not in move


def test_changed_scope_regrasp_or_lost_attachment_cannot_reuse_view():
    for change in ('epoch','grasp_contact','held','visible','synthetic','unseen_hash','acquisition_instance'):
        tracker,context,*_ = setup()
        source = sample('clear', -.4, aperture=True)
        if change=='visible': source.objects[1].visible=False
        if change=='synthetic': source.synthetic=True
        if change=='unseen_hash': source.objects[1].evidence_ids=['not-in-this-exposure']
        tracker.update(source,context,pose(-.4))
        if change=='epoch':context['epoch']=2
        if change=='grasp_contact':context['grasp_contact']=dict(request_id='another-close')
        if change=='held':tracker.update(sample('released',0,held=False),context,pose())
        current=sample('blocked',0)
        if change=='acquisition_instance': current.acquisition_instance='new'
        tracker.update(current,context,pose())
        assert tracker.move is None, change


def test_view_needs_authorized_skills_and_same_tcp_frame_chain():
    _, _, plan, config = setup()
    plan['task']['allowed_skills'] = ['move_tcp']
    assert planner_view_geometry(plan,config) is None
    plan['task']['allowed_skills'].append('reobserve')
    config['frame_chain'] = {'frame':'different'}
    assert planner_view_geometry(plan,config) is None


def test_measured_oblique_head_can_recover_view_without_descending():
    tracker,context,*_ = setup()
    tracker.update(sample('clear', 0, aperture=True),context,pose())
    tracker.update(sample('blocked',0,z=.2),context,pose(z=.2),head_pose=pose(-1,0,1))
    goal=np.reshape(tracker.move['target_tcp_pose'],(4,4))
    np.testing.assert_allclose(goal,pose(-.05,0,.2))


def test_fallback_without_anchor_moves_perpendicular_toward_negative_y_with_formula_step():
    tracker, context, *_ = setup()
    # Head at (0, 0, 1), block & TCP at (0.7, 0.2, 0.2) -> sightline in table plane is (0.7, 0.2, 0)
    obs = sample('occluded-1', 0.7, z=0.2, aperture=False, held=True)
    obs.objects[0].pose = pose(0.7, 0.2, 0.2).ravel().tolist()
    tracker.update(obs, context, pose(0.7, 0.2, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    move = tracker.move
    assert move is not None
    assert move['target_frame'] == 'world'
    assert move['source_observation_id'] == 'occluded-1'
    assert move['current_observation_id'] == 'occluded-1'
    assert move['source_frame_hashes'] == ['wr']
    assert move['purpose'] == 'noncontact_lateral_aperture_reobservation_without_prior_view'
    goal = np.reshape(move['target_tcp_pose'], (4, 4))
    delta = goal[:3, 3] - np.array([0.7, 0.2, 0.2])
    assert delta[1] < 0.0
    assert abs(delta[2]) < 1e-12
    np.testing.assert_allclose(goal[:3, :3], np.eye(3))
    ray_plane = np.array([0.7, 0.2, 0.0])
    assert abs(np.dot(delta, ray_plane)) < 1e-12
    direction = delta / np.linalg.norm(delta)
    expected_width = (abs(direction[0]) * 0.06 + abs(direction[1]) * 0.06)
    expected_step = (expected_width + 0.04) / 2.0
    np.testing.assert_allclose(np.linalg.norm(delta), expected_step, atol=1e-9)


def test_anchor_takes_precedence_and_behaves_identically():
    tracker, context, *_ = setup()
    tracker.update(sample('clear', -.4, aperture=True), context, pose(-.4), head_pose=pose(-1, 0, 1))
    tracker.update(sample('blocked', 0, z=.2), context, pose(z=.2), head_pose=pose(-1, 0, 1))
    move = tracker.move
    assert move is not None
    assert move['purpose'] == 'noncontact_lateral_aperture_reobservation'
    assert move['source_observation_id'] == 'clear'
    assert move['source_frame_hashes'] == ['hr']


def test_fallback_runs_at_most_once_per_holding_episode():
    tracker, context, *_ = setup()
    obs1 = sample('occ-1', 0.7, z=0.2, aperture=False, held=True)
    tracker.update(obs1, context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is not None
    # Executing the move (action_published=True) consumes the fallback for this holding episode.
    ctx_pub = dict(context, action_published=True)
    tracker.update(sample('occ-pub', 0.7, z=0.2, aperture=False, held=True), ctx_pub,
                   pose(0.7, -0.02, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    # Subsequent occluded frame after move finishes (pass or fail) must NOT generate another fallback.
    tracker.update(sample('occ-2', 0.7, z=0.2, aperture=False, held=True), context,
                   pose(0.7, -0.05, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    # A new holding episode (regrasp) resets fallback eligibility.
    ctx_regrasp = dict(context, grasp_contact=dict(request_id='close-2'))
    tracker.update(sample('occ-3', 0.7, z=0.2, aperture=False, held=True), ctx_regrasp,
                   pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is not None


def test_fallback_requires_head_frame_and_head_pose():
    tracker, context, *_ = setup()
    # Missing head_pose
    tracker.update(sample('occ-no-pose', 0.7, z=0.2), context, pose(0.7, 0.0, 0.2), head_pose=None)
    assert tracker.move is None
    # Missing head frame in scene.frames without retained_static_fixture
    obs_no_head = sample('occ-no-head-frame', 0.7, z=0.2)
    obs_no_head.frames = [f for f in obs_no_head.frames if f.camera != 'head']
    tracker.update(obs_no_head, context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    # H-32 object_only scene omits head from scene.frames while carrying retained_static_fixture + measured head_pose
    obs_retained = sample('occ-retained', 0.7, z=0.2)
    obs_retained.frames = [f for f in obs_retained.frames if f.camera != 'head']
    obs_retained.fixture = SimpleNamespace(source='retained_static_fixture')
    tracker.update(obs_retained, context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is not None
    assert tracker.move['purpose'] == 'noncontact_lateral_aperture_reobservation_without_prior_view'


def test_fallback_requires_held_true_and_valid_pose():
    tracker, context, *_ = setup()
    # held=False
    tracker.update(sample('not-held', 0.7, z=0.2, held=False), context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    # held=None
    tracker.update(sample('unknown-held', 0.7, z=0.2, held=None), context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    # pose=None
    obs_no_pose = sample('no-obj-pose', 0.7, z=0.2, held=True)
    obs_no_pose.objects[0].pose = None
    tracker.update(obs_no_pose, context, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None


def _make_observer_for_aperture_race(context_holder):
    import threading
    from f1_harness.live_scene import LiveSceneObserver
    tracker, context, *_ = setup()
    obs = sample('occ-race', 0.7, z=0.2, aperture=False, held=True)
    obs.objects[0].pose = pose(0.7, 0.2, 0.2).ravel().tolist()
    tracker.update(obs, context, pose(0.7, 0.2, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is not None
    context_holder['ctx'] = dict(context)
    observer = object.__new__(LiveSceneObserver)
    observer.lock = threading.Lock()
    observer.stop = threading.Event()
    observer.latest = None
    observer.aperture_viewpoint = tracker
    observer.raw = SimpleNamespace()
    observer.owner = SimpleNamespace(call=lambda method, **kw: {'perception_context': dict(context_holder['ctx'])})
    return observer, tracker, obs


def test_h44_waits_for_next_frame_after_stage_advance_and_returns_move():
    import threading, time
    holder = {}
    observer, tracker, obs = _make_observer_for_aperture_race(holder)

    def arrive():
        time.sleep(0.3)
        with observer.lock:
            observer.latest = obs
            tracker.update(obs, holder['ctx'], pose(0.7, 0.2, 0.2), head_pose=pose(0.0, 0.0, 1.0))

    t = threading.Thread(target=arrive, daemon=True)
    t.start()
    started = time.monotonic()
    res = observer.aperture_observation()
    elapsed = time.monotonic() - started
    t.join(timeout=2)
    assert 'observation_move' in res
    assert res['observation_move']['purpose'] == 'noncontact_lateral_aperture_reobservation_without_prior_view'
    assert 0.25 <= elapsed < 1.2


def test_h44_times_out_after_1_5s_without_new_frame():
    import time
    holder = {}
    observer, _, _ = _make_observer_for_aperture_race(holder)
    started = time.monotonic()
    res = observer.aperture_observation()
    elapsed = time.monotonic() - started
    assert 'observation_move' not in res
    assert res['observation_move_reason'] == 'no_distinct_measured_view_in_current_holding_episode'
    assert elapsed >= 1.45


def test_h44_scope_changed_returns_no_candidate_without_waiting():
    import time
    holder = {}
    observer, _, _ = _make_observer_for_aperture_race(holder)
    holder['ctx']['epoch'] = 99
    started = time.monotonic()
    res = observer.aperture_observation()
    elapsed = time.monotonic() - started
    assert 'observation_move' not in res
    assert res['observation_move_reason'] == 'no_distinct_measured_view_in_current_holding_episode'
    assert elapsed < 0.3


def test_h44_scope_changed_during_wait_rejects_candidate():
    import threading, time
    holder = {}
    observer, tracker, obs = _make_observer_for_aperture_race(holder)

    def change_and_arrive():
        time.sleep(0.2)
        holder['ctx']['grasp_contact'] = dict(request_id='different-close')
        with observer.lock:
            observer.latest = obs

    t = threading.Thread(target=change_and_arrive, daemon=True)
    t.start()
    res = observer.aperture_observation()
    t.join(timeout=2)
    assert 'observation_move' not in res
    assert res['observation_move_reason'] == 'no_distinct_measured_view_in_current_holding_episode'

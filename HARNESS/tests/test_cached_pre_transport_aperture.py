"""Comprehensive unit tests for H-42 cached pre-transport aperture & H-32 45s/contact-fault update."""
import numpy as np
import pytest

from f1_harness.aperture_view import ApertureViewpoint, planner_view_geometry
from f1_harness.contracts import FixturePose, Observation, SceneObject, Task, digest
from f1_harness.observation import measured_alignment_pair, require_geometry_identity
from f1_harness.revalidation import _measurement_identity, _scene_unchanged
from f1_harness.scene_pipeline import (
    CACHED_APERTURE_MAX_AGE_NS,
    OCCLUDED_FIXTURE_RETAIN_NS,
)
from f1_harness.verification import verify
from test_full_development_scene import full_scene, sample
from test_phase_perception import anchored_fixture


def _eye16(dx=0.0, dy=0.0, dz=0.0):
    m = np.eye(4, dtype=float)
    m[:3, 3] = (dx, dy, dz)
    return tuple(m.ravel().tolist())


def _make_fixture(measured_at_ns, *, source='rgbd_anchor_registration', dx=0.0):
    return FixturePose(
        fixture_id='fixture',
        pose=_eye16(dx=dx),
        reference_pose=_eye16(),
        frame_id='test-base',
        source=source,
        evidence_ids=('head-rgb-0', 'head-depth-0'),
        measured_at_ns=measured_at_ns,
    )


def _setup_pipeline_with_cache(monkeypatch, *, cache_ns=110_000_000_000, grasp_pub_ns=100_000_000_000):
    registered, _, masks = anchored_fixture()
    pipe, obs = full_scene()
    pipe.fixture_reference_camera = np.eye(4)
    pipe.occlusion = registered.occlusion
    pipe.config['relocalize_visible_fixture'] = True
    pipe.segment = lambda rgb, view: {'block': masks['block']} if view == 'right' else {}
    ctx = {
        'owner_instance_id': 'owner-1',
        'session_id': 'sess-1',
        'task_id': 'task-1',
        'task_hash': 'hash-1',
        'task_generation': 1,
        'epoch': 1,
        'authorized': True,
        'phase': 'transfer_above_hole',
        'grasp_contact': {'request_id': 'close-1', 'published_ns': grasp_pub_ns},
    }
    obs0 = sample(obs, 0)
    frames0 = tuple(f.model_copy(update=dict(source_ns=cache_ns, received_ns=cache_ns)) for f in obs0.frames)
    s16 = list(obs0.state16)
    s16[15] = 85.0
    obs0 = obs0.model_copy(update=dict(
        received_ns=cache_ns, state_received_ns=cache_ns, frames=frames0, state16=tuple(s16),
        host_boot_id='boot-1'))
    pipe.acquisition_identity = (
        obs0.acquisition_instance, obs0.clock_domain, obs0.host_boot_id, obs0.calibration_id, obs0.synthetic)
    pipe.context_identity = tuple(
        ctx.get(k) for k in ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'task_generation', 'epoch'))
    pipe.previous_context = dict(ctx)
    pipe.last_received_ns = cache_ns - 100_000_000

    monkeypatch.setattr(
        pipe, '_primary_evidence',
        lambda observation, base_object, eef, stamps, *, camera=None: (np.eye(4), True, True, True),
    )
    monkeypatch.setattr(
        pipe, '_locate_bucket_for_object',
        lambda observation, object_camera, head_view: _make_fixture(observation.received_ns),
    )
    monkeypatch.setattr(
        pipe, '_direct_aperture',
        lambda observation, hole_cfg, hole_depth, hole_masks, hole_evidence, *, placing: (
            np.eye(4), ('head-rgb-cache', 'head-depth-cache'), True, None),
    )
    first = pipe.process(obs0, base_eef=np.eye(4), context=ctx)
    assert pipe.cached_aperture is not None
    assert pipe.cached_aperture['measured_at_ns'] == cache_ns
    assert pipe.cached_aperture['evidence_ids'] == ('head-rgb-cache', 'head-depth-cache')
    return pipe, registered, obs, ctx, first


def _step_obs(obs, *, seq_offset, ts_ns, calibration_id=None, gripper=85.0):
    cur = sample(obs, seq_offset)
    frames = tuple(f.model_copy(update=dict(source_ns=ts_ns, received_ns=ts_ns)) for f in cur.frames)
    s16 = list(cur.state16)
    s16[15] = gripper
    upd = dict(received_ns=ts_ns, state_received_ns=ts_ns, frames=frames, state16=tuple(s16), host_boot_id='boot-1')
    if calibration_id is not None:
        upd['calibration_id'] = calibration_id
    return cur.model_copy(update=upd)


def test_h42_constants_and_scene_object_backward_compatible_hash():
    assert CACHED_APERTURE_MAX_AGE_NS == 45_000_000_000
    assert OCCLUDED_FIXTURE_RETAIN_NS == 45_000_000_000
    plain = SceneObject(object_id='square-hole-1', shape='square', pose=_eye16(), evidence_ids=('a', 'b'))
    dumped = plain.model_dump(mode='json')
    assert 'source' not in dumped and 'measured_at_ns' not in dumped
    # Cached aperture enforces visible=False and measured_at_ns
    with pytest.raises(ValueError, match='cached_pre_transport_aperture'):
        SceneObject(
            object_id='square-hole-1', shape='square', pose=_eye16(), evidence_ids=('a', 'b'),
            visible=True, source='cached_pre_transport_aperture', measured_at_ns=123,
        )
    cached = SceneObject(
        object_id='square-hole-1', shape='square', pose=_eye16(), evidence_ids=('a', 'b'),
        visible=False, source='cached_pre_transport_aperture', measured_at_ns=123,
    )
    assert cached.model_dump(mode='json')['source'] == 'cached_pre_transport_aperture'
    assert cached.model_dump(mode='json')['measured_at_ns'] == 123


def test_h42_valid_cache_used_with_retained_and_measured_fixture_and_allowed_skills(monkeypatch):
    pipe, registered, obs, ctx, _ = _setup_pipeline_with_cache(monkeypatch)
    try:
        # 1) Occluded head -> H-32 retained_static_fixture + H-42 cached_pre_transport_aperture (at +15 s)
        monkeypatch.setattr(
            pipe, '_locate_bucket_for_object',
            lambda *a, **kw: (_ for _ in ()).throw(ValueError('partial_lid_landmarks_unavailable')),
        )
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h1', 'h2'), False, None),
        )
        cur = _step_obs(obs, seq_offset=1, ts_ns=125_000_000_000)
        out = pipe.process(cur, base_eef=np.eye(4), context=ctx)
        hole = next((o for o in out.objects if o.object_id == 'hole'), None)
        assert hole is not None
        assert hole.source == 'cached_pre_transport_aperture'
        assert hole.visible is False
        assert hole.evidence_ids == ('head-rgb-cache', 'head-depth-cache')
        assert hole.measured_at_ns == 110_000_000_000
        assert out.fixture is not None and out.fixture.source == 'retained_static_fixture'
        assert 'aperture_from_pre_transport_cache' in out.quality_warnings
        assert pipe.last_timing['cached_aperture']['status'] == 'used'
        assert pipe.last_timing['cached_aperture']['age_s'] == pytest.approx(15.0)

        # Allowed for align, insert, release; REJECTED for verify_placed!
        for skill in ('align', 'insert', 'release'):
            require_geometry_identity(out, goal='placed', admission_profile='development_coarse',
                                      skill=skill, hole_id='hole')
        with pytest.raises(ValueError, match='aperture_measurement_required:hole:cached_aperture_not_allowed_for_verify_placed'):
            require_geometry_identity(out, goal='placed', admission_profile='development_coarse',
                                      skill='verify_placed', hole_id='hole')

        # Revalidation identity and scene_unchanged accept cached aperture + retained fixture for align/insert/release
        task = Task(task_id='t', object_id='block', hole_id='hole', calibration_id=out.calibration_id,
                    goal='placed', admission_profile='development_coarse')
        assert measured_alignment_pair(out, task) is True
        out2 = out.model_copy(update=dict(
            received_ns=out.received_ns + 50_000_000,
            state_received_ns=out.state_received_ns + 50_000_000,
        ))
        for skill in ('align', 'insert', 'release'):
            _measurement_identity(out, out2, task=task, skill=skill)
            _scene_unchanged(out, out2, task=task, threshold=dict(position_m=0.002, rotation_rad=0.08),
                             skill=skill, resweep=True)

        # verify_placed in verification.py also rejects cached aperture
        placed_obj = out.objects[0].model_copy(update=dict(held=False, inserted=True, inserted_hole_id='hole', stationary=True))
        placed_obs = out.model_copy(update=dict(objects=(placed_obj, hole)))
        ver = verify('verify_placed', task, placed_obs, 'req-vp')
        assert ver.outcome == 'pending_review' and ver.reason == 'matching_hole_evidence_missing'

        # 2) Current frame has measured rgbd_anchor_registration fixture within planning_margin_m -> still uses cache!
        monkeypatch.setattr(
            pipe, '_locate_bucket_for_object',
            lambda observation, object_camera, head_view: _make_fixture(observation.received_ns, dx=0.005),
        )
        cur2 = _step_obs(obs, seq_offset=2, ts_ns=135_000_000_000)
        out_live_fix = pipe.process(cur2, base_eef=np.eye(4), context=ctx)
        hole2 = next((o for o in out_live_fix.objects if o.object_id == 'hole'), None)
        assert hole2 is not None and hole2.source == 'cached_pre_transport_aperture'
        assert out_live_fix.fixture.source == 'rgbd_anchor_registration'

        # 3) Direct head measurement reappears -> ALWAYS preferred over cache & updates cache!
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda observation, hole_cfg, hole_depth, hole_masks, hole_evidence, *, placing: (
                np.eye(4), ('head-rgb-new', 'head-depth-new'), True, None),
        )
        cur3 = _step_obs(obs, seq_offset=3, ts_ns=140_000_000_000)
        out_direct = pipe.process(cur3, base_eef=np.eye(4), context=ctx)
        hole3 = next((o for o in out_direct.objects if o.object_id == 'hole'), None)
        assert hole3 is not None and hole3.visible is True and hole3.source is None
        assert hole3.evidence_ids == ('head-rgb-new', 'head-depth-new')
        assert 'aperture_from_pre_transport_cache' not in out_direct.quality_warnings
        assert pipe.cached_aperture['measured_at_ns'] == 140_000_000_000
    finally:
        pipe.close()
        registered.close()


@pytest.mark.parametrize('cause', [
    'close_request_changed',
    'motion_stalled',
    'waypoint_tracking_failed',
    'hold_requested',
    'fixture_moved',
    'age_exceeded',
    'calibration_changed',
])
def test_h42_invalidation_conditions_revert_to_aperture_measurement_required(monkeypatch, cause):
    pipe, registered, obs, ctx, _ = _setup_pipeline_with_cache(monkeypatch)
    try:
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h1', 'h2'), False, None),
        )
        ts_ns = 125_000_000_000
        cal_override = None
        if cause == 'close_request_changed':
            ctx = dict(ctx, grasp_contact={'request_id': 'close-2', 'published_ns': 120_000_000_000})
        elif cause == 'motion_stalled':
            ctx = dict(ctx, contact_fault={'reason': 'motion_stalled', 'at_ns': 115_000_000_000})
        elif cause == 'waypoint_tracking_failed':
            ctx = dict(ctx, contact_fault={'reason': 'waypoint_tracking_failed', 'at_ns': 115_000_000_000})
        elif cause == 'hold_requested':
            ctx = dict(ctx, hold_requested=True)
        elif cause == 'fixture_moved':
            # Move measured fixture by 0.03 m > planning_margin_m (0.015 m)
            monkeypatch.setattr(
                pipe, '_locate_bucket_for_object',
                lambda observation, object_camera, head_view: _make_fixture(observation.received_ns, dx=0.03),
            )
        elif cause == 'age_exceeded':
            ts_ns = 110_000_000_000 + CACHED_APERTURE_MAX_AGE_NS + 1
        elif cause == 'calibration_changed':
            cal_override = 'other-calibration'

        cur = _step_obs(obs, seq_offset=1, ts_ns=ts_ns, calibration_id=cal_override)
        if cause == 'calibration_changed':
            # Calibration change also resets acquisition_identity in _process, clearing cache!
            out = pipe.process(cur, base_eef=np.eye(4), context=ctx)
        else:
            out = pipe.process(cur, base_eef=np.eye(4), context=ctx)
        assert pipe.cached_aperture is None
        assert not any(o.object_id == 'hole' for o in out.objects)
        with pytest.raises(ValueError, match='aperture_measurement_required:hole:'):
            require_geometry_identity(out, goal='placed', admission_profile='development_coarse',
                                      skill='align', hole_id='hole')
        if cause in ('motion_stalled', 'waypoint_tracking_failed', 'hold_requested',
                     'fixture_moved', 'age_exceeded', 'close_request_changed'):
            assert pipe.last_timing['cached_aperture']['reason'] == 'cache_invalidated:' + cause
    finally:
        pipe.close()
        registered.close()


@pytest.mark.parametrize('fault_case', ['within_45s', 'past_45s', 'motion_stalled', 'waypoint_tracking_failed', 'hold_requested'])
def test_h32_retention_45s_and_contact_fault_invalidation(monkeypatch, fault_case):
    pipe, registered, obs, ctx, _ = _setup_pipeline_with_cache(monkeypatch)
    try:
        # Clear aperture cache to isolate H-32 fixture retention behavior
        pipe.cached_aperture = None
        monkeypatch.setattr(
            pipe, '_locate_bucket_for_object',
            lambda *a, **kw: (_ for _ in ()).throw(ValueError('partial_lid_landmarks_unavailable')),
        )
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h1', 'h2'), False, None),
        )
        ts_ns = 110_000_000_000 + 40_000_000_000  # 40 s (within 45 s!)
        if fault_case == 'past_45s':
            ts_ns = 110_000_000_000 + OCCLUDED_FIXTURE_RETAIN_NS + 1
        elif fault_case in ('motion_stalled', 'waypoint_tracking_failed'):
            ctx = dict(ctx, contact_fault={'reason': fault_case, 'at_ns': 115_000_000_000})
        elif fault_case == 'hold_requested':
            ctx = dict(ctx, hold_requested=True)
        cur = _step_obs(obs, seq_offset=1, ts_ns=ts_ns)
        out = pipe.process(cur, base_eef=np.eye(4), context=ctx)
        if fault_case == 'within_45s':
            assert out.fixture is not None and out.fixture.source == 'retained_static_fixture'
            assert 'static_fixture_retention_expired' not in out.quality_warnings
        else:
            assert out.fixture is None
            assert pipe.measured_fixture is None
            assert ('static_fixture_retention_expired' in out.quality_warnings) == (fault_case == 'past_45s')
    finally:
        pipe.close()
        registered.close()


def test_h42_h39_sidestep_suppressed_when_cached_aperture_present_and_active_when_absent():
    from types import SimpleNamespace
    from test_held_aperture_view import pose, setup, sample as view_sample
    tracker, ctx, *_ = setup()
    # With cached aperture present (visible=False, source='cached_pre_transport_aperture'): NO sidestep move
    obs_cached = view_sample('cached-1', 0.7, z=0.2, aperture=True, held=True)
    obs_cached.objects[1].visible = False
    obs_cached.objects[1].source = 'cached_pre_transport_aperture'
    tracker.update(obs_cached, ctx, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is None
    assert tracker.anchor is None  # Cached hole does not overwrite anchor either

    # When cache is absent/invalidated (no hole in objects): H-39 sidestep activates as backup
    obs_no_hole = view_sample('occ-backup', 0.7, z=0.2, aperture=False, held=True)
    tracker.update(obs_no_hole, ctx, pose(0.7, 0.0, 0.2), head_pose=pose(0.0, 0.0, 1.0))
    assert tracker.move is not None
    assert tracker.move['purpose'] == 'noncontact_lateral_aperture_reobservation_without_prior_view'


def test_h42_cross_view_3_evidence_cached_aperture_and_revalidation_binding(monkeypatch):
    """Real F1-09 pre-transport aperture measurements often use cross-view (3 evidence IDs)."""
    from f1_harness.contracts import Task
    from f1_harness.revalidation import _check_aperture_frame_binding, _measurement_identity
    pipe, registered, obs, ctx, _ = _setup_pipeline_with_cache(monkeypatch)
    try:
        # Simulate cross-view 3-hash direct aperture measurement while held
        pipe.cached_aperture = None
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h_rgb', 'h_depth', 'left_rgb_prev'), True, None),
        )
        obs_pre = _step_obs(obs, seq_offset=1, ts_ns=115_000_000_000)
        out_pre = pipe.process(obs_pre, base_eef=np.eye(4), context=ctx)
        assert pipe.cached_aperture is not None
        assert len(pipe.cached_aperture['evidence_ids']) == 3
        assert pipe.cached_aperture['evidence_ids'][2] == 'left_rgb_prev'

        # Arrival after transport: aperture occluded & fixture retained
        monkeypatch.setattr(
            pipe, '_locate_bucket_for_object',
            lambda *a, **kw: (_ for _ in ()).throw(ValueError('partial_lid_landmarks_unavailable')),
        )
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h1', 'h2'), False, None),
        )
        obs_arr1 = _step_obs(obs, seq_offset=2, ts_ns=128_000_000_000)
        obs_arr2 = _step_obs(obs, seq_offset=3, ts_ns=130_000_000_000)
        out1 = pipe.process(obs_arr1, base_eef=np.eye(4), context=ctx)
        out2 = pipe.process(obs_arr2, base_eef=np.eye(4), context=ctx)
        hole1 = next(o for o in out1.objects if o.object_id == 'hole')
        assert hole1.source == 'cached_pre_transport_aperture'
        assert hole1.visible is False
        assert len(hole1.evidence_ids) == 3

        task = Task(task_id='t1', object_id='cube', hole_id='hole', goal='placed',
                    calibration_id=out1.calibration_id,
                    admission_profile='development_coarse')
        # Must NOT fail _check_aperture_frame_binding or _measurement_identity for align
        _check_aperture_frame_binding(out1, task, 'align', out1.frames)
        _measurement_identity(out1, out2, task=task, skill='align')
        require_geometry_identity(out1, goal='placed', admission_profile='development_coarse',
                                  skill='align', hole_id='hole')
    finally:
        pipe.close()
        registered.close()


def test_h46_inflight_waypoint_polling_false_then_true_does_not_record_contact_fault(monkeypatch, tmp_path):
    """H-46: In-flight arrival poll proof.accepted=False then True must NOT record contact_fault."""
    import concurrent.futures
    from types import SimpleNamespace
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore
    from f1_harness.owner_bridge import OwnerBridge

    now = [100_000_000_000]
    engine = TaskEngine(EvidenceStore(tmp_path / 'h46-poll'), clock=lambda: now[0])
    engine.authorized = True
    engine.epoch = 1
    engine.running = SimpleNamespace(skill='transfer_above_hole', request_id='req-1',
                                     epoch=1, verification_request_id=None)
    engine.deadline_ns = 999_000_000_000

    bridge = object.__new__(OwnerBridge)
    bridge.engine = engine
    bridge.stop_delta_deg = 0.5
    bridge.stop_gripper_delta = 2.0
    bridge.feedback_age_ns = 1_000_000_000
    bridge.waypoint_published_ns = 100_000_000_000
    bridge.trajectory_start_ns = None
    bridge.trajectory_at_boundary = True
    bridge.index = 0
    bridge.current_target = np.zeros(16, dtype=float)
    bridge.arrival_evidence = None
    bridge.arrival_refresh = None
    bridge.arrival_retry_ns = 0
    bridge.waypoint_wait_logged_ns = 0
    bridge.settled_since = None
    bridge.progress_key = None
    bridge.progress_anchor = None
    bridge.progress_changed_ns = 0
    bridge.plan_observation = None
    bridge.plan_held_reference = np.zeros(16, dtype=float)
    bridge.stop_window_ns = 0
    bridge.waypoint_verifier = lambda *a, **kw: {'accepted': False, 'reason': 'tracking_task_space_error'}
    bridge.executor = SimpleNamespace(submit=lambda *a, **kw: None)
    bridge._record_grasp_close = lambda *a, **kw: None

    plan = SimpleNamespace(
        request_id='req-1', epoch=1, trajectory=None, tolerance_deg=0.5,
        gripper_tolerance=2.0, free_space_tracking_tolerance_deg=1.0,
        geometry_trace=(), targets=[np.zeros(16, dtype=float), np.zeros(16, dtype=float)],
        settle_window_ns=0,
    )
    bridge.proposal = plan

    # Step 1: In-flight poll returns accepted=False (arm still 3 deg away)
    now[0] = 100_300_000_000
    bridge.feedback_snapshot = {'full22': [0.0] * 22, 'stamps16': tuple([100_250_000_000] * 16)}
    fut1 = concurrent.futures.Future()
    fut1.set_result({'accepted': False, 'reason': 'tracking_task_space_error'})
    bridge.arrival_future = fut1
    bridge.arrival_check = {'key': ('req-1', 1, 0), 'stamp': 100_200_000_000,
                            'measured': np.array([0.0] * 7 + [5.0] * 7 + [0.0, 0.0])}
    bridge.after_publication(
        publication_id='pub-1',
        measured16=np.array([0.0] * 7 + [3.0] * 7 + [0.0, 0.0]),
        publication_complete=True,
        now_ns=now[0],
    )
    assert engine.last_contact_fault is None

    # Step 2: Arm arrives, poll returns accepted=True
    now[0] = 100_600_000_000
    bridge.feedback_snapshot = {'full22': [0.0] * 22, 'stamps16': tuple([100_550_000_000] * 16)}
    fut2 = concurrent.futures.Future()
    fut2.set_result({'accepted': True, 'reason': 'measured_free_space_tracking'})
    bridge.arrival_future = fut2
    bridge.arrival_check = {'key': ('req-1', 1, 0), 'stamp': 100_500_000_000,
                            'measured': np.array([0.0] * 7 + [0.2] * 7 + [0.0, 0.0])}
    bridge.after_publication(
        publication_id='pub-2',
        measured16=np.array([0.0] * 7 + [0.2] * 7 + [0.0, 0.0]),
        publication_complete=True,
        now_ns=now[0],
    )
    assert engine.last_contact_fault is None

    # Verify cached aperture and retained fixture both remain valid with engine's perception_context
    pipe, registered, obs, ctx, _ = _setup_pipeline_with_cache(monkeypatch, cache_ns=110_000_000_000)
    try:
        assert 'contact_fault' not in engine.status()['perception_context']
        monkeypatch.setattr(
            pipe, '_locate_bucket_for_object',
            lambda *a, **kw: (_ for _ in ()).throw(ValueError('partial_lid_landmarks_unavailable')),
        )
        monkeypatch.setattr(
            pipe, '_direct_aperture',
            lambda *a, **kw: (np.eye(4), ('h1', 'h2'), False, None),
        )
        out = pipe.process(_step_obs(obs, seq_offset=1, ts_ns=120_000_000_000), base_eef=np.eye(4), context=ctx)
        assert out.fixture is not None and out.fixture.source == 'retained_static_fixture'
        hole = next(o for o in out.objects if o.object_id == 'hole')
        assert hole.source == 'cached_pre_transport_aperture'
    finally:
        pipe.close()
        registered.close()


def test_h46_real_faults_still_record_contact_fault(tmp_path):
    """H-46: motion_stalled, non-success published motion, and revoke still record contact_fault."""
    from f1_harness.contracts import SkillIntent, SkillResult, Task, digest
    from f1_harness.engine import TaskEngine
    from f1_harness.evidence import EvidenceStore

    task = Task(task_id='t1', object_id='cube', hole_id='hole', goal='placed',
                calibration_id='cal-1', admission_profile='development_coarse')

    def _new_engine(subdir, clock_val):
        eng = TaskEngine(EvidenceStore(tmp_path / subdir), clock=lambda: clock_val[0])
        eng.task = task
        eng.authorized = True
        eng.epoch = 1
        eng.session = 'sess-1'
        eng.heartbeat_ns = clock_val[0]
        eng.deadline_ns = clock_val[0] + 60_000_000_000
        return eng

    # 1. motion_stalled
    t1 = [200_000_000_000]
    e1 = _new_engine('stall', t1)
    intent1 = SkillIntent(
        request_id='r-stall', session_id='sess-1', owner_instance_id=e1.instance,
        task_id=task.task_id, task_hash=digest(task), observation_id='obs:1',
        epoch=1, scene_version='sv1', calibration_id=task.calibration_id,
        skill='transfer_above_hole', parameters={}, budget_ms=25000,
    )
    e1.running = intent1
    e1.record_skill_publication('r-stall', publication_id='p1', now_ns=t1[0])
    e1.complete_stalled_motion(index=3, measured16=[0.0] * 16, now_ns=t1[0])
    assert e1.last_contact_fault == {'reason': 'motion_stalled', 'at_ns': t1[0]}

    # 2. non-success terminal published motion
    t2 = [300_000_000_000]
    e2 = _new_engine('nonsuccess', t2)
    intent2 = intent1.model_copy(update={'request_id': 'r-fail', 'owner_instance_id': e2.instance})
    e2.running = intent2
    e2.record_skill_publication('r-fail', publication_id='p2', now_ns=t2[0])
    e2.complete('r-fail', SkillResult(request_id='r-fail', lifecycle='completed',
                                      outcome='failure', reason='execution_timeout'))
    assert e2.last_contact_fault == {'reason': 'execution_timeout', 'at_ns': t2[0]}

    # 3. authority revocation
    t3 = [400_000_000_000]
    e3 = _new_engine('revoke', t3)
    e3.revoke('web_stop_requested')
    assert e3.last_contact_fault == {'reason': 'authority_revoked', 'at_ns': t3[0]}


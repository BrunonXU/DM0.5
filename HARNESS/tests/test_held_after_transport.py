"""Unit tests for H-40 (Conditions A & B): in-hand association reference retention
across transient failures and H-32 occluded static fixture retention in _partial_observation.
"""
import numpy as np
import pytest

from f1_harness.contracts import FixturePose, MeasuredGripperFeedback
from f1_harness.scene_pipeline import OCCLUDED_FIXTURE_RETAIN_NS
from test_full_development_scene import full_scene, sample
from test_gripper_contact import configured, packet
from test_gripper_contact_review import measured_observation
from test_phase_perception import anchored_fixture


def _eye16():
    return tuple(np.eye(4, dtype=float).ravel().tolist())


def _make_fixture_pose(*, measured_at_ns, source='rgbd_anchor_registration'):
    return FixturePose(
        fixture_id='fixture',
        pose=_eye16(),
        reference_pose=_eye16(),
        frame_id='test-base',
        source=source,
        evidence_ids=('head-rgb-1', 'head-depth-1'),
        measured_at_ns=measured_at_ns,
    )


def _establish_held_with_contact(pipeline, raw):
    context = dict(pipeline.previous_context, task_id='task', epoch=1)
    context['grasp_contact'] = dict(context['grasp_contact'])
    eef = np.eye(4)
    # First frame measures the synthetic block pose so expected_eef_object matches its actual grasp region.
    probe = pipeline.process(measured_observation(raw, 5), base_eef=eef, context=context)
    sq_probe = next(o for o in probe.objects if o.object_id == 'block')
    context['grasp_contact']['expected_eef_object'] = list(sq_probe.pose)
    out = pipeline.process(measured_observation(raw, 6), base_eef=eef, context=context)
    return eef, context, out


def test_condition_a_preserves_in_hand_reference_across_transient_failure():
    pipeline, raw = configured()
    try:
        eef, context, out = _establish_held_with_contact(pipeline, raw)
        sq = next(o for o in out.objects if o.object_id == 'block')
        assert sq.held is True
        assert pipeline.contact_attachment is not None
        assert pipeline.in_hand_reference is not None
        saved_ref = pipeline.in_hand_reference.copy()
        saved_grip = pipeline.in_hand_gripper

        # Single mid-transport visual failure while CAN remains blocked/held
        original = pipeline.segment
        pipeline.segment = lambda *a, **k: (_ for _ in ()).throw(ValueError('mask_truncated'))
        with pytest.raises(ValueError, match='mask_truncated'):
            pipeline.process(measured_observation(raw, 7), base_eef=eef, context=context)
        pipeline.segment = original

        assert pipeline.in_hand_quarantine is not None
        np.testing.assert_allclose(pipeline.in_hand_quarantine['reference'], saved_ref)
        assert pipeline.in_hand_quarantine['gripper'] == pytest.approx(saved_grip)

        # Next valid frame uses quarantined reference for surface_association (depth-fitted only)
        out2 = pipeline.process(measured_observation(raw, 8), base_eef=eef, context=context)
        sq2 = next(o for o in out2.objects if o.object_id == 'block')
        assert sq2.held is True
        assert sq2.pose is not None
        assert pipeline.in_hand_reference is not None
        assert pipeline.in_hand_quarantine is None
    finally:
        pipeline.close()


@pytest.mark.parametrize('clear_cause', ['opened', 'can_stopped', 'empty_closed', 'epoch_changed'])
def test_condition_a_clears_reference_on_open_unheld_unknown_or_scope_change(clear_cause):
    pipeline, raw = configured()
    try:
        eef, context, _ = _establish_held_with_contact(pipeline, raw)
        assert pipeline.in_hand_reference is not None

        # First transient failure moves reference into quarantine
        original = pipeline.segment
        pipeline.segment = lambda *a, **k: (_ for _ in ()).throw(ValueError('mask_truncated'))
        with pytest.raises(ValueError, match='mask_truncated'):
            pipeline.process(measured_observation(raw, 7), base_eef=eef, context=context)
        assert pipeline.in_hand_quarantine is not None

        # Now apply a clearing event during another visual failure: reference MUST clear!
        obs8 = measured_observation(raw, 8)
        ctx8 = dict(context)
        if clear_cause == 'opened':
            obs8 = measured_observation(raw, 8, opened_at=(800_000_000,))
        elif clear_cause == 'can_stopped':
            mg = obs8.measured_gripper.model_copy(update=dict(running=False))
            obs8 = obs8.model_copy(update=dict(measured_gripper=mg))
        elif clear_cause == 'empty_closed':
            mg = MeasuredGripperFeedback.model_validate(packet(raw=0, end=800_000_000))
            obs8 = obs8.model_copy(update=dict(measured_gripper=mg))
        elif clear_cause == 'epoch_changed':
            ctx8['epoch'] = 2

        with pytest.raises(ValueError, match='mask_truncated'):
            pipeline.process(obs8, base_eef=eef, context=ctx8)
        pipeline.segment = original
        assert pipeline.in_hand_reference is None
        assert pipeline.in_hand_quarantine is None
    finally:
        pipeline.close()


def _run_partial_fixture_case(
    monkeypatch,
    *,
    locate_error='partial_lid_landmarks_unavailable',
    held_value=True,
    prior_source='rgbd_anchor_registration',
    prior_measured_ns=110_000_000_000,
    grasp_pub_ns=100_000_000_000,
    obs_received_ns=120_000_000_000,
    has_prior=True,
):
    registered, _, masks = anchored_fixture()
    pipe, obs = full_scene()
    try:
        pipe.fixture_reference_camera = np.eye(4)
        pipe.occlusion = registered.occlusion
        pipe.config['relocalize_visible_fixture'] = True
        pipe.config['measured_gripper_contact'] = True
        pipe.config['measured_gripper_max_age_ns'] = 200_000_000
        pipe.config['search_object_box'] = dict(min=[-0.02, -0.02, 0.0], max=[0.02, 0.02, 0.06])
        pipe.config['surface_selection'] = dict(
            normal_world=[0.0, 0.0, 1.0],
            normal_tolerance_deg=15.0,
            plane_threshold_m=0.004,
            min_inliers=20,
        )
        pipe.segment = lambda rgb, view: (
            {'block': masks['block']} if view == 'right' else {}
        )
        pipe.measured_fixture = (
            _make_fixture_pose(measured_at_ns=prior_measured_ns, source=prior_source)
            if has_prior
            else None
        )

        ctx = {
            'owner_instance_id': 'owner-1',
            'session_id': 'sess-1',
            'task_id': 'task-1',
            'task_hash': 'hash-1',
            'task_generation': 1,
            'epoch': 1,
            'authorized': True,
            'phase': 'align',
            'grasp_contact': {'request_id': 'close-1', 'object_id': 'block',
                              'published_ns': grasp_pub_ns, 'target_closure': 100.0,
                              'expected_eef_object': _eye16()} if grasp_pub_ns is not None else {},
        }
        cur_obs = sample(obs, 0)
        frames = tuple(
            f.model_copy(update=dict(source_ns=obs_received_ns, received_ns=obs_received_ns))
            for f in cur_obs.frames
        )
        mg_raw = 70 if held_value is True else 255
        mg = MeasuredGripperFeedback.model_validate(packet(raw=mg_raw, end=obs_received_ns))
        if held_value is None:
            mg = mg.model_copy(update=dict(running=False))
        cur_obs = cur_obs.model_copy(
            update=dict(
                received_ns=obs_received_ns,
                state_received_ns=obs_received_ns,
                host_boot_id='boot',
                frames=frames,
                measured_gripper=mg,
            )
        )
        pipe.acquisition_identity = (
            cur_obs.acquisition_instance,
            cur_obs.clock_domain,
            cur_obs.host_boot_id,
            cur_obs.calibration_id,
            cur_obs.synthetic,
        )
        pipe.context_identity = tuple(
            ctx.get(k) for k in ('owner_instance_id', 'session_id', 'task_id', 'task_hash', 'task_generation', 'epoch')
        )
        pipe.previous_context = dict(ctx)
        pipe.last_received_ns = obs_received_ns - 100_000_000

        # Force confirmed tracking snapshot so _partial_observation can build local target
        rf_src_ns = next(f.source_ns for f in cur_obs.frames if f.camera == 'right' and f.stream == 'rgb')
        pipe.segment = type('Seg', (), {
            '__call__': lambda self, rgb, view: {'block': masks['block']} if view == 'right' else {},
            'diagnostic_snapshot': lambda self: {
                'right': {'block': {'tracking': {'identity_status': 'confirmed', 'identity_source': 'sam2', 'source_ns': rf_src_ns}}}
            },
        })()

        # Force recoverable object pose failure so process() enters _partial_observation
        import f1_harness.scene_pipeline as sp_mod
        monkeypatch.setattr(
            sp_mod,
            'object_from_mask',
            lambda *a, **k: (_ for _ in ()).throw(ValueError('supported_upright_face_not_observed')),
        )
        monkeypatch.setattr(
            pipe,
            '_locate_bucket_for_object',
            lambda observation, object_camera, head_view: (_ for _ in ()).throw(ValueError(locate_error)),
        )
        out = pipe.process(cur_obs, base_eef=np.eye(4), context=ctx)
        return pipe, out
    finally:
        pipe.close()
        registered.close()


@pytest.mark.parametrize('reason', ['partial_lid_landmarks_unavailable', 'bucket_landmark_registration_unavailable'])
def test_condition_b_partial_retains_static_fixture_when_all_five_conditions_hold(monkeypatch, reason):
    pipe, out = _run_partial_fixture_case(monkeypatch, locate_error=reason)
    assert out.geometry_scope == 'coarse_local_environment'
    assert out.fixture is not None
    assert out.fixture.source == 'retained_static_fixture'
    assert out.fixture.measured_at_ns == 110_000_000_000
    assert 'static_fixture_prediction_from_prior_measurement' in out.quality_warnings
    assert 'static_fixture_retained_while_held_arm_occludes_head' in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings
    assert pipe.measured_fixture is not None
    assert pipe.measured_fixture.source == 'rgbd_anchor_registration'


@pytest.mark.parametrize('case', ['wrong_reason', 'not_held', 'unknown_held', 'before_grasp', 'already_retained', 'expired'])
def test_condition_b_partial_rejects_retention_when_any_condition_fails(monkeypatch, case):
    kwargs = {}
    if case == 'wrong_reason':
        kwargs['locate_error'] = 'partial_lid_ambiguous'
    elif case == 'not_held':
        kwargs['held_value'] = False
    elif case == 'unknown_held':
        kwargs['held_value'] = None
    elif case == 'before_grasp':
        kwargs['prior_measured_ns'] = 95_000_000_000
    elif case == 'already_retained':
        kwargs['prior_source'] = 'retained_static_fixture'
    elif case == 'expired':
        kwargs['obs_received_ns'] = 110_000_000_000 + OCCLUDED_FIXTURE_RETAIN_NS + 1
    pipe, out = _run_partial_fixture_case(monkeypatch, **kwargs)
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert ('static_fixture_retention_expired' in out.quality_warnings) == (case == 'expired')

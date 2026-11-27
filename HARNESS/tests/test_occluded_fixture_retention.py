"""Unit tests for H-32 occluded static fixture retention in scene_pipeline.py."""
import numpy as np
import pytest

from f1_harness.contracts import FixturePose, MotionParameters, SkillIntent
from f1_harness.observation import require_geometry_identity
from f1_harness.scene_pipeline import (
    OCCLUDED_FIXTURE_RETAIN_NS,
    OCCLUDED_FIXTURE_RETAIN_REASONS,
)
from test_full_development_scene import full_scene, sample
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


def _run_case(
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
        pipe.segment = lambda rgb, view: (
            {'block': masks['block']} if view == 'right' else {}
        )
        pipe.measured_fixture = (
            _make_fixture_pose(measured_at_ns=prior_measured_ns, source=prior_source)
            if has_prior
            else None
        )

        # Pre-seed context identity so _context does not invalidate prior_fixture on first call.
        ctx = {
            'owner_instance_id': 'owner-1',
            'session_id': 'sess-1',
            'task_id': 'task-1',
            'task_hash': 'hash-1',
            'task_generation': 1,
            'epoch': 1,
            'authorized': True,
            'phase': 'align',
            'grasp_contact': {'published_ns': grasp_pub_ns} if grasp_pub_ns is not None else {},
        }
        cur_obs = sample(obs, 0)
        # Align timestamps to obs_received_ns
        frames = tuple(
            f.model_copy(update=dict(source_ns=obs_received_ns, received_ns=obs_received_ns))
            for f in cur_obs.frames
        )
        cur_obs = cur_obs.model_copy(
            update=dict(
                received_ns=obs_received_ns,
                state_received_ns=obs_received_ns,
                frames=frames,
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

        monkeypatch.setattr(
            pipe,
            '_primary_evidence',
            lambda observation, base_object, eef, stamps, *, camera=None: (
                np.eye(4),
                True,
                True,
                held_value,
            ),
        )

        def _fail_locate(observation, object_camera, head_view):
            raise ValueError(locate_error)

        monkeypatch.setattr(pipe, '_locate_bucket_for_object', _fail_locate)
        out = pipe.process(cur_obs, base_eef=np.eye(4), context=ctx)
        return pipe, out
    finally:
        pipe.close()
        registered.close()


def test_allowlist_contains_only_two_exact_occlusion_reasons():
    assert OCCLUDED_FIXTURE_RETAIN_NS == 20_000_000_000
    assert OCCLUDED_FIXTURE_RETAIN_REASONS == frozenset(
        (
            'bucket_landmark_registration_unavailable',
            'partial_lid_landmarks_unavailable',
        )
    )


@pytest.mark.parametrize(
    'reason',
    [
        'bucket_landmark_registration_unavailable',
        'partial_lid_landmarks_unavailable',
    ],
)
def test_retains_static_fixture_when_all_five_conditions_hold(monkeypatch, reason):
    pipe, out = _run_case(monkeypatch, locate_error=reason)
    assert out.fixture is not None
    assert out.fixture.source == 'retained_static_fixture'
    assert out.fixture.measured_at_ns == 110_000_000_000
    assert out.geometry_scope == 'coarse_static_task'
    assert 'static_fixture_retained_while_held_arm_occludes_head' in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings
    # Internal measured_fixture keeps original rgbd_anchor_registration source
    # so subsequent occluded frames within 20s still compare against original measurement time.
    assert pipe.measured_fixture is not None
    assert pipe.measured_fixture.source == 'rgbd_anchor_registration'
    assert pipe.measured_fixture.measured_at_ns == 110_000_000_000


@pytest.mark.parametrize(
    'excluded_reason',
    [
        'ambiguous_bucket_landmark_registration',
        'partial_lid_ambiguous',
        'bucket_landmark_frame_identity_changed',
        'partial_lid_prompted_anchor_disagrees:bucket-triangle',
        'fixture_relocalization_anchor_missing:bucket-triangle',
    ],
)
def test_excluded_reasons_do_not_retain_fixture(monkeypatch, excluded_reason):
    pipe, out = _run_case(monkeypatch, locate_error=excluded_reason)
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings


def test_not_held_does_not_retain(monkeypatch):
    pipe, out = _run_case(monkeypatch, held_value=False)
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings


def test_fixture_measured_before_grasp_closure_does_not_retain(monkeypatch):
    pipe, out = _run_case(
        monkeypatch,
        prior_measured_ns=95_000_000_000,
        grasp_pub_ns=100_000_000_000,
        obs_received_ns=105_000_000_000,
    )
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings


def test_prior_already_retained_source_does_not_chain(monkeypatch):
    pipe, out = _run_case(monkeypatch, prior_source='retained_static_fixture')
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert 'static_fixture_retention_expired' not in out.quality_warnings


def test_over_20s_clears_fixture_and_emits_retention_expired_warning(monkeypatch):
    pipe, out = _run_case(
        monkeypatch,
        prior_measured_ns=110_000_000_000,
        grasp_pub_ns=100_000_000_000,
        obs_received_ns=110_000_000_000 + OCCLUDED_FIXTURE_RETAIN_NS + 1,
    )
    assert out.fixture is None
    assert pipe.measured_fixture is None
    assert 'static_fixture_retained_while_held_arm_occludes_head' not in out.quality_warnings
    assert 'static_fixture_retention_expired' in out.quality_warnings


def test_retained_fixture_allows_move_tcp_but_rejects_align(monkeypatch):
    _, out = _run_case(monkeypatch, locate_error='partial_lid_landmarks_unavailable')
    assert out.fixture is not None
    assert out.fixture.source == 'retained_static_fixture'
    assert all(obj.object_id != 'hole' for obj in out.objects)
    # Formal identity check: retained_static_fixture permits move_tcp sidestep, never align/insert/release.
    require_geometry_identity(
        out,
        goal='placed',
        admission_profile='development_coarse',
        skill='move_tcp',
        hole_id='hole',
    )
    for blocked_skill in ('align', 'insert', 'release'):
        with pytest.raises(ValueError):
            require_geometry_identity(
                out,
                goal='placed',
                admission_profile='development_coarse',
                skill=blocked_skill,
                hole_id='hole',
            )

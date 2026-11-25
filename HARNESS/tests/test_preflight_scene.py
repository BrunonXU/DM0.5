"""Offline unit tests for HARNESS/scripts/preflight_scene.py (no ROS, no hardware)."""
import importlib.util
from pathlib import Path
import pytest


def _load_preflight():
    path = Path(__file__).resolve().parents[1] / 'scripts/preflight_scene.py'
    spec = importlib.util.spec_from_file_location('f1_harness_preflight_scene', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_limits(arm_l_j2_min=-94.7):
    limits = {}
    for name in _load_preflight().DEFAULT_FULL_NAMES:
        limits[name] = [-178.0, 178.0]
    limits['arm_l_j2'] = [arm_l_j2_min, 120.0]
    limits['arm_r_j2'] = [-94.0, 120.0]
    return limits


def _sample_obs(obs_id='obs-1', t_ns=1_000_000_000, *, include_block=True, include_hole=True, include_fixture=True):
    objects = []
    if include_block:
        objects.append({
            'object_id': 'square-1',
            'frame_id': 'world',
            'shape': 'square',
            'visible': True,
            'evidence_ids': ['ev-block'],
            'pose': [1.0, 0.0, 0.0, 0.3, 0.0, 1.0, 0.0, 0.1, 0.0, 0.0, 1.0, 0.7, 0.0, 0.0, 0.0, 1.0],
        })
    if include_hole:
        objects.append({
            'object_id': 'square-hole-1',
            'frame_id': 'world',
            'shape': 'square',
            'visible': True,
            'evidence_ids': ['ev-hole'],
            'pose': [1.0, 0.0, 0.0, 0.4, 0.0, 1.0, 0.0, -0.1, 0.0, 0.0, 1.0, 0.72, 0.0, 0.0, 0.0, 1.0],
        })
    fixture = {
        'source': 'rgbd_anchor_registration',
        'evidence_ids': ['ev-fixture'],
        'pose': [1.0, 0.0, 0.0, 0.4, 0.0, 1.0, 0.0, -0.1, 0.0, 0.0, 1.0, 0.65, 0.0, 0.0, 0.0, 1.0],
    } if include_fixture else {'source': 'none'}
    return {
        'observation_id': obs_id,
        'received_ns': t_ns,
        'synthetic': False,
        'calibration_id': 'coarse-aea05cd220e75f10',
        'geometry_scope': 'full',
        'state_valid_mask': [True] * 14 + [False, False],
        'frames': [
            {'camera': 'head', 'stream': 'rgb', 'sequence': 1, 'source_ns': t_ns, 'received_ns': t_ns, 'blob_sha256': 'a', 'camera_info_sha256': 'b'},
            {'camera': 'head', 'stream': 'depth', 'sequence': 2, 'source_ns': t_ns, 'received_ns': t_ns, 'blob_sha256': 'c', 'camera_info_sha256': 'd'},
            {'camera': 'right', 'stream': 'rgb', 'sequence': 3, 'source_ns': t_ns, 'received_ns': t_ns, 'blob_sha256': 'e', 'camera_info_sha256': 'f'},
            {'camera': 'right', 'stream': 'depth', 'sequence': 4, 'source_ns': t_ns, 'received_ns': t_ns, 'blob_sha256': 'g', 'camera_info_sha256': 'h'},
        ],
        'pairings': [
            {'camera': 'head', 'quality': 'receipt_only', 'rgb_sequence': 1, 'depth_sequence': 2},
            {'camera': 'right', 'quality': 'receipt_only', 'rgb_sequence': 3, 'depth_sequence': 4},
        ],
        'objects': objects,
        'fixture': fixture,
        'faults': [],
        'quality_warnings': [],
    }


def test_parse_joint_limits_detects_numerics_profile_mismatch():
    pf = _load_preflight()
    profile = {'joint_limits_deg': _make_limits(-94.7)}
    numerics = {'spec': {'gate': {'limits': [profile['joint_limits_deg'][n] for n in pf.ARM_NAMES]}}}
    limits, mismatches = pf.parse_joint_limits(numerics, profile)
    assert not mismatches
    assert len(limits) == 22
    assert limits['arm_l_j2'] == [-94.7, 120.0]
    assert limits['arm_r_j2'] == [-94.0, 120.0]

    numerics_stale = {'spec': {'gate': {'limits': [[-94.0, 120.0] if n == 'arm_l_j2' else profile['joint_limits_deg'][n] for n in pf.ARM_NAMES]}}}
    _, mismatches_stale = pf.parse_joint_limits(numerics_stale, profile)
    assert any('numerics_profile_limit_mismatch:arm_l_j2' in m for m in mismatches_stale)


def test_joint_limit_tracker_rebaseline_accepts_resting_left_j2_and_preserves_right_j2():
    pf = _load_preflight()
    resting = [0.0] * 22
    resting[1] = -94.155884  # measured arm_l_j2 after left gripper removal
    resting[8] = -88.5       # right arm j2

    old_tracker = pf.JointLimitTracker(_make_limits(-94.0))
    violations_old = old_tracker.update(resting)
    assert violations_old and 'arm_l_j2' in violations_old[0]
    assert old_tracker.summary()['all_within_limits'] is False

    new_tracker = pf.JointLimitTracker(_make_limits(-94.7))
    assert new_tracker.update(resting) == []
    assert new_tracker.summary()['all_within_limits'] is True

    # Right arm j2 remains strictly bounded at -94.0
    right_violation = list(resting)
    right_violation[8] = -94.05
    violations_r = new_tracker.update(right_violation)
    assert violations_r and 'arm_r_j2' in violations_r[0]
    assert 'arm_r_j2' in new_tracker.summary()['violating_joints']


def test_evaluate_scene_sample_and_preflight_pass_and_fail_reasons():
    pf = _load_preflight()
    scene_cfg = {
        'frame_state_skew_ms': 50,
        'geometry': {
            'calibration_id': 'coarse-aea05cd220e75f10',
            'native_frame': 'world',
            'object_id': 'square-1',
            'hole_id': 'square-hole-1',
        },
    }
    t_ns = 10_000_000_000
    obs = _sample_obs(t_ns=t_ns)
    metrics = pf.evaluate_scene_sample(obs, {t_ns: {'state_ns': t_ns + 5_000_000}}, scene_cfg)
    assert metrics['valid_dual_rgbd_feedback'] is True
    assert metrics['full_geometry'] is True

    # Missing hole must fail geometry
    obs_no_hole = _sample_obs(t_ns=t_ns, include_hole=False)
    metrics_no_hole = pf.evaluate_scene_sample(obs_no_hole, {t_ns: {'state_ns': t_ns}}, scene_cfg)
    assert metrics_no_hole['valid_dual_rgbd_feedback'] is True
    assert metrics_no_hole['full_geometry'] is False
    assert metrics_no_hole['hole_solved'] is False

    tracker = pf.JointLimitTracker(_make_limits(-94.7))
    pose = [0.0] * 22
    pose[1] = -94.156
    tracker.update(pose)

    left_ok = pf.evaluate_left_wrist_window([100.0 + i * 0.5 for i in range(25)], 100.0, 112.0, min_span_s=10.0)
    assert left_ok['passed'] is True

    passing = pf.evaluate_preflight(
        required_seconds=12.0,
        joint_summary=tracker.summary(),
        limit_mismatches=[],
        fk_check={'ok': True},
        contiguous_paired_s=12.2,
        unique_paired_scenes=8,
        rgbd_frame_advancement_s={'head.rgb': 12.1, 'head.depth': 12.1, 'right.rgb': 12.1, 'right.depth': 12.1},
        left_wrist_eval=left_ok,
        geometry_counts={'block': 8, 'fixture': 8, 'hole': 8, 'full': 8},
        geometry_examples=[{'observation_id': 'obs-1'}],
        sam_requests_delta=16,
        readonly_invariants={
            'hardware_command_publishers': 0,
            'denied_ipc': [],
            'final_authorized': False,
            'request_publications': 0,
            'start_sent': False,
        },
    )
    assert passing['passed'] is True
    assert passing['reasons'] == []
    text = pf.format_report({'bundle_id': 'abcdef123456', 'product_version': 'V53', 'rmw': 'rmw_fastrtps_cpp', 'required_contiguous_s': 12.0, **passing})
    assert 'PASS' in text

    # Failing geometry + short left wrist + non-zero command publisher must fail with explicit reasons
    left_bad = pf.evaluate_left_wrist_window([100.0, 100.5], 100.0, 112.0, min_span_s=10.0)
    failing = pf.evaluate_preflight(
        required_seconds=12.0,
        joint_summary=tracker.summary(),
        limit_mismatches=[],
        fk_check={'ok': False, 'error_type': 'ArmLimitRejected', 'error': 'outside limits'},
        contiguous_paired_s=4.0,
        unique_paired_scenes=1,
        rgbd_frame_advancement_s={'head.rgb': 3.5, 'head.depth': 3.5, 'right.rgb': 3.5, 'right.depth': 3.5},
        left_wrist_eval=left_bad,
        geometry_counts={'block': 0, 'fixture': 1, 'hole': 0, 'full': 0},
        geometry_examples=[],
        sam_requests_delta=0,
        readonly_invariants={
            'hardware_command_publishers': 1,
            'denied_ipc': ['submit_request'],
            'final_authorized': False,
            'request_publications': 0,
            'start_sent': False,
        },
    )
    assert failing['passed'] is False
    joined = '\n'.join(failing['reasons'])
    assert 'live_fk_rejected' in joined
    assert 'contiguous_paired_window_short' in joined
    assert 'left_wrist_not_continuous' in joined
    assert '3d_geometry_missing:block' in joined
    assert '3d_geometry_missing:square_hole' in joined
    assert 'unexpected_hardware_command_publisher' in joined
    assert 'unexpected_control_ipc_attempt' in joined

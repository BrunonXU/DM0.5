"""Measured face regressions and independent known-shape yaw checks."""
from collections import deque
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.geometry import fit_block_pose, transform
from f1_harness.grasp_tracking import square_rotation_error
from f1_harness.scene_pipeline import CalibratedScenePipeline


FIXTURE = Path(__file__).parent / 'fixtures/grasp_face_e51d'


def recorded_faces():
    manifest = json.loads((FIXTURE/'manifest.json').read_text())
    camera = transform(manifest['eef_camera'])
    offset = transform(manifest['face_object'])
    result = []
    for record in manifest['records']:
        path = FIXTURE / record['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']
        points = np.load(path, allow_pickle=False)['points']
        result.append(camera @ transform(fit_block_pose(points)['pose']) @ offset)
    return manifest, result


def test_real_stationary_face_sequence_keeps_existing_five_degree_contract():
    manifest, current = recorded_faces()
    old = [transform(record['original_eef_object']) for record in manifest['records']]
    assert max(square_rotation_error(a,b) for a in old for b in old) > math.radians(8.)
    assert max(square_rotation_error(a,b) for a in current for b in current) < math.radians(2.)
    # The estimator changes the uncertain face heading; it must not alter the
    # independently fitted plane normal or invent a translated grasp point.
    for before, after in zip(old, current):
        np.testing.assert_allclose(after[:3, 2], before[:3, 2], atol=2e-7)
        np.testing.assert_allclose(after[:3, 3], before[:3, 3], atol=2e-7)


@pytest.mark.parametrize('yaw', [0., 7., 23., 44., 68., 89.])
@pytest.mark.parametrize('case', ['clean', 'sparse_coplanar_cluster', 'missing_corner'])
def test_independent_rotated_faces_and_sparse_outliers(yaw, case):
    rng = np.random.default_rng(17)
    xy = rng.uniform(-.015, .015, (5000, 2))
    if case == 'missing_corner':
        xy = xy[~((xy[:, 0] > .004) & (xy[:, 1] > .004))]
    if case == 'sparse_coplanar_cluster':
        xy = np.concatenate((xy, rng.normal([.04, .02], [.001, .001], (30, 2))))
    angle = math.radians(yaw)
    rotation = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    points = np.column_stack((xy @ rotation.T, np.full(len(xy), .3)))
    target = np.eye(4)
    target[:2, :2] = rotation @ np.diag([1., -1.])
    target[2, 2] = -1.
    actual = transform(fit_block_pose(points)['pose'])
    assert square_rotation_error(actual, target) < math.radians(2.)


def test_degenerate_face_is_not_a_measured_edge():
    points = np.column_stack((np.linspace(-.01, .01, 40), np.zeros(40), np.full(40, .3)))
    with pytest.raises(ValueError, match='degenerate block edge'):
        fit_block_pose(points)


@pytest.mark.parametrize('following_motion', [True, False])
def test_measured_face_sequence_with_synthetic_closure_and_lift(following_motion):
    """Actual face estimates, synthetic motion: not a replay of missing lift frames."""
    _, poses = recorded_faces()
    pipeline = CalibratedScenePipeline.__new__(CalibratedScenePipeline)
    pipeline.config = dict(camera='right', stationary_window_ns=300_000_000,
        stationary_delta_deg=.05, object_stationary_m=.005, gripper_open_range=[17.5, 18.5],
        gripper_stationary_delta=.5, tracking_window_ns=2_000_000_000,
        tracking_tolerance_m=.005, planning_margin_m=.015, yaw_tolerance_rad=math.radians(5.))
    pipeline.coarse = True
    pipeline.stationary_history, pipeline.tracking_history = deque(), deque()
    pipeline.in_hand_reference = pipeline.in_hand_gripper = None
    pipeline.not_in_hand_reference = None
    pipeline.previous_context = pipeline.contact_attachment = None
    pipeline.last_timing = {}
    def push(index, relative, eef_z, gripper):
        stamp = (index + 1)*100_000_000
        eef = np.eye(4); eef[2, 3] = eef_z
        state = [0.]*16; state[7] = eef_z; state[15] = gripper
        observation = SimpleNamespace(acquisition_instance='synthetic-lift', state16=state,
            state_valid_mask=[True]*16, state_received_ns=stamp, frames=[
                SimpleNamespace(camera='right', stream=stream, received_ns=stamp, source_epoch=0, publisher_id=None)
                for stream in ('rgb','depth')])
        return pipeline._primary_evidence(observation, eef@relative, eef, (stamp,stamp))[-1]
    transient = poses[0].copy(); transient[0, 3] += .03
    assert push(0, transient, 0., 30.) is None
    transient[0, 3] -= .01
    assert push(1, transient, 0., 70.) is None
    for index, pose in enumerate(poses, 2):
        result = push(index, pose, (index-2)*.02 if following_motion else 0., 100.)
    assert result is (True if following_motion else None)
    for index in range(5, 30):
        result = push(index, poses[(index-5)%3], .04 if following_motion else 0., 100.)
    assert result is (True if following_motion else None)
    assert pipeline.config['yaw_tolerance_rad'] == math.radians(5.)

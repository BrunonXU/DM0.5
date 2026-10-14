"""Cross-check the model against recorded native FK, not against its own URDF.

The native matrices were computed by the independent robot SDK for virtual
inputs. Relative rotation angle is invariant under *any constant* base/tool
transforms, so this regression needs neither a fitted frame nor ROS/network.
It proves arm orientation consistency, not absolute geometry or calibration.
"""
import itertools
import json
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.geometry import frames


@pytest.fixture(scope='module')
def native_fk():
    return json.loads((Path(__file__).with_name('fixtures') / 'f1_native_fk.json').read_text())


def rotation_distance_deg(first, second):
    # trace(R_first.T @ R_second), independent of the translation components.
    trace = sum(first[row][col] * second[row][col] for row in range(3) for col in range(3))
    return math.degrees(math.acos(max(-1., min(1., (trace - 1.) / 2.))))


@pytest.mark.parametrize('side', ['left', 'right'])
def test_arm_relative_rotations_match_independent_native_fk(native_fk, side):
    rows = [row for row in native_fk['samples'] if row['side'] == side]
    assert len(rows) == 8
    # Preserve the regression's nondegenerate wrist coverage: a home pose with
    # wrist angles near zero cannot distinguish a wrong sign from a right one.
    for joint in (4, 5, 6):
        values = [row['request']['joint_positions'][joint] for row in rows]
        assert min(values) < -5 and max(values) > 5

    native, rendered = [], []
    for row in rows:
        assert row['response']['success']
        angles = row['request']['joint_positions']
        pose = {f'arm_{side[0]}_j{i + 1}': angle for i, angle in enumerate(angles)}
        rendered.append(frames(pose)[f'arm_{side[0].upper()}_Link7'])
        matrix = row['response']['pose_matrix']
        native.append([matrix[i:i + 4] for i in range(0, 16, 4)])

    # Every pair is checked. Expected rotations come directly from stored
    # service responses, never from DH reconstruction or model-derived fits.
    for first, second in itertools.combinations(range(len(rows)), 2):
        expected = rotation_distance_deg(native[first], native[second])
        actual = rotation_distance_deg(rendered[first], rendered[second])
        assert actual == pytest.approx(expected, abs=.005), (
            f'{side} virtual poses {first}/{second}: model {actual:.6f} deg, '
            f'native FK {expected:.6f} deg'
        )

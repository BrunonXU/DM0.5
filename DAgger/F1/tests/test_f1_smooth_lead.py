"""Regression for constant-input target rollback with real IK and output."""
import importlib.util
import json
from pathlib import Path
import numpy as np
import pytest
pytest.importorskip('pink')
pytest.importorskip('ruckig')
from test_f1_numerical import setup, _intent
from f1dagger.teleop import F1ArmTarget

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/65_f1_validate_smooth_lead.py'
spec = importlib.util.spec_from_file_location('smooth_lead_regression', SCRIPT)
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)


@pytest.mark.parametrize('jitter', [False, True])
def test_captured_pose_constant_input_has_no_periodic_speed_dips(setup, jitter):
    c, _, mapping, urdf, _ = setup
    full = np.asarray(json.loads(SCRIPT.with_suffix('.json').read_text())['full22'])
    report = probe.run_case(c, mapping, urdf, full, jitter=jitter)
    # Output momentum may temporarily move the window away while braking;
    # IK must never add to that existing excess or reset its reference.
    assert report['max_added_excess_lead_rad'] <= 1e-9
    assert report['modes'].get('limit_braking',0) == 0


def test_stalled_publication_has_bounded_lead_without_reference_rollback(setup):
    c, _, mapping, urdf, full = setup
    arm = F1ArmTarget(urdf, mapping, c['ik'])
    command = np.r_[full[:14], 43.,66.]
    arm.begin('right', full, command)
    published = arm.ik_virtual_q.copy()
    for tick in range(500):
        before = arm.ik_virtual_q.copy()
        target = arm.target(_intent('right',(-1.,0.,0.)),full,command,.02)
        assert np.max(np.abs(arm.ik_virtual_q-before)) <= .007+1e-9
        assert np.max(np.abs(arm.ik_virtual_q-published)) <= .03+1e-9
        mapping.check_action(target)
    origin = arm.ik.target_pose.translation.copy()
    for _ in range(20):
        arm.target(_intent('right',(1.,0.,0.)),full,command,.02)
    assert arm.ik.target_pose.translation[0] > origin[0]
    assert arm.side == 'right'

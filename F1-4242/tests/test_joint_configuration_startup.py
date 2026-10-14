"""Configuration drift must stop startup before touching ROS parameter services."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from robot.runtime_contract import verify_runtime_contract
import robot.runtime_contract as runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def robot_profile(monkeypatch):
    profile = json.loads((ROOT / 'configs/f1-09.json').read_text())
    real_read_text = Path.read_text
    def read_text(path, *args, **kwargs):
        if path == Path('/etc/machine-id'):
            return profile['expected_machine_id'] + '\n'
        return real_read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    return profile


@pytest.mark.parametrize('change', ['missing_contract', 'changed_limits', 'changed_group'])
def test_invalid_baseline_stops_before_driver_and_ros(robot_profile, monkeypatch, change):
    profile = copy.deepcopy(robot_profile)
    if change == 'missing_contract':
        del profile['joint_contract']
    elif change == 'changed_limits':
        profile['limits']['head_j2'] = [-28, 28]
        profile['joint_limits_deg']['head_j2'] = [-28, 28]
    else:
        profile['groups']['head'].reverse()
    process = Mock()
    parameters = Mock()
    monkeypatch.setattr(runtime, 'verify_process', process)
    monkeypatch.setattr(runtime, 'read_motor_parameters_isolated', parameters)
    result = verify_runtime_contract(profile)
    assert result['ok'] is False
    assert result['joint_configuration']['ok'] is False
    assert not process.called and not parameters.called
    assert result['control_publishers_created'] is False
    assert result['mode_changes'] is False


def test_consistency_is_not_physical_calibration_or_motion_acceptance(robot_profile, monkeypatch):
    monkeypatch.setattr(runtime, 'verify_process', Mock(return_value={
        'pid': 42, 'process_start_ticks': 20, 'sha256': robot_profile['controller_binary_sha256']}))
    monkeypatch.setattr(runtime, 'read_motor_parameters_isolated', Mock(return_value={
        'values': robot_profile['expected_motor_parameters']}))
    result = verify_runtime_contract(robot_profile)
    assert result['ok'] is True, result
    assert result['joint_configuration']['ok'] is True
    assert result['joint_configuration']['physical_calibration_verified'] is False
    assert result['scope'] == 'startup_contract_only_not_motion_acceptance'

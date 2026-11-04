import json
import copy
from pathlib import Path
import numpy as np
import pytest
from f1dagger.config import DAGGER_ROOT, load_config
from f1dagger.contract import JointMapping, home_target, FULL_NAMES, ARM_NAMES
from f1dagger.robot_io import ControlLock

ROOT = Path(__import__('os').environ.get('F1_INFRA_ROOT', str(DAGGER_ROOT.parent.parent/'F1-4242')))

@pytest.fixture
def mapping():
    return JointMapping(json.loads((ROOT/'configs/model_mapping.json').read_text()),
                        json.loads((ROOT/'configs/f1-09.json').read_text()),
                        ROOT/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf')

def test_configuration_is_explicit_and_pinned():
    cfg, profile = load_config(DAGGER_ROOT/'configs/f1_09_hil.json',infra_root=ROOT,profile_path=ROOT/'configs/f1-09.json')
    assert cfg['ik']['verified'] is False and cfg['policy']['endpoint'] == ''
    assert profile['robot_id'] == 'F1-09'

@pytest.mark.parametrize('index',range(22))
def test_hal_model_roundtrip_and_direction(mapping,index):
    q=np.zeros(22);q[index]=13.5
    model=mapping.to_model(q)
    assert np.sign(model[index]) == (-1 if index in (6,11,12) else 1)
    np.testing.assert_allclose(mapping.to_hardware(model),q,atol=1e-12)

def test_limits_are_remapped_and_wrist_coupling_is_enforced(mapping):
    limits=mapping.model_limits()
    assert np.all(limits[:,0]<limits[:,1])
    np.testing.assert_allclose(limits[6],np.sort(np.deg2rad(-mapping.limits[6])))
    q=np.zeros(16);q[5]=60;q[6]=90
    with pytest.raises(ValueError,match='coupled'):mapping.check_action(q)
    q[:]=0;q[14]=101
    with pytest.raises(ValueError,match='gripper'):mapping.check_action(q)

def test_home_preserves_grippers_and_does_not_include_body(mapping):
    profile=json.loads((ROOT/'configs/f1-09.json').read_text())
    q=np.zeros(16);q[14:]=[43,76]
    home=mapping.check_action(home_target(profile,q))
    assert home.shape==(16,) and list(home[14:])==[43,76]


def test_configured_demo_home_replaces_legacy_arms_without_changing_other_axes(mapping):
    before = (ROOT/'configs/f1-09.json').read_bytes()
    config, profile = load_config(DAGGER_ROOT/'configs/f1_09_hil.json',
                                 infra_root=ROOT, profile_path=ROOT/'configs/f1-09.json')
    submitted = np.r_[np.zeros(14), 43., 76.]
    home = mapping.check_action(home_target(profile, submitted))
    assert profile['f710_home']['name'] == '积木demo-V1'
    assert home[0] == submitted[0] and home[7] == -71.27658081054688
    np.testing.assert_array_equal(home[7:14], [config['f710_home']['joints'][k] for k in ARM_NAMES[7:]])
    np.testing.assert_array_equal(home[:7], submitted[:7])
    np.testing.assert_array_equal(home[14:], submitted[14:])
    np.testing.assert_array_equal(submitted[:14], np.zeros(14))
    assert set(profile['f710_home']['joints']) == set(ARM_NAMES)
    assert (ROOT/'configs/f1-09.json').read_bytes() == before
    config['f710_home']['joints']['arm_r_j1'] = 0
    assert home_target(profile, submitted)[7] == home[7]  # Session owns its snapshot.


@pytest.mark.parametrize('fault', ['missing_joint', 'extra_body', 'wrong_robot', 'wrong_units',
                                  'nan', 'bool', 'null'])
def test_invalid_configured_home_never_falls_back_to_legacy(fault, tmp_path):
    config = json.loads((DAGGER_ROOT/'configs/f1_09_hil.json').read_text())
    preset = config['f710_home']
    if fault == 'missing_joint': preset['joints'].pop('arm_l_j1')
    elif fault == 'extra_body': preset['joints']['body_j1'] = 0
    elif fault == 'wrong_robot': preset['robot_id'] = 'another-robot'
    elif fault == 'wrong_units': preset['units'] = 'radian'
    elif fault == 'nan': preset['joints']['arm_l_j1'] = float('nan')
    elif fault == 'bool': preset['joints']['arm_l_j1'] = True
    else: config['f710_home'] = None
    path = tmp_path/'config.json'; path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='home'):
        load_config(path, infra_root=ROOT, profile_path=ROOT/'configs/f1-09.json')


def test_configured_home_still_enforces_joint_and_coupled_limits(mapping):
    _, profile = load_config(DAGGER_ROOT/'configs/f1_09_hil.json',
                            infra_root=ROOT, profile_path=ROOT/'configs/f1-09.json')
    invalid = copy.deepcopy(profile)
    invalid['f710_home']['joints']['arm_r_j1'] = 999
    with pytest.raises(ValueError, match='outside'):
        mapping.check_action(home_target(invalid, np.zeros(16)))
    invalid = copy.deepcopy(profile)
    invalid['f710_home']['joints'].update(arm_r_j6=60, arm_r_j7=90)
    with pytest.raises(ValueError, match='coupled'):
        mapping.check_action(home_target(invalid, np.zeros(16)))

def test_shared_lock_does_not_overwrite_existing_owner(tmp_path):
    a,b=ControlLock(tmp_path/'control.lock'),ControlLock(tmp_path/'control.lock')
    a.acquire();before=(tmp_path/'control.lock').read_bytes()
    try:
        with pytest.raises(RuntimeError,match='occupied'):b.acquire()
        assert (tmp_path/'control.lock').read_bytes()==before
    finally:a.close()
    b.acquire();b.close()

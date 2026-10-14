"""Cross-file failure injection for the offline 22-axis configuration gate."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from robot.joint_contract import ROOT, main, verify_joint_contract


def profile():
    result = json.loads((ROOT / 'configs/f1-09.json').read_text())
    result['joint_contract'] = 'f1-09-joints.json'
    return result


@pytest.fixture
def package(tmp_path):
    # Ordinary configs are separate writable copies. Only immutable original
    # meshes/URDF use hard links; the byte-corruption test unlinks before writing.
    for relative in ('configs/f1-09-joints.json', 'configs/model_mapping.json', 'robot/model_geometry.json'):
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, dest)
    shutil.copytree(ROOT / 'web/models/f1_wheel', tmp_path / 'web/models/f1_wheel', copy_function=os.link)
    manifest = tmp_path / 'web/models/f1_wheel/manifest.json'
    raw = manifest.read_bytes()
    manifest.unlink()
    manifest.write_bytes(raw)
    return tmp_path


def edit_json(root, relative, change):
    path = root / relative
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def rejected(p, root=None, match=None):
    result = verify_joint_contract(p, root=root)
    assert result['configured'] is True
    assert result['ok'] is False and result['status'] == 'mismatch'
    assert result['errors']
    assert result['physical_calibration_verified'] is False
    if match:
        assert match in result['errors'][0]
    return result


def test_real_package_and_all_22_axes_match_without_motion_claims():
    p = profile()
    original = copy.deepcopy(p)
    result = verify_joint_contract(p)
    assert result['ok'] and result['status'] == 'verified'
    assert result['joint_count'] == 22 and result['verified_asset_count'] == 32
    assert result['unmapped_static_wheel_joint_count'] == 8
    assert result['angular_verification'] == {'left_arm': 'native_fk_only', 'right_arm': 'native_fk_only', 'body': 'unverified', 'head': 'unverified'}
    for field in ('physical_calibration_verified', 'motion_acceptance_verified', 'live_driver_verified', 'control_publishers_created'):
        assert result[field] is False
    assert p == original


@pytest.mark.parametrize('value', [None, ''])
def test_unconfigured_other_robot_and_template_never_look_verified(value):
    p = json.loads((ROOT / 'configs/robot.example.json').read_text())
    p['joint_contract'] = value
    result = verify_joint_contract(p)
    assert result['status'] == 'not_configured'
    assert not result['configured'] and not result['ok']
    assert result['errors'] == [] and not result['physical_calibration_verified']


@pytest.mark.parametrize('name', ['../configs/f1-09-joints.json', '/tmp/f1-09-joints.json', 'robot.example.json', 'F1-09-joints.json'])
def test_contract_path_requires_known_basename(name):
    p = profile()
    p['joint_contract'] = name
    rejected(p, match='白名单')


@pytest.mark.parametrize('change,match', [
    (lambda p: p['limits']['head_j2'].__setitem__(0, -28), 'limits.head_j2'),
    (lambda p: [p[k]['head_j2'].__setitem__(0, -28) for k in ('limits', 'joint_limits_deg')], 'head_j2'),
    (lambda p: p['joint_limits_deg'].pop('body_j5'), '22轴'),
    (lambda p: p['limits']['arm_l_j1'].__setitem__(0, False), '范围格式'),
    (lambda p: p['groups']['body'].reverse(), '分组'),
    (lambda p: p['groups']['head'].__setitem__(0, 'head_j2'), '分组'),
    (lambda p: p.__setitem__('joint_units', 'radians'), '单位'),
    (lambda p: p.__setitem__('controller_binary_sha256', '0' * 64), 'controller_binary_sha256'),
    (lambda p: p.__setitem__('expected_machine_id', '0' * 32), 'machine_id'),
    (lambda p: p.__setitem__('robot_id', 'F1-10'), 'robot_id'),
    (lambda p: p['topics'].__setitem__('joint_command', '/other/command'), '话题'),
    (lambda p: p['default_presets'][1]['joints'].__setitem__('body_j3', 0), '预设joints'),
    (lambda p: p['default_presets'][1]['joints'].pop('head_j2'), '预设joints'),
    (lambda p: p['default_presets'][0].__setitem__('source', 'native_pose_2'), '预设source'),
    (lambda p: p['default_presets'][0].__setitem__('units', 'radians'), '预设units'),
    (lambda p: p['full_home_source'].__setitem__('section', 'pose_1'), 'pose_2来源'),
])
def test_profile_cannot_drift_from_bound_driver_and_preset_contract(change, match):
    p = profile()
    change(p)
    rejected(p, match=match)


@pytest.mark.parametrize('relative,change,match', [
    ('web/models/f1_wheel/manifest.json', lambda x: x['joint_signs'].__setitem__('arm_l_j7', 1), '模型方向'),
    ('web/models/f1_wheel/manifest.json', lambda x: x['joint_map'].pop('body_j3'), '关节映射'),
    ('web/models/f1_wheel/manifest.json', lambda x: x.__setitem__('joint_input_units', 'radians'), '单位'),
    ('web/models/f1_wheel/manifest.json', lambda x: x.__setitem__('angle_mapping_sha256', '0' * 64), 'canonical'),
    ('robot/model_geometry.json', lambda x: x['joint_signs'].__setitem__('arm_r_j5', 1), '几何SHA'),
    ('configs/model_mapping.json', lambda x: x['joint_signs'].__setitem__('arm_l_j7', 1), '映射SHA'),
    ('configs/model_mapping.json', lambda x: x.__setitem__('input_units', 'hal_radians'), '映射SHA'),
    ('configs/model_mapping.json', lambda x: x.__setitem__('offset_deg', 180), '映射SHA'),
])
def test_generated_assets_and_canonical_mapping_must_agree(package, relative, change, match):
    edit_json(package, relative, change)
    rejected(profile(), root=package, match=match)


def test_axis_direction_change_is_detected_even_with_regenerated_geometry_digest(package):
    relative = 'robot/model_geometry.json'
    edit_json(package, relative, lambda x: next(j for j in x['joints'] if j['name'] == 'body_joint3')['axis'].__setitem__(1, 1))
    digest = hashlib.sha256((package / relative).read_bytes()).hexdigest()
    edit_json(package, 'configs/f1-09-joints.json', lambda x: x['model'].__setitem__('geometry_sha256', digest))
    rejected(profile(), root=package, match='body_joint3生成axis')


@pytest.mark.parametrize('relative', ['urdf/f1_wheel_0602_new.urdf', 'meshes/arm_L_Link7.STL'])
def test_original_asset_byte_change_is_rejected(package, relative):
    original = ROOT / 'web/models/f1_wheel' / relative
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    path = package / 'web/models/f1_wheel' / relative
    raw = path.read_bytes()
    path.unlink()  # Detach test hard link before corrupting bytes.
    path.write_bytes(raw + b'\n')
    rejected(profile(), root=package, match='SHA')
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before


def test_manifest_cannot_relabel_modified_mesh_as_original(package):
    relative = 'meshes/arm_R_Link7.STL'
    path = package / 'web/models/f1_wheel' / relative
    raw = path.read_bytes()
    path.unlink()
    path.write_bytes(raw + b'\n')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    def relabel(m):
        entry = next(e for e in m['files'] if e['path'] == relative)
        entry.update(sha256=digest, original_sha256=digest, bytes=path.stat().st_size)
    edit_json(package, 'web/models/f1_wheel/manifest.json', relabel)
    rejected(profile(), root=package, match='模型资产SHA')


def test_cli_returns_failure_for_unconfigured_profile_and_success_for_bound_profile(capsys):
    assert main(['--profile', str(ROOT / 'configs/f1-09.json')]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'verified'
    assert main(['--profile', str(ROOT / 'configs/robot.example.json')]) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'not_configured'


def test_version_and_cosmetic_preset_name_are_not_hardware_contract():
    p = profile()
    p['version'] = 'future-release'
    p['default_presets'][0]['name'] = '双臂复位'
    assert verify_joint_contract(p)['ok']

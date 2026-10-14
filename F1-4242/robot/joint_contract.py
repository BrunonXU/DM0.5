"""Offline, read-only consistency checks for a robot's complete joint contract.

``verify_joint_contract(profile)`` returns a report, never raises for malformed
configuration. ``ok`` means the packaged configuration agrees with its bound
contract, not physical calibration, live driver identity or motion acceptance.
No ROS imports, network calls, subprocesses, writes or robot commands occur.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_CONTRACTS = frozenset({'f1-09-joints.json'})
GROUPS = {
    'left_arm': [f'arm_l_j{i}' for i in range(1, 8)],
    'right_arm': [f'arm_r_j{i}' for i in range(1, 8)],
    'head': [f'head_j{i}' for i in range(1, 4)],
    'body': [f'body_j{i}' for i in range(1, 6)],
}
JOINT_MAP = {
    **{f'arm_{side}_j{i}': f'arm_{side.upper()}_joint{i}' for side in ('l', 'r') for i in range(1, 8)},
    **{f'head_j{i}': f'head_joint{i}' for i in range(1, 4)},
    **{f'body_j{i}': f'body_joint{6-i}' for i in range(1, 6)},
}
UNITS = {'feedback': 'degrees', 'command': 'degrees', 'urdf': 'radians'}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _file(root, relative):
    _require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), '合同文件必须使用工程内相对路径')
    path = (root / relative).resolve(strict=True)
    _require(path.is_relative_to(root) and path.is_file(), '合同文件超出工程边界或不是文件')
    return path


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f'JSON重复字段：{key}')
        result[key] = value
    return result


def _json(path):
    value = json.loads(path.read_text(), object_pairs_hook=_unique_pairs)
    _require(isinstance(value, dict), '合同配置必须为JSON对象')
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _range(value, label):
    _require(isinstance(value, list) and len(value) == 2 and all(_number(x) for x in value)
             and value[0] < value[1], f'{label}范围格式错误')
    return value


def _vector(node, attribute, default):
    value = [float(x) for x in (node.get(attribute, default) if node is not None else default).split()]
    _require(len(value) == 3 and all(_number(x) for x in value), 'URDF坐标格式错误')
    return value


def verify_joint_contract(profile, *, root=None):
    """Return ``not_configured``, ``mismatch`` or ``verified`` with an ``ok`` flag.

    A missing/empty ``joint_contract`` is explicitly unconfigured and not OK.
    The caller decides whether an unconfigured generic robot is allowed to stay
    read-only. A configured mismatch must not be treated as verified. ``root``
    only selects an isolated package root for offline checks/tests; it must not
    come from an HTTP request. Report fields do not assert live driver state.
    """
    report = {'ok': False, 'configured': False, 'status': 'not_configured', 'errors': [],
              'scope': 'packaged_joint_configuration_consistency_only',
              'physical_calibration_verified': False, 'motion_acceptance_verified': False,
              'live_driver_verified': False, 'control_publishers_created': False}
    try:
        _require(isinstance(profile, dict), 'profile必须为对象')
        name = profile.get('joint_contract')
        if name in (None, ''):
            report['reason'] = 'joint_contract not configured'
            return report
        report.update(configured=True, status='mismatch')
        _require(isinstance(name, str) and name in ALLOWED_CONTRACTS, 'joint_contract不在configs白名单中')
        root = Path(root or ROOT).resolve(strict=True)
        contract_path = _file(root, 'configs/' + name)
        contract = _json(contract_path)
        _require(contract.get('schema_version') == 1 and contract.get('robot_id') == 'F1-09', '全身合同版本或机器人身份不匹配')
        for field in ('robot_id', 'id'):
            if field in profile:
                _require(profile[field] == contract['robot_id'], f'{field}与全身合同身份不匹配')
        _require(profile.get('robot_id') == contract['robot_id'], '缺少对应机器人robot_id')
        binding = contract['binding']
        for field in ('expected_machine_id', 'controller_binary_sha256'):
            _require(profile.get(field) == binding[field], f'{field}与全身合同绑定不匹配')
        _require(contract.get('units') == UNITS, '合同角度单位不匹配')
        _require(profile.get('joint_units', 'degrees') == 'degrees', 'profile角度单位不匹配')
        _require(contract.get('groups') == GROUPS and profile.get('groups') == GROUPS, '关节分组/顺序必须覆盖完整22轴')
        rows = contract['joints']
        _require(isinstance(rows, list) and len(rows) == 22, '全身合同必须包含22轴')
        by_name = {row['hal_name']: row for row in rows}
        _require(len(by_name) == 22 and set(by_name) == set(JOINT_MAP), '全身合同关节缺失、重复或未知')
        limits, signs = {}, {}
        for group, names in GROUPS.items():
            for hal in names:
                row = by_name[hal]
                _require(row.get('group') == group and row.get('urdf_name') == JOINT_MAP[hal], f'{hal}关节组或URDF映射不匹配')
                limits[hal] = _range(row['command_range_deg'], hal)
                _require(row.get('command_source') == 'driver_command_range' and row.get('urdf_source') == 'pico_urdf', f'{hal}来源未绑定')
                sign, offset = row.get('model_sign'), row.get('model_offset_deg')
                _require(type(sign) is int and sign in (-1, 1) and _number(offset) and offset == 0, f'{hal}模型方向/零位格式不匹配')
                if sign != 1:
                    signs[hal] = sign
                calibration = row['calibration']
                expected = 'native_fk_verified' if group in ('left_arm', 'right_arm') else 'unverified'
                _require(calibration.get('angular_kinematics') == expected, f'{hal}标定范围声明不匹配')
                _require(all(calibration.get(k) == 'unverified' for k in ('physical_zero', 'physical_geometry', 'physical_hard_limits')), f'{hal}尚无实物标定验收依据')
        _require(signs == {'arm_l_j7': -1, 'arm_r_j5': -1, 'arm_r_j6': -1}, '合同模型方向不是已核验三轴映射')
        _require(contract.get('physical_calibration_verified') is False, '不得把配置核对声明为物理标定')
        for field in ('joint_limits_deg', 'limits'):
            actual = profile.get(field)
            _require(isinstance(actual, dict) and set(actual) == set(limits), f'{field}必须覆盖完整22轴')
            for hal, expected in limits.items():
                _require(_range(actual[hal], field + '.' + hal) == expected, f'{field}.{hal}与绑定命令范围不匹配')
        _require(contract['sources']['driver_command_range']['sha256'] == binding['controller_binary_sha256'], '命令范围来源与驱动SHA不一致')
        for key, value in contract['topics'].items():
            _require(profile.get('topics', {}).get(key) == value, f'{key}话题与全身合同不一致')

        spec = contract['model']
        _require(spec['base'] == 'web/models/f1_wheel' and spec['mapping'] == 'configs/model_mapping.json'
                 and spec['geometry'] == 'robot/model_geometry.json', '模型合同路径不匹配')
        mapping_path = _file(root, spec['mapping'])
        mapping = _json(mapping_path)
        mapping_sha = _sha(mapping_path)
        _require(mapping_sha == spec['mapping_sha256'], 'canonical模型角度映射SHA不匹配')
        _require(mapping.get('schema_version') == 1 and mapping.get('input_units') == 'hal_degrees', '模型输入单位不匹配')
        _require(mapping.get('joint_signs') == signs and type(mapping.get('offset_deg')) in (int, float)
                 and mapping['offset_deg'] == 0, 'canonical模型方向/零位不匹配')
        _require(mapping.get('model_urdf_sha256') == spec['urdf_sha256'], '模型映射绑定了不同URDF')
        manifest = _json(_file(root, spec['base'] + '/manifest.json'))
        geometry_path = _file(root, spec['geometry'])
        geometry = _json(geometry_path)
        _require(_sha(geometry_path) == spec['geometry_sha256'], '生成几何SHA不匹配')
        for label, data in (('manifest', manifest), ('geometry', geometry)):
            _require(data.get('joint_map') == JOINT_MAP, label + '关节映射缺轴或不匹配')
            _require(data.get('joint_signs') == signs, label + '模型方向不匹配')
            _require(data.get('angle_mapping_source') == spec['mapping'] and data.get('angle_mapping_sha256') == mapping_sha,
                     label + '不是当前canonical映射生成物')
        _require(manifest.get('joint_input_units') == 'degrees', 'manifest角度单位不匹配')
        _require(manifest.get('urdf') == spec['urdf'] and geometry.get('urdf_sha256') == spec['urdf_sha256'], '生成物URDF来源不匹配')
        urdf_path = _file(root, spec['base'] + '/' + spec['urdf'])
        _require(_sha(urdf_path) == spec['urdf_sha256'] == contract['sources']['pico_urdf']['sha256'], '原始URDF SHA不匹配，不得改写原始模型')
        tree = ET.parse(urdf_path).getroot()
        urdf_joints = {node.get('name'): node for node in tree.findall('joint')}
        remaining = {key: node for key, node in urdf_joints.items() if key not in JOINT_MAP.values()}
        unmapped = contract['non_hal_model_joints']
        _require(len(urdf_joints) == 30 and len(remaining) == len(unmapped) == 8, 'URDF应为22姿态轴与8个静态轮关节')
        _require({item['urdf_name'] for item in unmapped} == set(remaining), '未映射轮关节清单不一致')
        for item in unmapped:
            node = remaining[item['urdf_name']]
            _require(item.get('type') == node.get('type') == 'revolute' and item.get('hal_mapping') is None
                     and item.get('scope') == 'static_wheel_visual_only', '不得把静态轮关节声明为已映射姿态轴')
        geo_joints = {item['name']: item for item in geometry['joints']}
        _require(len(geo_joints) == len(geometry['joints']) == len(urdf_joints) and set(geo_joints) == set(urdf_joints), '生成几何关节树缺失或重复')
        for joint_name, node in urdf_joints.items():
            item = geo_joints[joint_name]
            _require(item.get('parent') == node.find('parent').get('link') and item.get('child') == node.find('child').get('link'), f'{joint_name}生成父子关系不匹配')
            for key, expected in (('xyz', _vector(node.find('origin'), 'xyz', '0 0 0')),
                                  ('rpy', _vector(node.find('origin'), 'rpy', '0 0 0')),
                                  ('axis', _vector(node.find('axis'), 'xyz', '1 0 0'))):
                _require(item.get(key) == expected, f'{joint_name}生成{key}与原URDF不一致')
        for hal, row in by_name.items():
            node = urdf_joints[row['urdf_name']].find('limit')
            raw = [float(node.get('lower')), float(node.get('upper'))]
            _require(_range(row['urdf_range_rad'], hal + '.urdf') == raw, f'{hal}原始URDF范围被改写')
            _require(len(row['urdf_range_deg']) == 2 and all(_number(v) and math.isclose(v, math.degrees(r), abs_tol=1e-9, rel_tol=0)
                     for v, r in zip(row['urdf_range_deg'], raw)), f'{hal}URDF范围单位转换不匹配')
        entries = {entry['path']: entry for entry in manifest['files']}
        expected_assets = spec['asset_sha256']
        _require(len(entries) == len(manifest['files']) == len(expected_assets) and set(entries) == set(expected_assets), '模型资产清单缺失或重复')
        for relative, digest in expected_assets.items():
            entry = entries[relative]
            asset = _file(root, spec['base'] + '/' + relative)
            _require(_sha(asset) == digest == entry.get('sha256') == entry.get('original_sha256'), f'模型资产SHA不匹配：{relative}')
            _require(asset.stat().st_size == entry.get('bytes'), f'模型资产大小不匹配：{relative}')

        presets = profile.get('default_presets')
        expected_presets = contract['default_presets']
        _require(isinstance(presets, list) and len(presets) == len(expected_presets), '默认预设缺失或重复')
        by_key = {item['key']: item for item in presets}
        _require(len(by_key) == len(presets) and set(by_key) == {item['key'] for item in expected_presets}, '默认预设key不匹配')
        for expected in expected_presets:
            actual = by_key[expected['key']]
            for key in ('source', 'units', 'joints'):
                _require(actual.get(key) == expected[key], f"{expected['key']}预设{key}与来源合同不匹配")
            _require(expected['units'] == 'degree', '预设角度单位不匹配')
            scope = set(JOINT_MAP) if expected['source'] == 'native_pose_2' else set(GROUPS['left_arm'] + GROUPS['right_arm'])
            _require(set(actual['joints']) == scope, '预设范围不是已核验的22轴/14轴')
            for hal, value in actual['joints'].items():
                _require(_number(value) and limits[hal][0] <= value <= limits[hal][1], f'{hal}预设目标超出命令范围')
        _require(profile.get('full_home_source') == contract['full_home_source'], '全身pose_2来源路径/哈希/段落不匹配')
        report.update(ok=True, status='verified', robot_id=contract['robot_id'], contract='configs/' + name,
                      contract_sha256=_sha(contract_path), joint_count=len(rows), driver_binding=binding,
                      urdf_sha256=spec['urdf_sha256'], angle_mapping_sha256=mapping_sha,
                      verified_asset_count=len(entries), unmapped_static_wheel_joint_count=8, gripper_mapping='separate_OmniPicker_not_URDF_fingers', physical_calibration_verified=False,
                      angular_verification={'left_arm': 'native_fk_only', 'right_arm': 'native_fk_only', 'body': 'unverified', 'head': 'unverified'})
    except Exception as exc:
        report.update(ok=False, status='mismatch')
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    return report


def main(argv=None):
    """Print a read-only report; exit 0 only for configured, matching packages."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path, help='Explicit profile JSON path; no connection is opened')
    args = parser.parse_args(argv)
    try:
        report = verify_joint_contract(_json(args.profile))
    except Exception as exc:
        report = {'ok': False, 'configured': False, 'status': 'mismatch',
                  'errors': [f'{type(exc).__name__}: {exc}'], 'physical_calibration_verified': False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Copy our authorized Pico model snapshot, preserving source bytes and hashes.

No network, ROS import or other user's home access. Existing destination files
are only overwritten when explicitly preparing this application's model assets.
Use --check to verify a prepared set without changing files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'integrations/pico_upper'
DEST = ROOT / 'web/models/f1_wheel'
ANGLE_MAPPING = ROOT / 'configs/model_mapping.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    source = SOURCE / 'assets/f1_wheel'
    upstream = json.loads((SOURCE / 'UPSTREAM.json').read_text())
    urdf = source / 'urdf/f1_wheel_0602_new.urdf'
    tree = ET.parse(urdf).getroot()
    paths = {urdf}
    for mesh in tree.findall('.//visual/geometry/mesh'):
        path = (urdf.parent / mesh.attrib['filename']).resolve()
        if not path.is_relative_to(source.resolve()):
            raise ValueError('Mesh escaped model source')
        paths.add(path)
    originals = {f['path']: f for f in upstream['files']}
    entries = []
    for path in sorted(paths):
        relative = path.relative_to(source.resolve()) if path.is_absolute() else path.relative_to(source)
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        original = originals['assets/f1_wheel/' + relative.as_posix()]
        entries.append({'path': relative.as_posix(), 'bytes': target.stat().st_size,
                        'sha256': sha(target), 'source': str(path),
                        'original_source': original['source'],
                        'original_sha256': original['source_sha256']})
    mapping = {f'arm_{side}_j{i}': f'arm_{side.upper()}_joint{i}'
               for side in ('l', 'r') for i in range(1, 8)}
    mapping.update({f'head_j{i}': f'head_joint{i}' for i in range(1, 4)})
    mapping.update({f'body_j{i}': f'body_joint{6-i}' for i in range(1, 6)})
    mapping_source = SOURCE / 'vendor/f1_safety/src/python/f1_safety_config.yaml'
    angle_mapping = json.loads(ANGLE_MAPPING.read_text())
    if angle_mapping['model_urdf_sha256'] != sha(urdf):
        raise ValueError('Angle mapping belongs to a different URDF')
    output = {'model': tree.attrib['name'], 'urdf': 'urdf/' + urdf.name,
              'source_version': upstream['upstream_version'],
              'source_captured_at': upstream['captured_at'],
              'prepared_at': '2026-09-28', 'joint_input_units': 'degrees',
              'joint_map': mapping, 'joint_signs': angle_mapping['joint_signs'],
              'angle_mapping_source': 'configs/model_mapping.json',
              'angle_mapping_sha256': sha(ANGLE_MAPPING),
              'mapping_source': str(mapping_source), 'mapping_source_sha256': sha(mapping_source),
              'license': 'No separate model license found in supplied snapshot; internal authorized reuse.',
              'live_scope': '22 body/head/arm axes; no wheel, gripper or base odometry animation',
              'calibration': angle_mapping['scope'],
              'files': entries}
    (DEST / 'manifest.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    geometry_path = ROOT / 'robot/model_geometry.json'
    geometry = json.loads(geometry_path.read_text())
    geometry.update(joint_signs=angle_mapping['joint_signs'], angle_mapping_source='configs/model_mapping.json', angle_mapping_sha256=sha(ANGLE_MAPPING))
    geometry_path.write_text(json.dumps(geometry, ensure_ascii=False, indent=2) + '\n')
    return output


def check():
    manifest = json.loads((DEST / 'manifest.json').read_text())
    errors = []
    mapping = json.loads(ANGLE_MAPPING.read_text())
    signs = mapping['joint_signs']
    if set(signs) != {'arm_l_j7','arm_r_j5','arm_r_j6'} or any(type(value) is not int or value != -1 for value in signs.values()):
        errors.append('unverified HAL-to-model direction mapping')
    if sha(DEST / manifest['urdf']) != mapping['model_urdf_sha256']:
        errors.append('angle mapping URDF hash mismatch')
    geometry = json.loads((ROOT / 'robot/model_geometry.json').read_text())
    for label, data in (('manifest', manifest), ('geometry', geometry)):
        if data.get('joint_signs') != signs or data.get('angle_mapping_sha256') != sha(ANGLE_MAPPING):
            errors.append(label + ' angle mapping mismatch')
    for entry in manifest['files']:
        path = DEST / entry['path']
        if not path.is_file() or sha(path) != entry['sha256']:
            errors.append(entry['path'])
        if entry['sha256'] != entry['original_sha256']:
            errors.append(entry['path'] + ' differs from upstream snapshot')
    if errors:
        raise SystemExit('Model integrity failure: ' + ', '.join(errors))
    print(f"Verified {len(manifest['files'])} original model files, "
          f"{sum(f['bytes'] for f in manifest['files']):,} bytes, 22 mapped axes")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not args.check:
        prepare()
    check()

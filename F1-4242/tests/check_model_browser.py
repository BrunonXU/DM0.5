#!/usr/bin/env python3
"""Render F1 meshes with the published angle mapping and cross-check FK algebra.

Pure offline test: fixture angles do not connect to ROS or robot HTTP APIs.
Requires installed Chrome and NumPy; no package installation or global changes.
"""
import functools
import html
import http.server
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def independent_fk(pose):
    def rotation(axis, theta):
        axis = np.asarray(axis, dtype=float)
        axis /= np.linalg.norm(axis)
        x, y, z = axis
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        return np.eye(3) * math.cos(theta) + (1 - math.cos(theta)) * np.outer(axis, axis) + math.sin(theta) * skew

    manifest = json.loads((ROOT / 'web/models/f1_wheel/manifest.json').read_text())
    signs = manifest['joint_signs']
    assert signs == {'arm_l_j7': -1, 'arm_r_j5': -1, 'arm_r_j6': -1}
    angles = {manifest['joint_map'][name]: value * signs.get(name, 1) * math.pi / 180 for name, value in pose.items()}
    tree = ET.parse(ROOT / 'web/models/f1_wheel' / manifest['urdf']).getroot()
    transforms = {'base_link': np.eye(4)}
    pending = list(tree.findall('joint'))
    while pending:
        progressed = False
        for joint in list(pending):
            parent = joint.find('parent').attrib['link']
            if parent not in transforms:
                continue
            xyz = [float(v) for v in joint.find('origin').attrib.get('xyz', '0 0 0').split()]
            r, p, y = [float(v) for v in joint.find('origin').attrib.get('rpy', '0 0 0').split()]
            frame = np.eye(4)
            frame[:3, :3] = rotation([0, 0, 1], y) @ rotation([0, 1, 0], p) @ rotation([1, 0, 0], r)
            frame[:3, 3] = xyz
            motion = np.eye(4)
            axis = [float(v) for v in joint.find('axis').attrib['xyz'].split()]
            motion[:3, :3] = rotation(axis, angles.get(joint.attrib['name'], 0))
            transforms[joint.find('child').attrib['link']] = transforms[parent] @ frame @ motion
            pending.remove(joint)
            progressed = True
        if not progressed:
            raise AssertionError('URDF tree has unresolved parents')
    return transforms


def main():
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        raise SystemExit('Chrome required for real WebGL test')
    output = ROOT / 'results/model'
    output.mkdir(parents=True, exist_ok=True)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(ROOT)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    browser_results = {}
    try:
        for name, suffix in [('fixture', ''), ('missing-mapping', '?mapping=missing')]:
            with tempfile.TemporaryDirectory(prefix='f1-4242-model-browser-') as profile:
                result = subprocess.run([
                    chrome, '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                    '--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader',
                    '--no-first-run', '--disable-background-networking',
                    '--user-data-dir=' + profile, '--window-size=1100,1100', '--virtual-time-budget=18000',
                    '--screenshot=' + str(output / (name + '.png')), '--dump-dom',
                    f'http://127.0.0.1:{server.server_port}/tests/model_viewer.html{suffix}',
                ], check=True, capture_output=True, text=True, timeout=75)
            (output / (name + '.html')).write_text(result.stdout)
            (output / (name + '-chrome.log')).write_text(result.stderr)
            match = re.search(r'<pre id="result">([\s\S]*?)</pre>', result.stdout)
            text = html.unescape(match.group(1)) if match else ''
            if not text.startswith('PASS\n'):
                raise AssertionError(text or 'No fixture result: ' + name)
            browser_results[name] = text
    finally:
        server.shutdown()
        server.server_close()
    text = browser_results['fixture']
    matrices = json.loads(text.split('\nMATRICES ', 1)[1])
    manifest = json.loads((ROOT / 'web/models/f1_wheel/manifest.json').read_text())
    pose = dict.fromkeys(manifest['joint_map'], 0)
    pose.update(arm_l_j1=90, arm_l_j2=-90, arm_l_j3=-90, arm_l_j4=-90,
                arm_r_j1=-90, arm_r_j2=-90, arm_r_j3=90, arm_r_j4=-90,
                arm_l_j5=20, arm_l_j6=-25, arm_l_j7=35,
                arm_r_j5=-30, arm_r_j6=22, arm_r_j7=-15,
                body_j3=-19, body_j4=-24, body_j5=-5, head_j2=-27)
    target_pose = {**pose, 'arm_l_j1': 95, 'arm_l_j7': -21, 'arm_r_j5': 26, 'arm_r_j6': -18}
    residual = {}
    for label, values in [('actual', pose), ('target', target_pose)]:
        expected = independent_fk(values)
        residual[label] = {}
        for name, raw in matrices[label].items():
            observed = np.asarray(raw).reshape((4, 4), order='F')
            residual[label][name] = float(np.max(np.abs(expected[name] - observed)))
            assert residual[label][name] < 1e-9, (label, name, residual[label][name])
    report = {'status': 'pass', 'browser_assertions': text.split('\nMATRICES ', 1)[0].splitlines()[1:],
              'independent_fk_max_abs_error': residual, 'physical_motion_tested': False,
              'scope': 'published mapping application and URDF rendering algebra, not physical calibration',
              'error_case_assertions': browser_results['missing-mapping'].splitlines()[1:],
              'joint_signs': manifest['joint_signs'],
              'fixture': 'tests/model_viewer.html', 'screenshot': 'results/model/fixture.png'}
    (output / 'validation.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

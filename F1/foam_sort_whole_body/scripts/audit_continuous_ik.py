#!/usr/bin/env python3
"""Read-only offline comparison with archived native-SDK targets; no robot I/O."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import sys

import numpy as np
import scipy

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'robot'))
from kinematics import ArmModel, ContinuousIK, pose_error


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stats(values):
    return dict(zip(['min', 'p50', 'p95', 'p99', 'max'],
                    np.percentile(values, [0, 50, 95, 99, 100]).tolist())) if values else {}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, default=TASK/'results/kinematics_validation_20260916')
    p.add_argument('--config', type=Path, default=TASK.parent/'reference/20260916_kinematics/files/home/user/workspace/f1/install/motor_ctl/share/motor_ctl/ccs_m6_40.MvKDCfg')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--limit', type=int, help='Pilot only: first N chunk targets')
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    model, chains = ArmModel(args.config), {}
    solver = ContinuousIK(model)
    req_path, resp_path = args.baseline/'requests.jsonl', args.baseline/'responses.jsonl'
    summary = {'python': platform.python_version(), 'numpy': np.__version__, 'scipy': scipy.__version__,
               'platform': platform.platform(), 'options': asdict(solver.options),
               'source_requests_sha256': sha(req_path), 'source_responses_sha256': sha(resp_path),
               'source_manifest_sha256': sha(args.baseline/'request_manifest.json'),
               'config_sha256': model.config_sha256,
               'code_sha256': {str(f.relative_to(TASK)): sha(f) for f in
                               [Path(__file__), TASK/'robot/kinematics.py']},
               'limit': args.limit, 'robot_motion': False, 'training_approved': False}
    fk_pos, fk_angle, fk_matrix = [], [], []
    records = []
    with req_path.open() as reqs, resp_path.open() as resps, (args.output/'responses.jsonl').open('x') as out:
        header = json.loads(next(resps))
        summary['native_contract'] = header['contract']
        for request_line, response_line in zip(reqs, resps, strict=True):
            req, native = json.loads(request_line), json.loads(response_line)
            assert (req['id'], req['arm']) == (native['id'], native['arm'])
            target = np.array(native['pose'])
            own = model.fk(req['arm'], req['joints'])
            pos, angle = pose_error(target, own)
            fk_pos.append(pos); fk_angle.append(angle)
            fk_matrix.append(float(np.max(np.abs(target-own))))
            if ':chunk:' not in req['id']:
                continue
            ref = chains.get(req['chain'], req['reference'])
            # Target comes from the independent native SDK. Future q is used above
            # for FK parity only and is never passed to the deployed solve API.
            result = solver.solve(req['arm'], target, ref)
            row = {'id': req['id'], 'arm': req['arm'], 'chain': req['chain'],
                   'reference': list(ref), 'ik': result}
            if result['success']:
                chains[req['chain']] = result['joints']
            records.append(row)
            out.write(json.dumps(row, allow_nan=False)+'\n')
            if len(records) % 500 == 0:
                out.flush()
                print(json.dumps({'processed': len(records),
                                  'failed': sum(not r['ik']['success'] for r in records)}), flush=True)
            if args.limit and len(records) >= args.limit:
                break
    summary['fk_parity'] = {'count': len(fk_pos), 'position_mm': stats(fk_pos),
                            'rotation_deg': stats(fk_angle), 'matrix_abs': stats(fk_matrix)}
    for prefix, selected in [('all', records), ('first20', [r for r in records if int(r['id'].split(':')[-1]) < 20])]:
        summary[prefix] = {}
        for arm in (0, 1):
            rs = [r['ik'] for r in selected if r['arm'] == arm]
            good = [r for r in rs if r['success']]
            summary[prefix][str(arm)] = {
                'count': len(rs), 'failed': len(rs)-len(good),
                'accepted_position_mm': stats([r['position_error_mm'] for r in good]),
                'accepted_rotation_deg': stats([r['rotation_error_deg'] for r in good]),
                'accepted_reference_step_deg': stats([r['reference_step_max_deg'] for r in good]),
                'latency_ms': stats([r['latency_ms'] for r in rs]),
                'optimizer_failures': sum(not r['optimizer_success'] for r in rs)}
    summary['output_sha256'] = sha(args.output/'responses.jsonl')
    (args.output/'report.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()

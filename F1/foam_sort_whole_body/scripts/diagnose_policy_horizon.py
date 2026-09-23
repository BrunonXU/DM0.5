#!/usr/bin/env python3
"""Trace all 50 model targets offline, including rejected IK candidates.

No ROS or command publication. Output intentionally is not an inference response.
References after failure are hypothetical candidates, never measured motion.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'models/DM0.5'))
from f1_data.reader import decode_rotation6d
from f1_training.execution import frozen_solver


def analyze_response(response):
    solver = frozen_solver()
    actions = np.asarray(response['diagnostics']['physical_action23'], float)
    anchor = np.asarray(response['anchor_joints14_deg'], float)
    reference = np.asarray(response['reference_joints14_deg'], float).reshape(2, 7).copy()
    if actions.shape != (50, 23) or anchor.shape != (14,) or not np.isfinite(anchor).all() or not np.isfinite(reference).all():
        raise ValueError('Expected physical H50/23, finite anchor/reference14')
    anchors = [solver.model.fk(a, anchor[a*7:a*7+7]) for a in (0, 1)]
    rows = []
    for step, action in enumerate(actions):
        for arm in (0, 1):
            block = action[arm*9:arm*9+9]
            row = dict(step=step, arm=arm, raw_eef_action9=block.tolist(), reference_deg=reference[arm].tolist())
            try:
                if not np.isfinite(block).all():
                    raise ValueError('nonfinite model EEF target')
                delta = np.eye(4)
                delta[:3, :3] = decode_rotation6d(block[3:])
                delta[:3, 3] = block[:3]
                target = anchors[arm] @ delta
                result = solver.solve(arm, target, reference[arm])
                row.update(target_eef=target.tolist(), ik=result)
                candidate = np.asarray(result['candidate_joints'], float)
                if candidate.shape != (7,) or not np.isfinite(candidate).all():
                    raise ValueError('nonfinite IK candidate')
                row['candidate_eef'] = solver.model.fk(arm, candidate).tolist()
                # Explicit hypothetical continuation, even after pose-error rejection.
                reference[arm] = candidate
            except (ValueError, FloatingPointError) as exc:
                row['error'] = str(exc)
                row['reference_policy'] = 'retain_previous_finite_reference'
            rows.append(row)
    summary = {}
    for arm in (0, 1):
        rs = [r for r in rows if r['arm'] == arm]
        iks = [r['ik'] for r in rs if 'ik' in r]
        summary[str(arm)] = dict(points=len(rs), numerical_errors=sum('error' in r for r in rs),
            strict_ik_rejections=sum(not ik['success'] for ik in iks),
            max_position_error_mm=max((ik['position_error_mm'] for ik in iks), default=None),
            max_rotation_error_deg=max((ik['rotation_error_deg'] for ik in iks), default=None),
            max_candidate_step_deg=max((ik['reference_step_max_deg'] for ik in iks), default=None))
    return dict(schema='f1_ik_diagnostic_v1', executable=False, hardware_published=False,
                request_id=response.get('request_id'), model_binding=response.get('model_binding'),
                horizon=50, physical_action23=response['diagnostics']['physical_action23'],
                continuation='hypothetical previous candidate even after rejection; not closed-loop rollout',
                limits='frozen solver joint/wrist bounds retained; no early exit for pose error or angle difference',
                summary=summary, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--response', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.response.read_bytes()
    result = analyze_response(json.loads(raw))
    result['source_sha256'] = hashlib.sha256(raw).hexdigest()
    with args.output.open('x') as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()

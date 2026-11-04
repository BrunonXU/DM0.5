#!/usr/bin/env python3
"""CPU-only exact-pose alternate-branch continuation; never commands a robot."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from scipy.optimize import minimize

from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK, pose_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    art = args.root / 'Eval/artifacts'
    sources = [art / '20261020-driver-range-study/results/original_bounds_other_branches.json',
               art / '20261020-50k-ik/joined-requests.jsonl',
               art / '20261020-50k-ik/offline-probe/inputs.json']
    config = args.root / 'src/f1_toys/native/ccs_m6_40.MvKDCfg'
    model = FastArmModel(config)
    solver = ContinuousIK(model)
    branches = json.loads(sources[0].read_text())
    requests = {(r['episode'], r['server']['request_id']): r['server']
                for r in map(json.loads, sources[1].read_text().splitlines())}
    inputs = {(r['episode'], r['request_id']): r for r in json.loads(sources[2].read_text())}
    started = time.monotonic()
    cases = []

    def check(target, q):
        error = pose_error(target, model.fk_rad(1, np.deg2rad(q)))
        return {'position_error_mm': float(error[0]), 'rotation_error_deg': float(error[1]),
                'valid': bool(model.admissible_rad(np.deg2rad(q)) and error[0] <= .01 and error[1] <= .001)}

    for group in branches:
        key = group['episode'], group['request_id']
        row, inp = requests[key], inputs[key]
        rtc = row['rtc_request']
        committed = np.asarray(rtc['committed_actions16'])[:, 7:14]
        count, coverage = len(committed), rtc['coverage_steps']
        failed_index = inp['failure_step_1_based'] - 1
        targets = [relative_pose(np.asarray(x)) for x in row['absolute_eef10'][:coverage]]
        # Already committed commands are immutable. Their FK, rather than a fresh
        # prediction for those indices, is the exact reference for seam auditing.
        for index, q in enumerate(committed):
            targets[index] = model.fk_rad(1, np.deg2rad(q))
        np.testing.assert_allclose(targets[failed_index], inp['target'], atol=1e-10)
        seed_results = [a['result'] for a in group['attempts'] if a['result']['success']]
        case = {'episode': key[0], 'request_id': key[1], 'failed_step_1_based': failed_index + 1,
                'committed_count': count, 'coverage_steps': coverage, 'branches': []}
        for number, original in enumerate(seed_results):
            branch_started = time.monotonic()
            target = targets[failed_index]
            reference = np.deg2rad(inp['reference'])
            seed = np.deg2rad(original['joints'])
            def objective(q):
                delta = q-reference
                return .5*float(delta@delta), delta
            result = minimize(objective, seed, method='SLSQP', jac=True,
                bounds=list(zip(model.low, model.high)),
                constraints=[{'type': 'eq', 'fun': lambda q: model.residual(1, q, target)[0],
                              'jac': lambda q: model.residual(1, q, target)[1]},
                             {'type': 'ineq', 'fun': lambda q: model.wrist_max-model.wrist@q,
                              'jac': lambda q: -model.wrist}],
                options={'maxiter': 150, 'ftol': 1e-11})
            polished = np.rad2deg(result.x)
            polish_check = check(target, polished)
            q_start = polished if polish_check['valid'] else np.rad2deg(seed)
            joints = {failed_index: q_start.tolist()}
            failures = []
            for direction, indices in [('backward', range(failed_index-1, -1, -1)),
                                       ('forward', range(failed_index+1, coverage))]:
                previous = q_start
                for index in indices:
                    solved = solver.solve(1, targets[index], previous)
                    if not solved['success']:
                        failures.append({'direction': direction, 'step_1_based': index+1, 'diagnostic': solved})
                        break
                    previous = np.asarray(solved['joints'])
                    assert check(targets[index], previous)['valid']
                    joints[index] = previous.tolist()
            steps = [float(np.max(np.abs(np.asarray(joints[k])-joints[k-1])))
                     for k in sorted(joints) if k-1 in joints]
            all_suffix = all(k in joints for k in range(count, coverage))
            seam = (float(np.max(abs(np.asarray(joints[count])-committed[-1])))
                    if count in joints else None)
            anchor_gap = (float(np.max(abs(np.asarray(joints[count-1])-committed[-1])))
                          if count-1 in joints else None)
            out = {'branch_index': number, 'polish_success': polish_check['valid'],
                   'polish_optimizer_status': int(result.status),
                   'original_failed_point_distance_deg': original['max_delta_from_original_reference_deg'],
                   'polished_failed_point_distance_deg': float(np.max(abs(q_start-np.rad2deg(reference)))),
                   'suffix_all_exact_pose_points_solved': all_suffix,
                   'entire_42_points_solved_with_changed_prefix': len(joints)==coverage,
                   'max_internal_neighbor_joint_change_deg': max(steps, default=0),
                   'immutable_prefix_to_first_suffix_jump_deg': seam,
                   'alternate_same_pose_at_prefix_end_joint_gap_deg': anchor_gap,
                   'solved_indices': sorted(joints), 'joints_by_index': joints, 'failures': failures,
                   'latency_ms': (time.monotonic()-branch_started)*1000}
            case['branches'].append(out)
            print(json.dumps({k: v for k, v in out.items() if k not in ['joints_by_index', 'failures', 'solved_indices']}), flush=True)
        cases.append(case)
        (args.output/'results.json').write_text(json.dumps(cases, indent=2)+'\n')
    manifest = {'robot_commands': 0, 'model_calls': 0, 'deployment_changes': 0,
                'scope': '3 first rejected requests, 8 previously known exact-pose alternate seeds; original hard limits and strict gate',
                'method': 'Polish known valid alternate seed toward original failed-point joint reference; exact-target backward and forward continuation; compare immutable prefix seam.',
                'limitations': ['Finite local searches, no global infeasibility claim.',
                                'Counterfactual alternate prefix is diagnostic only and cannot replace already committed commands.',
                                'No collision, speed, acceleration, hardware or closed-loop task certification.',
                                'Only pointwise 30Hz poses checked, no between-sample EEF interpolation proof.'],
                'position_tolerance_mm': .01, 'rotation_tolerance_deg': .001,
                'elapsed_seconds': time.monotonic()-started,
                'sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sources+[config, Path(__file__).resolve()]}}
    (args.output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()

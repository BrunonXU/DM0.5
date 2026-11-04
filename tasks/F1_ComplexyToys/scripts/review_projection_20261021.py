#!/usr/bin/env python3
"""Read-only numerical audit of saved projection proposals; no model or robot calls."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from f1_toys.eef import relative_pose
from f1_toys.native.fast_kinematics import FastArmModel
from f1_toys.native.kinematics import ContinuousIK, pose_error


def stat(values):
    return dict(n=len(values), min=float(min(values)), p50=float(np.median(values)),
                p95=float(np.percentile(values, 95)), max=float(max(values))) if values else dict(n=0)


def runs(steps):
    result = []
    for step in steps:
        if result and step == result[-1][-1]+1:
            result[-1].append(step)
        else:
            result.append([step])
    return [dict(first=x[0], last=x[-1], points=len(x), sample_span_s=(len(x)-1)/30) for x in result]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    artifact = args.task/'Eval/artifacts'
    source = artifact/'20261020-50k-ik/joined-requests.jsonl'
    rows = {(r['episode'], r['server']['request_id']): r for r in map(json.loads, source.read_text().splitlines())}
    config = args.task/'src/f1_toys/native/ccs_m6_40.MvKDCfg'
    ik = ContinuousIK(FastArmModel(config))
    inputs = [source, config]
    summaries = []
    started = time.perf_counter()
    for case in ('p1_r0p5', 'p2_r2'):
        path = artifact/'20261020-execution-tolerance/projection-fast-r3'/f'{case}.json'
        inputs.append(path)
        records = json.loads(path.read_text())['requests']
        windows, exact_checks = [], []
        material_count = projected_count = total_count = direct_changed = 0
        for record in records:
            if record['original_success'] or not record['success']:
                continue
            row = rows[record['episode'], record['request_id']]
            raw = row['server']
            changed_steps = []
            errors = []
            for diagnostic in record['diagnostics']:
                step = diagnostic['step_1_based']
                target = relative_pose(np.asarray(raw['absolute_eef10'][step-1]))
                p, a = pose_error(target, ik.model.fk(1, np.asarray(diagnostic['joints'])))
                assert abs(p-diagnostic['position_error_mm']) < 1e-7
                assert abs(a-diagnostic['rotation_error_deg']) < 1e-7
                changed = p > .01 or a > .001
                material_count += changed
                projected_count += diagnostic.get('projected', False)
                total_count += 1
                if changed:
                    changed_steps.append(step)
                    errors.append(dict(step=step, position_mm=p, rotation_deg=a,
                        gripper_proposal=float(np.clip(raw['absolute_eef10'][step-1][9]*50+50, 0, 100))))
                if changed and diagnostic.get('direct_projection'):
                    direct_changed += 1
                    reference = record['joints'][step-2] if step > 1 else raw['prediction_replay']['state16'][7:14]
                    exact = ik.solve(1, target, np.asarray(reference))
                    exact_checks.append(dict(episode=record['episode'], request_id=record['request_id'], step=step,
                        projected_error_mm=p, projected_error_deg=a, exact_success=bool(exact['success']),
                        exact_position_mm=exact['position_error_mm'], exact_rotation_deg=exact['rotation_error_deg'],
                        exact_joint_step_max_deg=exact['reference_step_max_deg'],
                        exact_joints=exact['joints']))
            grips = [float(np.clip(raw['absolute_eef10'][i][9]*50+50, 0, 100)) for i in range(record['coverage_steps'])]
            segments = runs(changed_steps)
            windows.append(dict(episode=record['episode'], request_id=record['request_id'],
                total_suffix_points=len(record['diagnostics']), changed_points=len(changed_steps),
                changed_runs=segments, changed_point_errors=errors,
                gripper_proposal_min=min(grips), gripper_proposal_max=max(grips),
                gripper_proposal_crosses_half=min(grips)<50<=max(grips),
                changed_gripper_min=min((x['gripper_proposal'] for x in errors), default=None),
                changed_gripper_max=max((x['gripper_proposal'] for x in errors), default=None)))
        failure_records = [x for x in records if not x['original_success']]
        paired_estimates = []
        for record in failure_records:
            raw = rows[record['episode'], record['request_id']]
            timing = raw['client_timing']['timing']
            replacement = timing['client_total_ms']-raw['server']['timing_ms']['ik']+record['latency_ms']
            paired_estimates.append(dict(episode=record['episode'], request_id=record['request_id'],
                estimate_client_ms=replacement, over_d14_deadline=replacement>14000/30))
        summary = dict(case=case, rescued_windows=len(windows), suffix_points=total_count,
            changed_beyond_original_pose_tolerance=material_count,
            changed_fraction=material_count/total_count,
            projected_solver_calls=projected_count,
            changed_points_per_window=stat([x['changed_points'] for x in windows]),
            changed_run_points=stat([r['points'] for x in windows for r in x['changed_runs']]),
            windows_with_gripper_proposal_crossing_half=sum(x['gripper_proposal_crosses_half'] for x in windows),
            direct_projection_changed_points=direct_changed,
            direct_changed_but_same_reference_exact_succeeds=sum(x['exact_success'] for x in exact_checks),
            exact_success_joint_step_deg=stat([x['exact_joint_step_max_deg'] for x in exact_checks if x['exact_success']]),
            saved_ik_latency_ms=stat([x['latency_ms'] for x in failure_records]),
            saved_ik_alone_over_d14_deadline=sum(x['latency_ms']>14000/30 for x in failure_records),
            historical_total_replacement_estimate_ms=stat([x['estimate_client_ms'] for x in paired_estimates]),
            historical_total_replacement_over_d14=sum(x['over_d14_deadline'] for x in paired_estimates),
            current_d14_deadline_ms=14000/30,
            current_d14_s16_replan_ms=16000/30,
            pose_equivalence_to_model=False,
            closed_loop_task_equivalence_established=False)
        result = dict(summary=summary, windows=windows, strict_checks=exact_checks,
            historical_timing_estimates=paired_estimates,
            caveats=[
                'Changed means FK residual exceeds original 0.01 mm / 0.001 deg numerical tolerances; not a task-clearance criterion.',
                'Checks use identical original target and previous saved r3 joint reference. Success is a local exact solution witness; failure is not global infeasibility.',
                'A pointwise exact witness does not certify entire corrected continuation, dynamic limits or collision/contact clearance.',
                'Gripper values are original future model proposals, not observed contact, insertion or release labels. Mixed gripper proposals preclude calling every window transport-only.',
                'Frozen old LoRA 50K D20/S20 K42 windows, not new Full625/1000 D14/S16 K32. No counterfactual new images or predictions.',
                'Timing substitution mixes old network/model timing with offline CPU IK; illustrative only, neither current Full nor production deadline certification.',
                '18 original successful controls do not test subsequent model behavior after projected execution.',
                'Projected minimax has unit/budget weighting and local optimization; it is not uniquely correct task-aware nearest action.'
            ])
        (args.output/f'{case}.json').write_text(json.dumps(result, indent=2)+'\n')
        summaries.append(summary)
        print(json.dumps(summary), flush=True)
    (args.output/'summary.json').write_text(json.dumps(summaries, indent=2)+'\n')
    inputs.append(Path(__file__))
    (args.output/'manifest.json').write_text(json.dumps(dict(
        inputs={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
        robot_commands=0, model_calls=0, gpu_calls=0, deployment_changes=0,
        elapsed_s=time.perf_counter()-started), indent=2)+'\n')


if __name__ == '__main__':
    main()

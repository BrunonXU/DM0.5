"""Exercise mid-motion branches on a reference-only archived full cycle.

This validates the output smoother, not owner publication or environment checks.
The numerical branch has no transport, and no actual future measurement enters
the branch computation. Prescribed future reference timing is still archived.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from f1dagger.robot_io import make_output
from output_branch import fork_output


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def step(output, row):
    target = row['reference16'][:14] if row['new_motion_reference'] else output.position.copy()
    now = row['virtual_ns'] / 1e9
    output.push(target, now, enabled=(False, row['new_motion_reference']))
    value = output.sample(now)
    if output.fault:
        raise RuntimeError(output.fault)
    return value.copy()


def run(receipt_path, destination):
    receipt_path = receipt_path.resolve()
    prior = json.loads(receipt_path.read_text())
    input_path = receipt_path.parent / 'input.json'
    if sha(input_path) != prior['input_sha256'] or not prior['fidelity_passed']:
        raise ValueError('prior_reference_identity_invalid')
    sources = {**prior['source_sha256'], **prior['inherited_source_sha256']}
    if not all(sha(p) == h for p, h in sources.items()):
        raise ValueError('source_changed_since_full_owner_comparison')
    value = json.loads(input_path.read_text())
    report_path = Path(prior['report'])
    report = json.loads(report_path.read_text())
    rows = value['references']
    oracle_path = report_path.parent / 'mock-owner-outputs.json'
    oracle = np.array([r['submitted16'][:14] for r in json.loads(oracle_path.read_text())])
    targets = {}
    for stage in report['stages']:
        skill = stage['request']['skill']
        if skill not in ('grasp', 'transfer_above_hole', 'withdraw_reset'):
            continue
        indices = [i for i, row in enumerate(rows) if row['request_id'] == stage['request']['request_id']
                   and row['new_motion_reference']]
        for numerator in (1, 2):
            targets[indices[len(indices) * numerator // 3]] = skill
    config = report['offline_relocations']['numerics_spec']['gate']['config']
    output = make_output(config)
    # Exact original limits, not a new generic envelope.
    owner_path = Path(report['scenario']['site']) / 'owner.local.json'
    from f1_harness.numerical_service import NumericalDispatcher
    from copy import deepcopy
    spec = deepcopy(report['offline_relocations']['numerics_spec'])
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    spec['gate']['runtime'] = str(destination / 'native-model')
    native = NumericalDispatcher(spec)
    output.planner.set_limits(native.numerics.mapping.limits)
    output.push(value['initial_state16'][:14], value['initialized_at_ns'] / 1e9, enabled=(False, False))
    results = []
    whole = []
    for index, row in enumerate(rows):
        whole.append(step(output, row))
        if index not in targets:
            continue
        start = time.perf_counter()
        branch, before = fork_output(output)
        fork_s = time.perf_counter() - start
        start = time.perf_counter()
        predicted = np.array([step(branch, future) for future in rows[index + 1:]])
        suffix_s = time.perf_counter() - start
        _, after = fork_output(output)
        velocity = float(np.max(abs(np.rad2deg(before['native_state']['velocity']))))
        results.append(dict(skill=targets[index], index=index, virtual_ns=row['virtual_ns'],
            fork_s=fork_s, suffix_compute_s=suffix_s, suffix_samples=len(predicted),
            source_state_unchanged=before == after,
            max_velocity_at_fork_deg_s=velocity, moving_state=velocity > .1,
            exact_future_output=bool(np.array_equal(predicted, oracle[index + 1:])),
            max_output_difference_deg=float(np.max(abs(predicted - oracle[index + 1:]))),
            initial_state=before))
    passed = (bool(results) and all(r['source_state_unchanged'] and r['exact_future_output'] for r in results)
              and np.array_equal(whole, oracle) and any(r['moving_state'] for r in results))
    sources.update({str(p.resolve()): sha(p) for p in (
        receipt_path, input_path, oracle_path, owner_path, Path(__file__), Path(__file__).with_name('output_branch.py'))})
    unchanged = all(sha(p) == h for p, h in sources.items())
    result = dict(schema='f1-output-branch-study-v1', cases=results, passed=bool(passed and unchanged),
        original_full_arm_output_exact=bool(np.array_equal(whole, oracle)), source_unchanged=unchanged,
        source_sha256=sources, physical_motion=False, execution_authorized=False,
        limitations=['Original output smoother only: no new robot/body/task-scene check in this comparison.',
            'Reference timing is recorded; no prospective time or perception prediction.',
            'Fork is in-process and single-threaded; native certificate is immutable by F1 convention.',
            'No RPC serialization, live owner integration, deployment or real experience promotion.'])
    (destination / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(passed=result['passed'], cases=len(results),
        moving_cases=sum(r['moving_state'] for r in results),
        max_fork_ms=max(r['fork_s'] for r in results) * 1000,
        max_difference_deg=max(r['max_output_difference_deg'] for r in results)), indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.receipt, args.output))

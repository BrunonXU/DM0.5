"""Validate bounded output preview against a saved, nominal full simulation.

Reference timestamps come from the saved run: this measures software prediction
fidelity, NOT prospective stage timing or online planning. Future submitted and
measured outputs are only comparison oracles, never preview inputs. No robot,
ROS, socket service or model request is started.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from f1_harness.contracts import Task
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from output_preview import preview_references
from output_sweeps import sweep_outputs


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(report_path, output):
    report_path = report_path.resolve()
    report = json.loads(report_path.read_text())
    if report['mode'] != 'fake' or report['physical_motion'] or report['robot_connected']:
        raise ValueError('preview_requires_offline_simulation')
    scenario = report['scenario']
    if scenario['follower'] != 'approved-output' or scenario['missing_scene_at'] is not None:
        raise ValueError('preview_requires_nominal_approved_output')
    if not report['checks']['full_cycle_completed']:
        raise ValueError('preview_requires_complete_cycle')
    changed = [p for p, expected in report['source_files'].items() if sha(p) != expected]
    if changed:
        raise ValueError('saved_simulation_source_changed:' + ','.join(changed))
    source_paths = [report_path, report_path.parent / 'mock-owner-outputs.json',
                    report_path.parent / 'collected/owner/harness/trace.jsonl',
                    Path(__file__), Path(__file__).with_name('output_preview.py'),
                    Path(__file__).with_name('output_sweeps.py')]
    identities = {str(p.resolve()): sha(p) for p in source_paths}
    actual = json.loads(source_paths[1].read_text())
    events = [json.loads(line) for line in source_paths[2].read_text().splitlines()]
    # The historical exporter omitted owner.hold calls. Nominal successful
    # traces have no mid-motion hold/recovery. Reject unsupported traces rather
    # than inventing hidden state resets from future output observations.
    forbidden = {'harness_motion_checkpoint', 'hold_acknowledged', 'skill_revoked'}
    if any(e['kind'] in forbidden for e in events):
        raise ValueError('preview_hold_history_not_exported')
    if any(e['kind'] == 'authority_revoked' and e['monotonic_ns'] < actual[-1]['virtual_ns'] for e in events):
        raise ValueError('preview_authority_changed_before_last_output')
    scene = json.loads(Path(scenario['scene']).read_text())[scenario['scene_index']]
    initial = scene['observation']
    if actual[0]['virtual_ns'] != 10_020_000_000:
        raise ValueError('unknown_simulator_initialization')
    value = dict(schema='f1-reference-preview-input-v1', initial_state_kind='reset_at_rest',
        initialized_at_ns=10_000_000_000, full22=scene['full22'], initial_state16=initial['state16'],
        state_valid_mask=initial['state_valid_mask'], references=[
            {k: row[k] for k in ('virtual_ns', 'reference16', 'new_motion_reference', 'request_id')}
            for row in actual])
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    (output / 'input.json').write_text(json.dumps(value, indent=2) + '\n')
    spec = deepcopy(report['offline_relocations']['numerics_spec'])
    spec['gate']['runtime'] = str(output / 'native-model')
    numeric = NumericalDispatcher(spec)
    prediction = preview_references(value, config=spec['gate']['config'],
        mapping=numeric.numerics.mapping, gate=numeric.numerics.gate)
    (output / 'prediction.json').write_text(json.dumps(prediction, indent=2) + '\n')
    rows = prediction['rows']
    expected = np.array([row['submitted16'] for row in actual])
    predicted = np.array([row['submitted16'] for row in rows])
    measured = np.array([row['measured16'] for row in rows])
    times = np.array([row['virtual_ns'] for row in rows], dtype=np.int64)
    checks = dict(
        output_exactly_reproduced=bool(np.array_equal(expected, predicted)),
        predicted_feedback_matches_oracle=bool(np.array_equal(measured, [r['measured16'] for r in actual])),
        complete_publications=all(r['output_evidence']['publication_complete'] for r in rows),
        immutable_left_arm=bool(np.all(predicted[:, :7] == predicted[0, :7])),
    )
    speed = float(np.max(abs(np.diff(predicted[:, :14], axis=0) / (np.diff(times)[:, None] / 1e9))))
    checks['sampled_speed_within_profile'] = speed <= report['output_profile']['velocity_deg_s'] + 1e-6
    site = Path(scenario['site'])
    fixture = json.loads((site / 'plan.local.json').read_text())['fixture']
    fixture['frame_chain']['development_owner_config'] = str(site / 'owner.local.json')
    service = PlanService(Task.model_validate_json((site / 'task.local.json').read_text()),
                          fixture, output / 'unused.sock')

    class Direct:
        def call(self, method, **kwargs):
            return numeric.dispatch(method, kwargs)

    service.numerics = Direct()
    started = time.perf_counter()
    sweeps = sweep_outputs(rows, events, service, numeric)
    sweep_s = time.perf_counter() - started
    checks['both_noncontact_stages_swept'] = {s['skill'] for s in sweeps} == {'transfer_above_hole', 'withdraw_reset'}
    checks['source_unchanged'] = all(sha(p) == h for p, h in {**report['source_files'], **identities}.items())
    clear = bool(sweeps) and all(s['passed'] for s in sweeps)
    result = dict(schema='f1-reference-preview-comparison-v1', report=str(report_path),
        source_sha256=identities, inherited_source_sha256=report['source_files'],
        input_sha256=sha(output / 'input.json'), prediction_sha256=sha(output / 'prediction.json'),
        checks=checks, fidelity_passed=all(checks.values()), noncontact_scene_clear=clear,
        max_output_difference_deg=float(np.max(abs(expected - predicted))),
        sampled_max_speed_deg_s=speed, samples=len(rows), preview_compute_s=prediction['compute_s'],
        sweep_compute_s=sweep_s, sweeps=sweeps, physical_motion=False, execution_authorized=False,
        prospective_timing_verified=False, real_experience_eligible=False,
        limitations=prediction['limitations'] + [
            'Input reference timeline and per-stage scene context came from a saved simulation.',
            'No online planner integration, live state clone, recipe fallback or physical acceptance.',
            'Sweep covers transfer/empty return only, at 0.1 degree joint subdivisions.'])
    (output / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ('fidelity_passed', 'noncontact_scene_clear',
        'max_output_difference_deg', 'samples', 'preview_compute_s', 'sweep_compute_s')}, indent=2))
    return 0 if result['fidelity_passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.report, args.output))

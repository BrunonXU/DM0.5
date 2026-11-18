"""Audit saved in-memory F1Owner outputs, never connect to robot hardware.

Check publication integrity, finite-difference output speed and non-moving
rechecks. Reconstruct transfer/empty-return scenes and sweep the submitted
output, not just the planned waypoints. Other stages have special contact
contracts and are explicitly outside this independent scene sweep.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from f1_harness.contracts import Task
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from output_sweeps import sweep_outputs

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report', required=True, type=Path)
parser.add_argument('--output', required=True, type=Path)
args = parser.parse_args()
report_path = args.report.resolve()
report = json.loads(report_path.read_text())
assert report['scenario']['follower'] == 'approved-output'

run = report_path.parent
output_path = run / 'mock-owner-outputs.json'
trace_path = run / 'collected/owner/harness/trace.jsonl'
outputs = json.loads(output_path.read_text())
events = [json.loads(line) for line in trace_path.read_text().splitlines()]
from offline_audit_scope import require_offline_output_report
require_offline_output_report(report,events,outputs)
q = np.array([row['submitted16'] for row in outputs])
t = np.array([row['virtual_ns'] for row in outputs], dtype=np.int64)
assert len(q) > 1 and np.isfinite(q).all() and np.all(np.diff(t) > 0)
speed = np.diff(q[:, :14], axis=0) / (np.diff(t)[:, None] / 1e9)
checks = dict(
    complete_publications=all(row['output_evidence']['publication_complete'] for row in outputs),
    immutable_left_arm=bool(np.all(q[:, :7] == q[0, :7])),
    sampled_speed_within_profile=bool(np.max(abs(speed)) <= report['output_profile']['velocity_deg_s'] + 1e-6),
    input_identity_unchanged=all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha
        for p, sha in report['source_files'].items()),
)
# A recheck may still publish a hold keepalive. It must not change that command
# or enqueue another motion reference; counting publisher rows alone is wrong.
rechecks = []
for stage in report['stages']:
    request = stage['request']
    if not request.get('verification_request_id'):
        continue
    rows = [row for row in outputs if row['request_id'] == request['request_id']]
    assert rows, 'Missing output evidence for recheck'
    anchor = np.array(rows[0]['measured16'])
    delta = float(np.max(abs(np.array([row['submitted16'] for row in rows]) - anchor)))
    rechecks.append(dict(request_id=request['request_id'], rows=len(rows), max_change_deg=delta,
                         no_motion_reference=not any(row['new_motion_reference'] for row in rows)))
checks['rechecks_hold_unchanged'] = all(r['max_change_deg'] <= 1e-9 and r['no_motion_reference'] for r in rechecks)

site = Path(report['scenario']['site']).resolve()
fixture = json.loads((site / 'plan.local.json').read_text())['fixture']
fixture['frame_chain']['development_owner_config'] = str(site / 'owner.local.json')
spec = report['offline_relocations']['numerics_spec']
numeric = NumericalDispatcher(spec)

class Direct:
    def call(self, method, **kwargs):
        return numeric.dispatch(method, kwargs)

service = PlanService(Task.model_validate_json((site / 'task.local.json').read_text()),
                      fixture, run / 'unused-audit.sock')
service.numerics = Direct()
sweeps = sweep_outputs(outputs, events, service, numeric)
checks['covered_output_sweeps_clear'] = all(s['passed'] for s in sweeps)
completed_covered = {'transfer_above_hole', 'withdraw_reset'} & set(report['completed_skills'])
checks['completed_stages_have_sweeps'] = completed_covered <= {s['skill'] for s in sweeps}
result = dict(schema='f1-replay-output-audit-v1', physical_motion=False,
    report=str(report_path), source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (report_path, output_path, trace_path, Path(__file__), Path(__file__).with_name("output_sweeps.py"))},
    checks=checks, passed=all(checks.values()), output_samples=len(outputs),
    sampled_max_speed_deg_s=float(np.max(abs(speed))), rechecks=rechecks, sweeps=sweeps,
    limitations=['Outputs and feedback are simulated; local profile not verified against robot.',
        'Only transfer and empty-return environment sweeps; other contact stages are not audited here.',
        'Finite-difference speed is sampled evidence, not a continuous acceleration/jerk certificate.',
        'No camera, ROS, plant, full ControlLoop, STOP or seal verification.'])
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(dict(passed=result['passed'], checks=checks, output=str(args.output)), indent=2))
if not result['passed']:
    raise SystemExit(1)

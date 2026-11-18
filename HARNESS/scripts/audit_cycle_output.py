"""Combine actual software output audits with explicit request-level coverage."""
import argparse,hashlib,json
from pathlib import Path
import numpy as np
from f1_harness.contracts import Task
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from ordinary_output_sweeps import sweep_ordinary,readonly_outputs

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--report',type=Path,required=True)
p.add_argument('--prior-audit',type=Path,action='append',default=[])
p.add_argument('--output',type=Path,required=True)
a=p.parse_args();read=lambda f:json.loads(f.read_text());report=read(a.report);run=a.report.parent

identity={k:hashlib.sha256(Path(k).read_bytes()).hexdigest()==v for k,v in report['source_files'].items()}
if not identity or not all(identity.values()):raise ValueError('recorded_runtime_or_site_changed')
rows=read(run/'mock-owner-outputs.json');plans=read(run/'plans.json')
events=[json.loads(l) for l in (run/'collected/owner/harness/trace.jsonl').read_text().splitlines()]
from offline_audit_scope import require_offline_output_report
require_offline_output_report(report,events,rows)
site=Path(report['scenario']['site']).resolve();fixture=read(site/'plan.local.json')['fixture']
numeric=NumericalDispatcher(report['offline_relocations']['numerics_spec'])
class Direct:
    def call(self,method,**kw):return numeric.dispatch(method,kw)
service=PlanService(Task.model_validate_json((site/'task.local.json').read_text()),fixture,run/'unused-cycle-audit.sock');service.numerics=Direct()
sweeps=sweep_ordinary(rows,events,plans,service,numeric);prior=[]
for path in a.prior_audit:
    value=read(path)
    if Path(value['report']).resolve()!=a.report.resolve() or not value['passed']:
        raise ValueError('prior_audit_scope_or_result_mismatch')
    evidence=value.get('sources',value.get('source_sha256',{}))
    if str((run/'mock-owner-outputs.json').resolve()) not in evidence:
        raise ValueError('prior_audit_output_binding_missing')
    for name,sha in evidence.items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=sha:raise ValueError('prior_audit_source_changed:'+name)
    # Do not treat a success flag as coverage: actual per-request sweeps only.
    records=value.get('sweeps',value.get('grasp_results',value.get('results',[])))
    if not records or any(not r['passed'] or not r['samples'] for r in records):
        raise ValueError('prior_audit_empty_or_failed')
    prior.extend(dict(request_id=r['request_id'],skill=r.get('skill'),samples=r['samples'],audit=str(path)) for r in records)
read_only=readonly_outputs(rows,report['stages'])
covered={r['request_id'] for r in [*sweeps,*prior,*read_only]}
actual={r['request_id'] for r in rows if r['request_id'] is not None}
q=np.asarray([r['submitted16'] for r in rows]);t=np.asarray([r['virtual_ns'] for r in rows],dtype=np.int64)
if len(q)<2 or not np.isfinite(q).all() or np.any(np.diff(t)<=0):raise ValueError('invalid_output_stream')
speed=np.max(abs(np.diff(q[:,:14],axis=0)/(np.diff(t)[:,None]/1e9)))
checks=dict(all_request_outputs_covered=actual<=covered,all_sweeps_clear=all(r['passed'] for r in sweeps),
    read_only_holds_unchanged=all(r['passed'] for r in read_only),
    complete_publications=all(r['output_evidence']['publication_complete'] for r in rows),
    left_fixed=bool(np.all(q[:,:7]==q[0,:7]) and np.all(q[:,14]==q[0,14])),
    sampled_speed_within_profile=bool(speed<=report['output_profile']['velocity_deg_s']+1e-6),
    all_anonymous_outputs_stationary=all(not r['new_motion_reference'] and np.array_equal(r['submitted16'],r['measured16']) for r in rows if r['request_id'] is None))
files=[a.report,run/'mock-owner-outputs.json',run/'plans.json',run/'collected/owner/harness/trace.jsonl',Path(__file__),Path(__file__).with_name('ordinary_output_sweeps.py'),*a.prior_audit]
result=dict(schema='f1-cycle-output-audit-v1',report=str(a.report.resolve()),physical_motion=False,passed=all(checks.values()),
    checks=checks,uncovered_requests=sorted(actual-covered),ordinary_sweeps=sweeps,prior_sweeps=prior,read_only=read_only,
    output_rows=len(rows),sampled_max_speed_deg_s=float(speed),
    sources={str(f.resolve()):hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
    limitations=['Real initial scene, simulated following observations/plant; no physical result.',
        'Actual software F1Owner/Ruckig output; interpolation at at most 0.1 degree joint steps.',
        'Not a continuous collision, acceleration/jerk or hardware clearance certificate.',
        'Registered target contact at grasp/release; no force, friction or moving-jaw geometry.',
        'No actual camera/SAM/planner/RPC/ROS full-chain validation in this audit.'])
if a.output.exists():raise FileExistsError(a.output)
a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(passed=result['passed'],checks=checks,uncovered=result['uncovered_requests'],ordinary=[dict(skill=r['skill'],samples=r['samples'],failures=len(r['failures'])) for r in sweeps]),indent=2))
if not result['passed']:raise SystemExit(1)

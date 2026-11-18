"""Offline actual owner command audit under both unknown-attachment hypotheses."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from f1_harness.contracts import Observation,Task
from f1_harness.geometry import transform
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from f1_harness.skills import _motion_clear
from f1_harness.contact_departure import contact_exit_geometry_clear
from f1_harness.command_forecast import sweep_anchor

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--report',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();report=json.loads(a.report.read_text());run=a.report.parent

identity={name:hashlib.sha256(Path(name).read_bytes()).hexdigest()==sha
          for name,sha in report['source_files'].items() if '/scripts/' not in name}
if not identity or not all(identity.values()):raise ValueError('recorded_runtime_or_site_changed')
read=lambda path:json.loads(path.read_text())
rows=read(run/'mock-owner-outputs.json')
events=[json.loads(line) for line in (run/'collected/owner/harness/trace.jsonl').read_text().splitlines()]
from offline_audit_scope import require_offline_output_report
require_offline_output_report(report,events,rows)
site=Path(report['scenario']['site']).resolve();fixture=read(site/'plan.local.json')['fixture']
numeric=NumericalDispatcher(report['offline_relocations']['numerics_spec'])
class Direct:
    def call(self,method,**kw):return numeric.dispatch(method,kw)
service=PlanService(Task.model_validate_json((site/'task.local.json').read_text()),fixture,run/'unused-audit.sock')
service.numerics=Direct();results=[]
for start in events:
    if start['kind']!='harness_planning_started' or start['intent']['skill']!='recover_grasp':continue
    request=start['request_id'];commands=[r for r in rows if r['request_id']==request]
    if not commands:continue
    proof=next(e['proof'] for e in events if e['kind']=='harness_plan_revalidated' and e['proof']['request_id']==request)
    full=start['full_reference'];before=sweep_anchor(commands)
    obs=Observation.model_validate(proof['observation'])
    scene=service._scene(obs,full,obs.state16,skill='recover_grasp')
    assert scene.object_held is None and scene.eef_object is None
    hypothetical=np.linalg.inv(transform(scene.base_eef))@transform(scene.base_object)
    fk=lambda q:transform(numeric.dispatch('forward_pose',dict(full22=full,held16=q.tolist())))
    poses=[fk(before)];failures=[];samples=0
    anchor=before.copy();grip_ok=True;left_ok=True;publication_ok=True
    for row in commands:
        target=np.asarray(row['submitted16'],float)
        grip_ok &= target[15]==anchor[15]
        left_ok &= bool(np.array_equal(target[:7],anchor[:7]) and target[14]==anchor[14])
        publication_ok &= row['output_evidence']['publication_complete']
        steps=max(1,int(np.ceil(np.max(abs(target[:14]-before[:14]))/.1)))
        for fraction in np.linspace(0,1,steps+1)[1:]:
            q=before+fraction*(target-before)
            try:
                numeric.numerics.mapping.check_action(q);numeric.numerics.gate.check_preventive(q,full)
            except Exception as exc:failures.append(dict(type='native',reason=str(exc)))
            current=fk(q)
            if not _motion_clear(scene,poses[-1],current,hypothetical,allow_target_contact=True,support_lift=True):
                failures.append(dict(type='possibly_loaded',virtual_ns=row['virtual_ns'],fraction=float(fraction)))
            poses.append(current);samples+=1
        before=target
    checks=dict(empty_hypothesis_contact_exit_clear=contact_exit_geometry_clear(scene,poses),
                loaded_hypothesis_and_native_clear=not failures,gripper_unchanged=bool(grip_ok),
                left_fixed=bool(left_ok),publication_complete=bool(publication_ok))
    results.append(dict(request_id=request,samples=samples,checks=checks,passed=all(checks.values()),failures=failures))
if not results:raise ValueError('no_observation_motion_output')
result=dict(schema='f1-grasp-observation-output-audit-v1',report=str(a.report.resolve()),passed=all(r['passed'] for r in results),
    physical_motion=False,results=results,recorded_runtime_and_site_identity=identity,
    sources={str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest() for path in
             (a.report,run/'mock-owner-outputs.json',run/'collected/owner/harness/trace.jsonl',Path(__file__))},
    limitations=['Saved real initial geometry, ideal simulated plant/attachment and observations after motion.',
                 'Actual F1Owner/Ruckig software outputs; no robot, camera, model API or physical grasp proof.',
                 'Both collision hypotheses checked; neither grants a held=True observation.'])
if a.output.exists():raise FileExistsError(a.output)
a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(passed=result['passed'],results=results),indent=2))
if not result['passed']:raise SystemExit(1)

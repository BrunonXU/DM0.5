"""Audit simulated grasp/regrasp output, including failed emitted prefixes."""
import argparse,json,hashlib
from pathlib import Path
from f1_harness.contracts import Task
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.skill_services import PlanService
from grasp_output_sweeps import sweep_grasp
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
a=parser.parse_args();report=json.loads(a.report.read_text());run=a.report.parent

# Re-evaluation must use the recorded runtime and site. Replay drivers are not
# executed here, and may have changed since the original command recording.
identity={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()==h
          for p,h in report['source_files'].items() if '/scripts/' not in p}
if not identity or not all(identity.values()):raise ValueError('recorded_runtime_or_site_changed')
read=lambda p:json.loads(p.read_text())
outputs=read(run/'mock-owner-outputs.json');plans=read(run/'plans.json')
events=[json.loads(l) for l in (run/'collected/owner/harness/trace.jsonl').read_text().splitlines()]
from offline_audit_scope import require_offline_output_report
require_offline_output_report(report,events,outputs)
site=Path(report['scenario']['site']).resolve();fixture=read(site/'plan.local.json')['fixture']
fixture['frame_chain']['development_owner_config']=str(site/'owner.local.json')
numeric=NumericalDispatcher(report['offline_relocations']['numerics_spec'])
class Direct:
 def call(self,method,**kwargs):return numeric.dispatch(method,kwargs)
service=PlanService(Task.model_validate_json((site/'task.local.json').read_text()),fixture,run/'unused-audit.sock');service.numerics=Direct()
results=sweep_grasp(outputs,events,plans,service,numeric)
result=dict(schema='f1-grasp-command-output-audit-v3',report=str(a.report.resolve()),
 physical_motion=False,passed=all(r['passed'] for r in results),grasp_results=results,
 recorded_runtime_and_site_identity=identity,
 sources={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in [a.report,run/'mock-owner-outputs.json',run/'plans.json',
 run/'collected/owner/harness/trace.jsonl',Path(__file__),Path(__file__).with_name('grasp_output_sweeps.py'),
 Path(__file__).with_name('support_plane_diagnostic.py')]},
 limitations=['Passing means all recorded emitted commands passed the path audit, not that the skill succeeded.',
 'Partial exit may use a registered mathematical extension witness; that witness is not emitted output.',
 'No contact forces, friction, jaw aperture geometry, slip or physical grasp proof.',
 'Hypothetical rigid attachment at commanded closure; not measured in-hand geometry.',
 'Registered target contact is allowed; all other obstacles and support margin retained.',
 'Plane diagnostics describe a linear-translation/shortest-rotation pose arc, not measured contact or the exact continuous joint path.',
 'Diagnostic clearance does not override a rejected registered sweep.',
 'Audit is diagnostic and grants no execution authority.'])
a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(dict(passed=result['passed'],rows=[dict(samples=r['samples'],checks=r['checks'],failures=len(r['failures']),native_failures=len(r['native_failures'])) for r in results]),indent=2))
if not result['passed']:raise SystemExit(1)

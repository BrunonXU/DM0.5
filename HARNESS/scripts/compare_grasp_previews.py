"""Compare saved model grasps through the native planner without robot output."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import time
from f1_harness.contracts import Observation,Task,MotionParameters
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness.owner_bridge import numerical_failure_detail
from f1_harness.placement_advisory import placement_advisory
from f1_harness.skill_services import PlanService

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--report',type=Path,required=True)
p.add_argument('--site',type=Path,required=True)
p.add_argument('--request',type=Path,action='append',required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
report=json.loads(a.report.read_text())
input_path=a.report.parent/'planning-inputs.jsonl'
source=next(json.loads(line) for line in input_path.read_text().splitlines()
            if json.loads(line)['intent']['skill']=='approach_grasp')
obs=Observation.model_validate(source['observation'])
numeric=NumericalDispatcher(report['offline_relocations']['numerics_spec'])
fixture=json.loads((a.site/'plan.local.json').read_text())['fixture']
service=PlanService(Task.model_validate_json((a.site/'task.local.json').read_text()),fixture,
    a.output.parent/'unused-grasp-comparison.sock',clock=lambda:obs.received_ns)
service.numerics=SimpleNamespace(call=lambda method,**kw:numeric.dispatch(method,kw))
rows=[]
for path in a.request:
    request=json.loads(path.read_text())
    if request['name']!='approach_grasp':raise ValueError('approach_request_required')
    args=deepcopy({k:v for k,v in source.items() if k!='simulation'})
    args['intent']['parameters']=MotionParameters.model_validate(request['parameters']).model_dump()
    start=time.perf_counter()
    try:
        proposal=service.dispatch('plan_skill',args)
        row=dict(planned=True,placement_advisory=placement_advisory(proposal['geometry_trace']),proposal=proposal)
    except Exception as exc:row=dict(planned=False,diagnostic=numerical_failure_detail(exc))
    row.update(request=str(path),request_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
               elapsed_s=time.perf_counter()-start)
    rows.append(row)
    print(json.dumps({k:v for k,v in row.items() if k!='proposal'}),flush=True)
result=dict(physical_motion=False,robot_connected=False,api_calls=0,
    source=str(input_path),source_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
    site=str(a.site),rows=rows,limitation='Saved initial scene and feedback; predicted attachment is not physical grasp evidence.')
with a.output.open('x') as f:json.dump(result,f,indent=2)

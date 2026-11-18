"""Reproduce an actual recorded planning call; observe departure checks unchanged."""
import argparse,hashlib,json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from scipy.spatial.transform import Rotation
from f1_harness.contracts import Observation,Task
from f1_harness.geometry import transform,tool_sweep_clear
from f1_harness.skill_services import PlanService
from f1_harness.numerical_service import NumericalDispatcher
from f1_harness import grasp_observation
from f1_harness.contact_departure import _corners,contact_exit_geometry_clear
from f1_harness.skills import tool_margin

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--report',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
p.add_argument('--site',type=Path,help='Explicit current-source offline site; recorded input remains unchanged')
a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
report=json.loads(a.report.read_text());run=a.report.parent
source=next(json.loads(l) for l in (run/'planning-inputs.jsonl').read_text().splitlines()
            if json.loads(l)['intent']['skill']=='recover_grasp')
site=a.site or Path(report['scenario']['site']);fixture=json.loads((site/'plan.local.json').read_text())['fixture']
obs=Observation.model_validate(source['observation'])
numeric=NumericalDispatcher(report['offline_relocations']['numerics_spec'])
service=PlanService(Task.model_validate_json((site/'task.local.json').read_text()),fixture,
                    run/'unused-diagnostic.sock',clock=lambda:obs.received_ns)
service.numerics=SimpleNamespace(call=lambda method,**kw:numeric.dispatch(method,kw))
diagnostic={}
original=grasp_observation.check_actual_path
def inspect(scene,poses,targets,held,config):
    path=[transform(v) for v in poses]
    target=next(o for o in scene.obstacles if o.get('role')=='target_object')
    others=[o for o in scene.obstacles if o is not target]
    margin=tool_margin(scene)
    def clear(a,b,obstacles,boxes=scene.tool_local_boxes):
        return tool_sweep_clear(a,b,scene.eef_tip_reference,scene.attached_radius_m,
                               obstacles,margin_m=margin,local_boxes=boxes)
    diagnostic.update(poses=[v.tolist() for v in path],gripper_unchanged=all(v[15]==held[15] for v in targets),
        full_endpoint_clear=clear(path[-1],path[-1],scene.obstacles),
        non_target_clear=all(clear(x,y,others) for x,y in zip(path,path[1:])),
        original_departure_clear=contact_exit_geometry_clear(scene,path),boxes=[])
    other,other_axes=_corners(target)
    for box in scene.tool_local_boxes:
        corners,local_axes=_corners(box);world=corners@path[0][:3,:3].T+path[0][:3,3]
        axes=local_axes@path[0][:3,:3].T
        axes=np.concatenate((axes,other_axes,np.cross(axes[:,None,:],other_axes[None,:,:]).reshape(-1,3)))
        axes=axes[np.linalg.norm(axes,axis=1)>1e-10];axes/=np.linalg.norm(axes,axis=1)[:,None]
        axes=np.concatenate((axes,-axes));gap=(world@axes.T).min(0)-(other@axes.T).max(0)
        allowed=gap>0 if np.max(gap)>0 else gap>=np.max(gap)-1e-10
        radius=float(np.max(np.linalg.norm(corners,axis=1)))
        item=dict(initial_max_gap_m=float(np.max(gap)),initial_allowed_axes=axes[allowed].tolist(),segments=[])
        for index,(x,y) in enumerate(zip(path,path[1:])):
            if clear(x,y,[target],(box,)):continue
            angle=float(np.linalg.norm(Rotation.from_matrix(x[:3,:3].T@y[:3,:3]).as_rotvec()))
            change=axes@(y[:3,3]-x[:3,3])-angle*radius
            item['segments'].append(dict(index=index,angle_rad=angle,translation_m=(y[:3,3]-x[:3,3]).tolist(),
                remaining_faces=int(allowed.sum()),best_margin_m=float(change[allowed].max()) if allowed.any() else None))
            allowed&=change>=-1e-12
        item['passed']=bool(allowed.any());diagnostic['boxes'].append(item)
    return original(scene,poses,targets,held,config)
grasp_observation.check_actual_path=inspect
try:
    result=service.dispatch('plan_skill',{k:v for k,v in source.items() if k!='simulation'})
    diagnostic.update(planned=True)
except Exception as exc:diagnostic.update(planned=False,error_type=type(exc).__name__,error=str(exc))
finally:grasp_observation.check_actual_path=original
diagnostic.update(report=str(a.report),site=str(site),source_sha256=hashlib.sha256((run/'planning-inputs.jsonl').read_bytes()).hexdigest(),
    physical_motion=False,source_observation_simulated=True,checks_unchanged=True)
with a.output.open('x') as f:json.dump(diagnostic,f,indent=2)
print(json.dumps({k:v for k,v in diagnostic.items() if k not in ('poses',)},indent=2))

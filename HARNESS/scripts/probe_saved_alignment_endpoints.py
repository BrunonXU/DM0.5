"""Static endpoint feasibility diagnostic, never an executable motion planner.

Use only registered successful posture seeds and the unchanged F1 joint/wrist
polytope. Optimized endpoints still require original collision/scene checks.
Success here proves neither a connecting path nor actual physical insertion.
"""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from scipy.optimize import minimize, LinearConstraint

from audit_saved_transfer import load_saved
from f1_harness.geometry import transform
from f1_harness.skills import _square_goals, _motion_clear


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    ctx=load_saved(args.run,args.output,dict(transfer_policy='registered_transport'))
    numeric,service,event=(ctx[k] for k in ('numeric','service','event'))
    full=np.asarray(event['full_reference']);held=np.asarray(event['held_reference'])
    scene=service._scene(ctx['observation'],full.tolist(),held.tolist())
    arm=numeric.numerics.arm
    arm.begin('right',full,held)
    ik,pin=arm.ik,arm.pin
    data=ik.model.createData();frame=ik.model.getFrameId(ik.frame)
    A,b=ik.arm_limits.A,ik.arm_limits.b
    signs=numeric.numerics.mapping.signs[7:14]
    seeds=[]
    for name,value in service.posture_prior['postures_deg'].items():
        q=np.deg2rad(value)*signs
        if not any(np.max(abs(q-s[1]))<1e-9 for s in seeds): seeds.append((name,q))
    rows=[]
    def fk(q):
        pin.forwardKinematics(ik.model,data,q);pin.updateFramePlacements(ik.model,data)
        pose=data.oMf[frame];result=np.eye(4)
        result[:3,:3]=pose.rotation;result[:3,3]=pose.translation
        return result
    for skill,height in [('align',scene.transfer_clearance_m),('insert',-scene.insertion_depth_m)]:
        object_goal=transform(scene.base_hole).copy()
        object_goal[:3,3]+=object_goal[:3,2]*height
        for variant,goal in enumerate(_square_goals(object_goal,scene.base_eef,scene.eef_object)):
            def objective(q):
                pose=fk(q)
                linear=pose[:3,3]-goal[:3,3]
                angular=pin.log3(pose[:3,:3]@goal[:3,:3].T)
                return float(linear@linear+(.25**2)*(angular@angular))
            for name,seed in seeds:
                result=minimize(objective,seed,method='SLSQP',
                    constraints=[LinearConstraint(A,np.full(len(b),-np.inf),b)],
                    options=dict(maxiter=250,ftol=1e-13))
                q=result.x;pose=fk(q)
                position=float(np.linalg.norm(pose[:3,3]-goal[:3,3]))
                rotation=float(np.linalg.norm(pin.log3(pose[:3,:3]@goal[:3,:3].T)))
                command=held.copy();command[7:14]=np.rad2deg(q)*signs
                feasible=bool(np.min(b-A@q)>=-1e-9)
                numerical=feasible and position<=numeric.numerics.position_tolerance and rotation<=numeric.numerics.rotation_tolerance
                collision=None;environment=False;gate_error=None
                if numerical:
                    try:
                        numeric.numerics.mapping.check_action(command)
                        collision=numeric.numerics.gate.check_preventive(command,full)
                    except Exception as exc:gate_error=dict(error=str(exc),details=getattr(exc,'details',{}))
                    environment=_motion_clear(scene,pose,pose,scene.eef_object)
                row=dict(skill=skill,variant=variant,seed=name,optimized=result.success,
                    message=result.message,iterations=result.nit,q_deg=command[7:14].tolist(),
                    position_error_m=position,rotation_error_rad=rotation,
                    minimum_limit_margin_rad=float(np.min(b-A@q)),kinematically_feasible=numerical,
                    collision=collision,gate_error=gate_error,environment_clear=environment,
                    accepted_endpoint=bool(numerical and collision is not None and environment))
                rows.append(row)
            print(json.dumps(dict(skill=skill,variant=variant,
                kinematic=sum(r['kinematically_feasible'] for r in rows if r['skill']==skill and r['variant']==variant),
                accepted=sum(r['accepted_endpoint'] for r in rows if r['skill']==skill and r['variant']==variant))),flush=True)
    report=dict(schema='f1-static-endpoint-feasibility-v1',physical_motion=False,
        source_sha256=ctx['source_sha256'],source_script=str(Path(__file__).resolve()),rows=rows,
        source_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        limitations=['Static constrained optimization only; no connected path, output or hardware proof.',
            'Failure of these seeds does not prove mathematical global unreachability.',
            'Collision and current attachment/calibration are model-based.'])
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()

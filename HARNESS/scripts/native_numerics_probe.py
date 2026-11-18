"""Run in an isolated robot /tmp checkout. Only pure model computations, no ROS or outputs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

import numpy as np


def main():
    root=Path(__file__).resolve().parent
    release=Path('/home/user/Bruno/DAgger/releases/f1-rtc-rlt-V47/DAgger')
    infra=Path('/home/user/Bruno/DAgger-runtime/f1-toys-20261016/full-h20/startup-read-v2/infra')
    profile=Path('/home/user/Bruno/F1/F1-4242/runtime/robot-v22.json')
    sys.path[:0]=[str(root),str(release/'src')]
    from f1dagger.config import load_config
    from f1dagger.contract import JointMapping,FULL_NAMES
    from f1dagger.robot_io import CollisionGate
    c,p=load_config(release/'configs/f1_09_hil.json',infra_root=infra,profile_path=profile)
    urdf=infra/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
    mapping=JointMapping(json.loads((infra/'configs/model_mapping.json').read_text()),p,urdf)
    full=np.array([p['default_presets'][1]['joints'][k] for k in FULL_NAMES],dtype=float)
    held=np.r_[full[:14],0.,0.]
    spec=importlib.util.spec_from_file_location('f1dagger.harness_compute',root/'harness_compute.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    gate=CollisionGate(c,mapping,root/'gate',full)
    specification=dict(gate=gate.plan_worker_spec,urdf=str(urdf),ik=c['ik'],
        position_tolerance_m=.0005,rotation_tolerance_rad=.005,max_iterations=3000)
    specification['gate']['runtime']=str(root/'numerics-gate')
    started=time.monotonic();solver=module.HarnessNumerics(specification)
    origin=np.array(solver.forward_pose(full22=full.tolist(),held16=held.tolist()))
    rows=[]
    for distance in (0.,.002):
        goal=origin.copy();goal[2,3]+=distance
        t=time.monotonic()
        targets=solver.solve_path(goals=[goal.tolist()],gripper=None,full22=full.tolist(),held16=held.tolist(),
            scene=dict(attached_radius_m=.01,obstacles=[],uncertainty_m=.001))
        reached=np.array(solver.forward_pose(full22=full.tolist(),held16=targets[-1]))
        rows.append(dict(z_offset_m=distance,waypoints=len(targets),elapsed_s=time.monotonic()-t,
                         final_position_error_m=float(np.linalg.norm(reached[:3,3]-goal[:3,3]))))
        assert all(np.array_equal(t[:7],held[:7]) and t[14]==held[14] for t in targets)
    try:
        solver.solve_path(goals=[goal.tolist()],gripper=None,full22=full.tolist(),held16=held.tolist(),
            scene=dict(attached_radius_m=.01,obstacles=[dict(min=[-10,-10,-10],max=[10,10,10])],uncertainty_m=.001))
    except ValueError as exc:
        assert str(exc)=='actual_IK_path_scene_sweep_blocked'
    else:raise AssertionError('scene collision was accepted')
    result=dict(source_sha256=hashlib.sha256((root/'harness_compute.py').read_bytes()).hexdigest(),scene_collision_rejected=True,robot_actions=0,command_publishers=0,input='profile preset, not live feedback',
                native_python=sys.version.split()[0],release_dependencies=str(release),
                total_s=time.monotonic()-started,cases=rows)
    (root/'result-final.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))


if __name__=='__main__':main()

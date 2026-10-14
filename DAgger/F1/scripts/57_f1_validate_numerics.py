#!/usr/bin/env python3
"""Run F1 numerical acceptance on synthetic poses; never import ROS or publish."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import platform
import sys
import time
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from f1dagger.config import DAGGER_ROOT,load_config,infra_import
from f1dagger.contract import JointMapping,FULL_NAMES,MODEL_NAMES,home_target,sha256
from f1dagger.teleop import F1ArmTarget,HomeMotion
from f1dagger.robot_io import CollisionGate,make_output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--infra-root',required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--config',default=str(DAGGER_ROOT/'configs/f1_09_hil.json'))
    parser.add_argument('--runtime',required=True,type=Path)
    args=parser.parse_args()
    runtime=args.runtime.resolve()
    if runtime.exists():parser.error('use a new validation runtime; existing evidence is immutable')
    runtime.mkdir(parents=True)
    report=dict(ok=False,robot_control_started=False,scope='synthetic_model_only_not_live_shadow_or_motion',
                checked_at_utc=datetime.now(timezone.utc).isoformat(),architecture=platform.machine(),checks={})
    try:
        c,p=load_config(args.config,infra_root=args.infra_root,profile_path=args.profile)
        root=Path(c['_infra_root']);u=root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
        mapping=JointMapping(json.loads((root/'configs/model_mapping.json').read_text()),p,u)
        geometry=infra_import(root,'robot.geometry')
        full=np.array([next(v for v in p['default_presets'] if v['key']=='native-pose2-full-v1')['joints'][k] for k in FULL_NAMES])
        state=np.r_[full[:14],43.,66.]
        checks=report['checks'];checks['fk_max_error']=0.;checks['directions']=[]
        from types import SimpleNamespace
        solve_times=[]
        for side in ('left','right'):
            offset=0 if side=='left' else 7
            for perturb in (np.zeros(22),np.linspace(-1,1,22)):
                a=F1ArmTarget(u,mapping,c['ik']);a.begin(side,full+perturb)
                reference=np.array(geometry.frames(dict(zip(FULL_NAMES,full+perturb)))[a.ik.frame])
                error=float(np.max(np.abs(a.ik.takeover_pose.homogeneous-reference)))
                if error>1e-9:raise ValueError('workbench/reduced FK mismatch')
                checks['fk_max_error']=max(checks['fk_max_error'],error)
            for mode in ('translation','rotation'):
                for axis in range(3):
                    for sign in (-1,1):
                        a=F1ArmTarget(u,mapping,c['ik']);a.begin(side,full)
                        before_pose=a.ik.takeover_pose.copy();target=state.copy()
                        axes=tuple(np.eye(3)[axis]*sign*.2)
                        packet=SimpleNamespace(active_side=side,mode=mode,axes=axes,angular_axes=(0,0,0),speed_scale=1.)
                        for _ in range(10):
                            previous=target.copy();started=time.perf_counter()
                            target=a.target(packet,full,target,.02)
                            solve_times.append((time.perf_counter()-started)*1000)
                            if np.max(np.abs(np.deg2rad(target[:14]-previous[:14])))>c['ik']['safety']['max_joint_step_rad']+1e-9:
                                raise ValueError('IK joint step exceeded')
                        model22=mapping.to_model(np.r_[target[:14],full[14:]])
                        cfg=a.ik.pink.Configuration(a.ik.model,a.ik.model.createData(),model22[offset:offset+7])
                        after=cfg.get_transform_frame_to_world(a.ik.frame)
                        displacement=after.translation-before_pose.translation if mode=='translation' else a.pin.log3(after.rotation@before_pose.rotation.T)
                        if sign*displacement[axis]<=0:raise ValueError(f'{side}/{mode}/{axis}/{sign} direction mismatch')
                        other=slice(7,14) if side=='left' else slice(0,7)
                        np.testing.assert_array_equal(target[other],state[other]);np.testing.assert_array_equal(target[14:],state[14:])
                        checks['directions'].append(dict(side=side,mode=mode,axis=axis,sign=sign,displacement=list(displacement)))
        output=make_output(c);goal=home_target(p,state);goal[0]-=10
        home=HomeMotion(p,state,goal);q=home.propose(.02);home.commit()
        if not 0<state[0]-q[0]<.1:raise ValueError('Home must advance incrementally')
        np.testing.assert_array_equal(q[14:],state[14:])
        output.push(state[:14],1.,enabled=(False,False));output.push(goal[:14],1.02,enabled=(True,False))
        q=output.sample(1.024);output.push(q,1.028,enabled=(False,False))
        np.testing.assert_allclose(output.sample(1.032),q,atol=1e-12)
        checks['home_grippers_and_output_hold']=True
        guard=CollisionGate(c,mapping,runtime/'collision',full)
        checks['nominal_collision']=guard.check(state,full)
        import mujoco
        model=guard.protector._collision_guard._model;data=mujoco.MjData(model)
        for name,value in zip(MODEL_NAMES,mapping.to_model(full)):
            jid=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_JOINT,name);data.qpos[model.jnt_qposadr[jid]]=value
        mujoco.mj_forward(model,data)
        for side in ('left','right'):
            a=F1ArmTarget(u,mapping,c['ik']);a.begin(side,full)
            bid=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_BODY,a.ik.frame)
            np.testing.assert_allclose(data.xpos[bid],a.ik.takeover_pose.translation,atol=2e-6)
        checks['collision_fk_matches']=True
        unsafe=np.array([149.021775,39.864277,148.495636,32.261501,-100.341137,43.935292,41.535349,-79.079957,76.567320,130.018930,-83.615231,9.626982,-51.421583,14.982914,0,0])
        try:guard.check(unsafe,full)
        except RuntimeError as exc:checks['collision_rejection']=str(exc)
        else:raise ValueError('known arm/head collision was not rejected')
        report.update(ok=True,infra_binding=c['_infra_manifest_sha256'],config_sha256=c['_config_sha256'],
            profile_sha256=c['_profile_sha256'],solve_ms=dict(median=float(np.median(solve_times)),p95=float(np.percentile(solve_times,95)),maximum=max(solve_times)))
    except BaseException as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        (runtime/'report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(ok=report['ok'],report=str(runtime/'report.json'),solve_ms=report['solve_ms'])))

if __name__=='__main__':main()

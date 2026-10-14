#!/usr/bin/env python3
"""F1-local one-shot right-arm clearance. No sender, policy or data recording.

Default is read-only preflight. --execute still requires local terminal Enter.
Uses the manifest-bound control library, never launches its HIL application.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time
import numpy as np
from f1dagger.config import load_config, infra_import
from f1dagger.contract import JointMapping, ARM_NAMES, home_target
from f1dagger.identity import runtime_identity
from f1dagger.robot_io import ControlLock, CollisionGate, F1Owner, make_output
from f1dagger.observation import RosObservationSource
from f1dagger.teleop import F1ArmTarget
from f1dagger.clearance_home import FeedbackAlignedClearanceHome as ClearanceHomeMotion


def preview(config, profile, mapping, urdf, full, state, gate):
    """Numerical path check only; synthetic tracking is not physical arrival."""
    arm = F1ArmTarget(urdf, mapping, config['ik'])
    motion = ClearanceHomeMotion(profile, state, home_target(profile, state), arm, full, 0)
    output = make_output(config)
    output.planner.set_limits(mapping.limits)
    output.push(state[:14], 0., enabled=(False, False))
    q = state.copy(); full = full.copy(); stages = []; rotation = 0.
    for tick in range(1, 15001):
        now = tick*4_000_000
        full[:14] = q[:14]
        if tick % 5 == 0:
            target = motion.propose(.02, full, q, q, (now,)*16, now)
            gate.check(target, full)
            output.push(target[:14], now/1e9, enabled=motion.enabled)
            motion.commit()
        q = np.r_[output.sample(now/1e9), state[14:]]
        if output.fault:raise RuntimeError('preview output fault: '+str(output.fault))
        mapping.check_action(q); gate.check(q, full)
        if not stages or stages[-1] != motion.stage:stages.append(motion.stage)
        if motion.stage != 'return':
            rotation = max(rotation, float(np.rad2deg(np.linalg.norm(motion.pin.log3(motion.pose(q[7:14]).rotation@motion.rotation.T)))))
        if motion.done:
            return dict(stages=stages, simulated_s=now/1e9, max_rotation_deg=rotation, physical_arrival_verified=False)
    raise RuntimeError('preview did not finish; no motion permitted')


def run_motion(config, profile, mapping, urdf, source, transport, gate, stop, emit):
    # Shadow owner supplies the same guarded planner, but never publishes arms
    # or grippers. Only the explicit right-arm dictionary below reaches ROS.
    owner = F1Owner(transport, source, mapping, make_output(config), gate, powered=False)
    transport.assert_exclusive('joint_command')
    transport.prepare('joint_command')
    full = owner.initialize(time.monotonic())
    arm = F1ArmTarget(urdf, mapping, config['ik'])
    arm.warmup(full, owner.last_submitted)
    full = owner.initialize(time.monotonic())
    motion = ClearanceHomeMotion(profile, owner.last_submitted, home_target(profile, owner.last_submitted), arm, full, time.monotonic_ns())
    fixed = full.copy(); stage = None; target_at = None; next_target = time.monotonic(); next_sample = next_target
    try:
        while not stop():
            now = time.monotonic()
            full, measured, stamps = source.feedback()
            if np.max(np.abs(np.r_[full[:7]-fixed[:7], full[14:]-fixed[14:]])) > .5:
                raise RuntimeError('inactive arm/body/head moved; stop reset')
            if now >= next_target:
                dt = .02 if target_at is None else now-target_at
                target = motion.propose(dt, full, owner.last_submitted, measured, stamps, int(now*1e9))
                gate.check(target, full)
                if stop():break
                owner.approve(target, now, enabled=motion.enabled); motion.commit()
                target_at = now; next_target = now+.02
                if stage != motion.stage:
                    stage = motion.stage; emit('stage', stage=stage, right_measured_deg=measured[7:14].tolist())
                if motion.done:
                    emit('completed', right_measured_deg=measured[7:14].tolist(), feedback_verified=True)
                    return
            dispatch = time.monotonic()
            transport.assert_exclusive_cached('joint_command')
            if stop():break
            command, measured, _ = owner.publish(dispatch)
            if stop():break
            transport.publish('joint_command', dict(zip(ARM_NAMES[7:14], command[7:14])))
            if owner.sequence % 25 == 0:
                emit('sample', stage=motion.stage, right_submitted_deg=command[7:14].tolist(), right_measured_deg=measured[7:14].tolist(), verbose=False)
            next_sample += .004
            time.sleep(max(0., next_sample-time.monotonic()))
            if next_sample < time.monotonic()-.004:next_sample=time.monotonic()
        emit('cancelled', reason='local_interrupt')
    finally:
        owner.hold(time.monotonic())
        transport.release('joint_command')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--runtime', required=True)
    parser.add_argument('--execute', action='store_true')
    args=parser.parse_args()
    if args.execute and not sys.stdin.isatty():raise RuntimeError('execute requires a local interactive terminal')
    c,p=load_config(args.config)
    identity=runtime_identity(c, powered=True)
    for key in ('motion_enabled','joints_enabled','limits_verified','control_contract_verified','gripper_verified'):
        if p.get(key) is not True:raise RuntimeError('profile gate closed: '+key)
    if c['ik']['verified'] is not True:raise RuntimeError('IK not verified')
    check=infra_import(c['_infra_root'],'robot.runtime_contract').verify_runtime_contract(p)
    if not check['ok']:raise RuntimeError('runtime contract failed: '+json.dumps(check))
    runtime=Path(args.runtime).resolve()
    if Path('/home/user/Bruno') not in runtime.parents:raise RuntimeError('runtime must be under /home/user/Bruno')
    run=runtime/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ');run.mkdir(parents=True)
    print('日志：'+str(run/'events.jsonl'), flush=True)
    stopped=[False];transport=None; lock=ControlLock(os.environ.get('F1_CONTROL_LOCK'))
    def emit(event, verbose=True, **fields):
        row=dict(event=event, monotonic_ns=time.monotonic_ns(), **fields)
        with (run/'events.jsonl').open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        if verbose:print(json.dumps(row,ensure_ascii=False),flush=True)
    phase = 'preflight'
    try:
        lock.acquire()
        adapter=infra_import(c['_infra_root'],'robot.adapter')
        store=adapter.StateStore();transport=adapter.RosTransport(p,store);transport.start()
        source=RosObservationSource(store,p,c);deadline=time.monotonic()+5
        while True:
            try:full,state,_=source.feedback();transport.assert_exclusive('joint_command');break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(.05)
        root=Path(c['_infra_root']);urdf=root/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'
        mapping=JointMapping(json.loads((root/'configs/model_mapping.json').read_text()),p,urdf)
        mapping.check_action(state)
        emit('preflight', identity=identity, full_measured_deg=full.tolist(), right_goal_deg=home_target(p,state)[7:14].tolist(), commands_sent=False)
        gate=CollisionGate(c,mapping,run/'model_runtime',full)
        checked=full.copy()
        phase = 'preview'
        result=preview(c,p,mapping,urdf,full,state,gate)
        emit('preview_passed', **result, commands_sent=False)
        if not args.execute:
            print('只读预检完成，没有发送动作。',flush=True);return
        phase = 'waiting_enter'
        input('将只动右臂：上抬 6cm → 向机器人右侧 10cm → 积木demo-V0；避让允许少量姿态变化，不增加独立转腕动作。确认净空后按 Enter；Ctrl+C 取消：')
        full,_,_=source.feedback()
        if np.max(np.abs(full-checked))>p['joint_tolerance_deg']:
            raise RuntimeError('pose changed after preview; rerun preflight')
        for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stopped.__setitem__(0,True))
        emit('local_enter_confirmed')
        phase = 'execution'
        run_motion(c,p,mapping,urdf,source,transport,gate,lambda:stopped[0],emit)
    except BaseException as exc:
        if phase == 'preview':
            emit('preview_failed', error=str(exc) or type(exc).__name__, commands_sent=False)
            print('轨迹预演未通过，尚未发送机器人动作；这不是实机反馈超时。', flush=True)
        emit('stopped', phase=phase, error=str(exc) or type(exc).__name__)
        raise
    finally:
        if transport is not None:transport.close()
        lock.close()

if __name__=='__main__':main()

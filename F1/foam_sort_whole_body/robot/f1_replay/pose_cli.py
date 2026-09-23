"""Save, prepare and restore F1 joint poses. Only onsite actions publish."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import select
import signal
import socket
import sys
import time
from types import SimpleNamespace
import uuid

from .bundle import ARM_NAMES, BODY_NAMES, load_bundle
from .cli import Journal, read_json, runtime_lock, check_execution_host
from .pose_backend import PoseBackend, ArmsOnlyBackend, validate_body_profile
from .pose_segments import segment_end_indices, wait_pose_segment
from .pose_model import (JOINT_NAMES, SIDES, capture_pose, validate_pose, compare_to_start,
                         start_target, restore_target, plan_transition)
from .runtime import (ReplayError, ReplayInterrupted, finite_number, profile_issues, base_stopped,
                      check_command_peers, check_command_activity, check_controller_parameters, run_preparation_schedule, stop_motion)

ROOT = Path('/home/user/Bruno/F1/foam_sort_whole_body')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, data):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def create_run():
    path = ROOT/'runtime/pose_runs'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8])
    if not path.resolve().is_relative_to(Path('/home/user/Bruno')):
        raise ReplayError('姿态日志目录必须在Bruno内')
    path.mkdir(parents=True, exist_ok=False)
    return path


def live_pose(backend, profile, profile_path, bundle):
    return capture_pose(backend.snapshot(), profile, now_ns=time.monotonic_ns(),
                        host=socket.gethostname(), profile_sha256=digest(profile_path),
                        source_bundle={'path': str(bundle.path), 'manifest_sha256': bundle.sha256})


def feedback_ok(pose, profile):
    base = pose['base']
    if not base_stopped(pose['raw_snapshot'], profile, pose['captured_monotonic_ns']):
        raise ReplayError('准备/恢复期间底盘必须保持静止')
    if (base.get('is_soft_auto') is not False or base.get('is_hard_auto') is not profile['expected_hard_auto']
            or base.get('is_parking_mode') is not False or base.get('is_avoid') is not False):
        raise ReplayError('底盘模式与核定配置不一致')


def target_errors(pose, joints, grippers):
    return {'joints': {n: joints[n]-pose['joint_positions_deg'][n] for n in JOINT_NAMES},
            'grippers': {s: grippers[s]-pose['grippers'][s] for s in SIDES}}


def target_reached(errors, profile):
    return (all(abs(errors['joints'][n]) <= profile['start_arm_tolerance_deg'] for n in ARM_NAMES)
            and all(abs(errors['joints'][n]) <= profile['body_tolerance_deg'] for n in BODY_NAMES)
            and all(abs(v) <= profile['start_gripper_tolerance'] for v in errors['grippers'].values()))


def check_tracking(pose, command, policy, *, defer_arms=False):
    errors = target_errors(pose, command['joint_positions_deg'], command['grippers'])
    for name, error in errors['joints'].items():
        if defer_arms and name in ARM_NAMES:
            continue
        bound = policy['arm_tracking_tolerance_deg'] if name in ARM_NAMES else policy['body_tracking_tolerance_deg']
        if abs(error) > bound:
            raise ReplayError(f'{name}跟随误差{error:.3f}°超过准备动作策略{bound}°')
    if any(abs(v) > policy['gripper_tracking_tolerance'] for v in errors['grippers'].values()):
        raise ReplayError('准备动作夹爪跟随误差过大')
    return errors


def wait_arm_tracking(read_errors, hold, log, tolerance, timeout_s, hz,
                      now=time.monotonic, sleep=time.sleep):
    """Keep the already-issued target while waiting; never advance a stuck arm."""
    errors=read_errors()
    worst=lambda values:max((abs(values['joints'][n]),n) for n in ARM_NAMES)
    if worst(errors)[0]<=tolerance:return False
    started=now()
    log('arm_tracking_wait_start',{'errors':errors,'timeout_s':timeout_s})
    while worst(errors)[0]>tolerance:
        if now()-started>=timeout_s:
            error,name=worst(errors)
            raise ReplayError(f'{name}复位目标已暂停推进并等待{timeout_s:g}秒，仍差{error:.3f}°；未继续累积目标')
        hold()
        sleep(1/hz)
        # Callback also checks fresh feedback, cancellation, and ownership.
        errors=read_errors()
    log('arm_tracking_wait_resumed',{'elapsed_s':now()-started,'errors':errors})
    return True


def body_sample_stride(policy, sample_hz):
    """Only explicitly selected candidate plans decouple body and other outputs."""
    mode = policy.get('sampling_policy', 'legacy_combined')
    if mode == 'legacy_combined':
        if not finite_number(sample_hz) or sample_hz != 30:
            raise ReplayError('旧组合起姿保持30Hz；身体升频必须显式选择分频候选')
        return None
    auxiliary = policy.get('auxiliary_hz')
    if (mode != 'body_only_interleaved_v1' or not finite_number(sample_hz)
            or not finite_number(auxiliary) or sample_hz != 200 or auxiliary != 25):
        raise ReplayError('分频候选仅定义身体200Hz、其他输出25Hz；尚非硬件验收')
    return 8


def execute_plan(backend, profile, profile_path, bundle, start, plan, policy, output):
    """Do not create publishers until feedback/ownership/onsite confirmation pass."""
    check_execution_host(profile)
    log = Journal(output/'events.jsonl')
    result = {'target_reached': False, 'motion_started': False, 'output': str(output)}
    interrupted = False
    previous_handlers = {}
    last_command = plan['frames'][0]
    last_peer_check = 0
    split_stride = body_sample_stride(policy, plan['rate_hz'])
    sent_samples = 0
    tracking_wait_s=policy.get('arm_tracking_wait_s',0)
    if tracking_wait_s and policy.get('execution_scope')!='arms_only':
        raise ReplayError('跟随等待仅用于双臂home复位，不改变身体200Hz准备调度')

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    def tick(check_start=False):
        nonlocal last_peer_check
        if interrupted:
            raise ReplayInterrupted('操作者停止姿态调整')
        backend.poll(0.0)
        check_command_activity(backend, profile)
        pose = live_pose(backend, profile, profile_path, bundle)
        feedback_ok(pose, profile)
        check_tracking(pose, last_command, policy,
                       defer_arms=bool(tracking_wait_s and result['motion_started'] and not check_start))
        if check_start:
            errors = target_errors(pose, start['joint_positions_deg'], start['grippers'])
            if not target_reached(errors, profile):
                raise ReplayError('确认期间姿态改变，请重新生成准备计划')
        now = time.monotonic_ns()
        if now-last_peer_check >= 500_000_000:
            check_command_peers(backend, profile, enabled=backend.commands_enabled)
            last_peer_check = now
        return pose

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupt)
        params = backend.read_controller_parameters()
        check_controller_parameters(params['parameters'], profile)
        backend.check_body_parameters(params['parameters'])
        check_command_peers(backend, profile)
        log('setup_start', {'backup': str(output/'before_move_pose.json'), 'profile': profile,
                           'plan_file': 'plan.json', 'parameters': params,
                           'baseline_not_overwritten': True, 'base_velocity_command': [0,0,0]})
        tick(check_start=True)
        if policy.get('execution_scope') == 'arms_only':
            print(f"将恢复双臂14关节，约{plan['duration_s']:.1f}秒；底盘持续零速，不发送身体或夹爪命令。")
        else:
            print(f"将慢速调整双臂、身体5轴和双夹爪，约{plan['duration_s']:.1f}秒；底盘持续零速。")
        print('这是关节插补，不含碰撞规划。现场确认全程空间畅通，按Enter开始；Ctrl-C取消：', flush=True)
        while True:
            tick(check_start=True)
            readable, _, _ = select.select([sys.stdin], [], [], .05)
            if readable:
                if not sys.stdin.readline():
                    raise ReplayInterrupted('现场终端已关闭')
                break
        for remaining in (3,2,1):
            print(f'{remaining}…', flush=True)
            until = time.monotonic()+1
            while time.monotonic()<until:
                tick(check_start=True)
                time.sleep(.01)
        check_command_peers(backend, profile)
        # If publisher creation partially fails, finally must still stop safely.
        result['motion_started'] = True
        backend.enable_commands()
        check_command_peers(backend, profile, enabled=True)

        def send(values):
            nonlocal last_command, sent_samples
            joints = dict(zip(JOINT_NAMES, values[:19]))
            grips = dict(zip(SIDES, values[19:21]))
            if split_stride is None:
                sent = backend.publish_pose(joints, grips)
                last_command = {'joint_positions_deg': joints, 'grippers': grips}
            else:
                auxiliary = sent_samples % split_stride == 0
                sent = backend.publish_body_sample(joints, grips, include_auxiliary=auxiliary)
                if auxiliary:
                    last_command = {'joint_positions_deg': joints, 'grippers': grips}
                else:
                    last_command['joint_positions_deg'].update({n:joints[n] for n in BODY_NAMES})
            sent_samples += 1
            log('feedback', {'snapshot': backend.snapshot()})
            return sent

        planned = [SimpleNamespace(time_ns=f['time_ns'], source_time_ns=f['time_ns'],
                   action=tuple(f['joint_positions_deg'][n] for n in JOINT_NAMES)+tuple(f['grippers'][s] for s in SIDES))
                   for f in plan['frames']]
        schedule = SimpleNamespace(frames=planned,
                    duration_ns=planned[-1].time_ns+round(1e9/plan['rate_hz']))
        segment_ends = (segment_end_indices(planned, split_stride or 1)
                        if policy.get('segmented_preparation') else [])
        segment_number = 0

        def segment_gate():
            nonlocal segment_number
            if segment_number >= len(segment_ends) or sent_samples != segment_ends[segment_number]+1:
                return False
            log('segment_wait', {'segment':segment_number+1,'total':len(segment_ends),
                                'endpoint':last_command})
            def errors():
                return target_errors(tick(),last_command['joint_positions_deg'],last_command['grippers'])
            def hold(index):
                if split_stride is None:
                    sent=backend.publish_pose(last_command['joint_positions_deg'],last_command['grippers'])
                else:
                    sent=backend.publish_body_sample(last_command['joint_positions_deg'],last_command['grippers'],
                                                     include_auxiliary=(index+1)%split_stride==0)
                log('segment_hold', {'publication':sent,'snapshot':backend.snapshot()})
            wait_pose_segment(errors,hold,log,hz=plan['rate_hz'],
                              body_tolerance=min(1.,profile['body_tolerance_deg']),
                              gripper_tolerance=min(5.,profile['start_gripper_tolerance']),
                              max_stall_s=profile['feedback_max_age_s'])
            segment_number+=1
            return True

        def setup_log(event, fields):
            log('pose_'+event, {'time_reference': 'generated_preparation_plan_not_demonstration',
                'command_layout': list(JOINT_NAMES)+list(SIDES), **fields})

        def wait_tracking():
            def errors():
                return target_errors(tick(),last_command['joint_positions_deg'],last_command['grippers'])
            def hold():
                log('arm_tracking_hold',{'publication':backend.publish_pose(
                    last_command['joint_positions_deg'],last_command['grippers'])})
            return wait_arm_tracking(errors,hold,log,policy['arm_tracking_tolerance_deg'],
                                     tracking_wait_s,profile['stop_publish_hz'])

        run_preparation_schedule(schedule, send, tick, setup_log, profile['feedback_max_age_s'],
                     stopped=lambda: interrupted, max_command_hz=profile['max_command_hz'],
                     before_frame=segment_gate if segment_ends else wait_tracking if tracking_wait_s else None)
        if segment_ends:
            segment_gate()
            result['segments_completed']=segment_number
        # The last sample is on the auxiliary cadence because the planner pads
        # by extending duration; no immediate extra batch/catch-up is sent here.
        end = time.monotonic()+policy['settle_timeout_s']
        stable_since = None
        while time.monotonic()<end:
            pose = tick()
            errors = target_errors(pose, last_command['joint_positions_deg'], last_command['grippers'])
            at_target = target_reached(errors, profile)
            if plan.get('target_source', {}).get('kind') == 'recorded_hal_start':
                at_target = at_target and compare_to_start(pose, bundle.frames[0], profile)['all_within_tolerance']
            if at_target:
                stable_since = stable_since or time.monotonic()
                if time.monotonic()-stable_since >= policy['settle_stable_s']:
                    result['target_reached'] = True
                    result['final_errors'] = errors
                    break
            else:
                stable_since = None
            backend.publish_pose(last_command['joint_positions_deg'], last_command['grippers'])
            log('settle_feedback', {'errors': errors, 'snapshot': pose['raw_snapshot']})
            time.sleep(1.0/profile['stop_publish_hz'])
        if not result['target_reached']:
            raise ReplayError('准备轨迹已发完，但目标姿态未稳定到达；不能开始replay')
        save_json(output/'arrival_pose.json', live_pose(backend, profile, profile_path, bundle))
    except (Exception, KeyboardInterrupt) as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        log('error', {'error': result['error']})
    finally:
        if result['motion_started']:
            try:
                result['stop'] = stop_motion(backend, profile, log)
                result['stop']['hold_scope'] = ('arms14_fresh_feedback' if policy.get('execution_scope') == 'arms_only'
                                                else 'arms14_and_body5_fresh_feedback')
            except Exception as exc:
                result['stop_error'] = f'{type(exc).__name__}: {exc}'
        for sig, old in previous_handlers.items():
            signal.signal(sig, old)
        try:
            after = live_pose(backend, profile, profile_path, bundle)
            save_json(output/'after_pose.json', after)
            if (result['target_reached'] and plan.get('target_source', {}).get('kind') == 'recorded_hal_start'
                    and not compare_to_start(after, bundle.frames[0], profile)['all_within_tolerance']):
                result['target_reached'] = False
                result['error'] = '停止保持后的反馈未同时满足HAL起姿与首动作；请查看after_pose，不开始replay'
        except Exception as exc:
            result['after_pose_error'] = str(exc)
        log('setup_end', result)
        log.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='F1姿态备份、慢速准备与恢复；默认只读status')
    parser.add_argument('mode', choices=('status','capture','plan-start','plan-restore','to-start','restore'), nargs='?', default='status')
    parser.add_argument('--config', required=True)
    parser.add_argument('--arms-only', action='store_true')
    parser.add_argument('--segmented', action='store_true')
    parser.add_argument('--arm-tracking-wait-s',type=float,default=0.)
    args = parser.parse_args(argv)
    if args.segmented and (args.mode not in ('plan-start','to-start') or args.arms_only):
        parser.error('--segmented requires full-body plan-start or to-start')
    if (not finite_number(args.arm_tracking_wait_s) or not 0<=args.arm_tracking_wait_s<=30
            or (args.arm_tracking_wait_s and not args.arms_only)):
        parser.error('--arm-tracking-wait-s must be 0..30 and requires --arms-only')
    if args.arms_only and args.mode not in ('restore', 'plan-restore'):
        parser.error('--arms-only requires restore or plan-restore')
    backend = None
    try:
        config = read_json(args.config)
        profile_path = Path(config['profile'])
        profile = read_json(profile_path)
        issues = profile_issues(profile)
        if issues:
            raise ReplayError('；'.join(issues))
        validate_body_profile(profile)
        if socket.gethostname()!=profile['expected_host']:
            raise ReplayError('姿态工具须在F1主机运行')
        if (profile.get('expected_machine_id') and
                Path('/etc/machine-id').read_text().strip()!=profile['expected_machine_id']):
            raise ReplayError('姿态配置与本机machine-id不同')
        baseline_path = None
        baseline = None
        if config.get('baseline_pose'):
            baseline_path = Path(config['baseline_pose'])
            if digest(baseline_path)!=config['baseline_pose_sha256']:
                raise ReplayError('原始姿态备份SHA256不匹配；不自动选用其他备份')
            baseline = validate_pose(read_json(baseline_path))
            if baseline['host']!=profile['expected_host']:
                raise ReplayError('姿态备份来自其他机器人主机')
        elif args.mode in ('restore','plan-restore'):
            raise ReplayError('未绑定本机恢复备份；to-start使用示教起点，不能冒充restore')
        bundle = load_bundle(config['bundle'])
        if config.get('bundle_sha256') and bundle.sha256 != config['bundle_sha256']:
            raise ReplayError('起姿来源bundle SHA256不匹配')
        backend = (ArmsOnlyBackend if args.arms_only else PoseBackend)(profile)
        backend.open()
        until = time.monotonic()+3
        while time.monotonic()<until:
            backend.poll(.05)
        current = live_pose(backend, profile, profile_path, bundle)
        output = create_run()
        save_json(output/'before_move_pose.json', current)
        comparison = compare_to_start(current, bundle.frames[0], profile)
        target = (restore_target(baseline, profile) if args.mode in ('restore','plan-restore')
                  else start_target(bundle.frames[0], profile,
                                    policy=config.get('start_pose_policy', 'recorded_hal')))
        policy = dict(config['motion_policy'])
        policy['segmented_preparation'] = args.segmented
        if args.arms_only:
            # Validate the immutable saved HAL evidence before deriving a scoped target.
            for name in BODY_NAMES:
                target['joint_positions_deg'][name] = current['joint_positions_deg'][name]
            target['grippers'] = dict(current['grippers'])
            target['source']['restored_joint_groups'] = ['arms14']
            policy.update(sampling_policy='legacy_combined', sample_hz=30,
                          auxiliary_hz=30, execution_scope='arms_only',
                          arm_tracking_wait_s=args.arm_tracking_wait_s)
        backend.validate_pose_target(target['joint_positions_deg'], target['grippers'])
        sample_hz = policy.get('sample_hz', 30.0)
        stride = body_sample_stride(policy, sample_hz)
        plan = plan_transition(current, target, profile,
                rate_hz=sample_hz,
                arm_rate_deg_s=policy['arm_rate_deg_s'], body_rate_deg_s=policy['body_rate_deg_s'],
                gripper_rate_s=policy['gripper_rate_s'],
                body_acceleration_deg_s2=policy.get('body_acceleration_deg_s2'),
                body_jerk_deg_s3=policy.get('body_jerk_deg_s3'),
                sample_stride=stride or 1)
        plan['sampling_policy'] = policy.get('sampling_policy', 'legacy_combined')
        plan['auxiliary_hz'] = policy.get('auxiliary_hz', sample_hz)
        for frame in plan['frames']:
            backend.validate_pose_target(frame['joint_positions_deg'], frame['grippers'])
        plan['body_limits_verified'] = True
        plan['body_adapter_required'] = False
        plan['body_adapter'] = 'named_joint_state_via_verified_motor_ctl'
        if args.segmented:
            segment_frames=[SimpleNamespace(action=tuple(f['joint_positions_deg'][n] for n in JOINT_NAMES)
                            +tuple(f['grippers'][s] for s in SIDES)) for f in plan['frames']]
            plan['segment_end_indices']=segment_end_indices(segment_frames,stride or 1)
            plan['segment_policy']={'arm_step_deg':1.,'body_step_deg':.25,'gripper_step':5.,
                                    'arm_arrival_deg':.25,'wait_timeout_s':10.}
        save_json(output/'plan.json', plan)
        save_json(output/'comparison.json', comparison)
        blockers = []
        for check in (lambda: feedback_ok(current, profile), lambda: check_command_peers(backend, profile)):
            try:check()
            except Exception as exc:blockers.append(str(exc))
        try:
            params = backend.read_controller_parameters()
            check_controller_parameters(params['parameters'], profile)
            backend.check_body_parameters(params['parameters'])
        except Exception as exc:
            blockers.append(str(exc))
        report = {'mode': args.mode, 'output': str(output), 'baseline_pose': str(baseline_path) if baseline_path else None,
                  'baseline_sha256': config.get('baseline_pose_sha256'), 'backup_saved': True,
                  'at_demonstration_start': comparison['all_within_tolerance'],
                  'differences': [f for f in comparison['fields'] if not f['within_tolerance']],
                  'duration_s': plan['duration_s'], 'blockers': blockers,
                  'motion_started': False, 'control_publishers_created': False,
                  'start_pose_selection': target.get('start_pose_selection'),
                  'scope': 'arms14_body5_grippers2; head_saved_unchanged; no_base_position_restore'}
        if args.segmented:
            report['segment_count']=len(plan['segment_end_indices'])
            report['segment_policy']=plan['segment_policy']
        if args.arms_only:
            report['scope'] = 'arms14_and_base_zero; no_body_no_gripper_commands'
        if profile.get('command_ownership_policy') == 'observed_idle_v1':
            report['command_activity'] = backend.command_activity()
            report['command_peers'] = backend.command_peers()
        if args.mode in ('to-start','restore'):
            if blockers:
                report['error'] = '现场条件未满足，尚未发送任何动作'
            else:
                with runtime_lock(output):
                    report.update(execute_plan(backend, profile, profile_path, bundle, current, plan, policy, output))
                report['control_publishers_created'] = backend.commands_enabled
        save_json(output/'report.json', report)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if args.mode in ('to-start','restore'):
            return 0 if (report.get('target_reached') and report.get('stop',{}).get('zero_velocity_feedback_confirmed')
                         and report.get('stop',{}).get('fresh_arm_hold_published')) else 1
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f'F1 pose: {type(exc).__name__}: {exc}',file=sys.stderr)
        return 2
    finally:
        if backend is not None:
            backend.close()


if __name__=='__main__':
    raise SystemExit(main())

"""Bounded, original-time publication replay under the console's action lease.

No source filtering/resampling, background resume, or implicit initial movement.
"""
from bisect import bisect_left, bisect_right
import json
import math
import os
from pathlib import Path
import uuid

from .replay_data import ARMS, CONTEXT, KEYS, Segment, number
from .pose_action import feedback, arrival_feedback
from .replay_compare import compare


def limits(profile):
    maximum = number(profile.get('max_replay_task_s', 600), '回放总时限')
    gap = number(profile.get('body_max_command_gap_s', .025), '命令节拍预算')
    if not 1 <= maximum <= 1800 or not 0 < gap <= .025:
        raise ValueError('回放时限/节拍配置无效')
    return maximum, gap


def check_values(profile, q):
    for name, value in zip(ARMS, q[:14]):
        low, high = profile['joint_limits_deg'][name]
        if not low <= value <= high:
            raise ValueError('回放目标越界: ' + name)
    for offset in (0, 7):
        if 1.025*abs(q[offset+5])+abs(q[offset+6]) > 110.5+1e-7:
            raise ValueError('回放目标超过 F1 耦合腕部限位')
    if any(not 0 <= v <= 100 for v in q[14:]):
        raise ValueError('回放夹爪目标超出 0–100')


def validate(profile, segment):
    if not isinstance(segment, Segment):
        raise ValueError('回放只能执行后端已核对的录制')
    rec = segment.recording
    if rec.robot != profile.get('id', profile.get('robot_id')):
        raise ValueError('录制机器人身份与本机不符')
    if not rec.profile_sha256 or rec.profile_sha256 != profile.get('profile_sha256'):
        raise ValueError('录制绑定的机器人配置与当前配置不一致，不能自动套用')
    maximum, gap = limits(profile)
    if segment.duration + profile['settle_timeout_s'] > maximum:
        raise ValueError('所选片段超过回放总任务预算，请缩短区间')
    rows = segment.commands
    max_speed, max_acceleration = 0., 0.
    previous_velocity, previous_dt = None, None
    for i, row in enumerate(rows):
        check_values(profile, row.values)
        if not i:
            continue
        dt = (row.publications[0][1]-rows[i-1].publications[0][1])/1e9
        if dt > gap:
            raise ValueError('原始手臂命令间隔超过现有节拍预算；不会补帧或跳过')
        velocity = [(x-y)/dt for x, y in zip(row.values[:14], rows[i-1].values[:14])]
        max_speed = max(max_speed, *map(abs, velocity))
        if previous_velocity is not None:
            max_acceleration = max(max_acceleration, *(abs(v-u)/((dt+previous_dt)/2)
                                   for v, u in zip(velocity, previous_velocity)))
        previous_velocity, previous_dt = velocity, dt
    # These are derivatives of publication samples, NOT measured motor speed
    # or the original planner's acceleration. Publication jitter/hold transitions
    # magnify them. The profile's manual-pose generation caps must not be applied
    # to this different execution contract as a new arbitrary replay threshold.
    for _, context in segment.observations:
        if any(abs(x-y) > profile['joint_tolerance_deg'] for x, y in zip(context, segment.context)):
            raise ValueError('片段内身体或头部发生移动，16D 回放不能静默忽略')
    return dict(max_discrete_command_speed_deg_s=max_speed,
                max_discrete_command_acceleration_deg_s2=max_acceleration,
                basis='publication_samples_diagnostic_only_not_motor_derivatives')


def live_check(a, segment, *, start=False, cached=False):
    if not all(a.profile.get(k) for k in ('joints_enabled', 'limits_verified', 'gripper_verified')):
        raise ValueError('本机双臂/夹爪控制合同尚未启用')
    guard = a.transport.assert_exclusive_cached if cached else a.transport.assert_exclusive
    for key in KEYS:
        guard(key)
    snap = a._common()
    actual = []
    for name in ARMS:
        item = a._fresh(snap['joints'].get(name), name)
        actual.append(number(item['position'], name))
    for side in ('left', 'right'):
        item = a._fresh(snap['grippers'].get(side), side)
        actual.append(number(item['position'], side))
    check_values(a.profile, actual)
    mismatches = []
    for name, target in zip(CONTEXT, segment.context):
        value = number(a._fresh(snap['joints'].get(name), name)['position'], name)
        if abs(value-target) > a.profile['joint_tolerance_deg']:
            mismatches.append(name)
    if mismatches:
        raise ValueError('身体/头部与录制上下文不一致: ' + ', '.join(mismatches))
    if start:
        for i, (value, target) in enumerate(zip(actual, segment.commands[0].values)):
            tolerance = a.profile['joint_tolerance_deg'] if i < 14 else a.profile['gripper_tolerance']
            if abs(value-target) > tolerance:
                mismatches.append(ARMS[i] if i < 14 else ('left', 'right')[i-14]+'夹爪')
        if mismatches:
            raise ValueError('回放起点尚未对齐: ' + ', '.join(mismatches))
    return snap, actual


def review(a, segment):
    metrics = validate(a.profile, segment)
    live_check(a, segment, start=True)
    return metrics



def prepare_schedule(segment):
    # All allocation/sorting happens before the playback clock starts.
    schedule = []
    for i, row in enumerate(segment.commands):
        for key, ns in row.publications:
            value = dict(zip(ARMS, row.values[:14])) if key == KEYS[0] else row.values[14 if key == KEYS[1] else 15]
            schedule.append(((ns-segment.begin)/1e9, key, i, value))
    for key in KEYS[1:]:
        if key not in dict(segment.commands[0].publications):
            schedule.append((0., key, 0, segment.commands[0].values[14 if key == KEYS[1] else 15]))
    return sorted(schedule, key=lambda x: x[0])

def execute(a, spec, cancel, on_progress=None):
    segment = spec.get('_segment')
    validate(a.profile, segment)
    schedule = prepare_schedule(segment)
    if not a._action_lock.acquire(blocking=False):
        raise ValueError('已有动作正在执行')
    a._active_cancel = cancel
    prepared, sent, held = [], set(), []
    samples, output_count, last_progress = [], 0, -math.inf
    max_error, max_late = 0., 0.
    dispatch_lateness = []
    start = None
    result = dict(status='cancelled', commands_sent=False, feedback_verified=False,
                  physical_stop_confirmed=False, replay=segment.summary())
    rows = segment.commands
    times = [(r.publications[0][1]-segment.begin)/1e9 for r in rows]
    maximum, gap = limits(a.profile)
    final = dict(targets=dict(zip(ARMS, rows[-1].values[:14])),
                 grippers=dict(zip(('left', 'right'), rows[-1].values[14:])),
                 parts=[dict(id=p, label=p) for p in ('left_arm', 'right_arm', 'left_gripper', 'right_gripper')])

    def hold():
        for key in tuple(sent):
            try:
                a.transport.assert_exclusive(key)
                snap = a._common()
                if key == KEYS[0]:
                    q = [number(a._fresh(snap['joints'].get(n), n)['position'], n) for n in ARMS]
                    check_values(a.profile, q+[0, 0])
                    value = dict(zip(ARMS, q))
                else:
                    side = 'left' if key == KEYS[1] else 'right'
                    value = number(a._fresh(snap['grippers'].get(side), side)['position'], side)
                    if not 0 <= value <= 100:
                        continue
                a.transport.publish(key, value); held.append(key)
            except Exception:
                pass

    grip_changed = {j:[] for j in (14, 15)}
    for j in grip_changed:
        changed = 0
        for i, row in enumerate(rows):
            if i and row.values[j] != rows[i-1].values[j]:
                changed = i
            grip_changed[j].append(times[changed])

    def monitor(elapsed, row_index):
        nonlocal last_progress, max_error
        snap, actual = live_check(a, segment, cached=True)
        # Same feedback-age window and tolerances as ordinary pose following,
        # with extrema across every source knot (paths may reverse direction).
        for j, name in enumerate(ARMS):
            age = snap['joints'][name]['age_s']
            left = max(0, bisect_left(times, max(0., elapsed-age-a.profile['feedback_max_age_s']))-1)
            values = [r.values[j] for r in rows[left:row_index+1]] or [rows[0].values[j]]
            tolerance = a.profile['joint_tolerance_deg']
            if not min(values)-tolerance <= actual[j] <= max(values)+tolerance:
                raise ValueError('回放实际关节未跟上命令: ' + name)
        # A gripper event gets its existing actuator timeout; never demand an
        # instantaneous jump or repeatedly resend a held close/open command.
        for j in (14, 15):
            if elapsed-grip_changed[j][row_index] > a.profile['gripper_timeout_s']:
                if abs(actual[j]-rows[row_index].values[j]) > a.profile['gripper_tolerance']:
                    raise ValueError('回放夹爪未在已有时限内到位')
        error = max(abs(x-y) for x, y in zip(actual[:14], rows[row_index].values[:14]))
        max_error = max(max_error, error)
        if elapsed-last_progress >= .02:
            observed = snap.get('snapshot_monotonic_s', a.clock())-start
            items = [snap['joints'][n] for n in ARMS]+[snap['grippers'][s] for s in ('left','right')]
            sample = dict(elapsed_s=observed, feedback_times_s=[observed-v['age_s'] for v in items], sequence=rows[row_index].sequence,
                          actual=actual, target=list(rows[row_index].values), max_arm_error_deg=error)
            samples.append(sample); last_progress = elapsed
            if on_progress and (len(samples) == 1 or len(samples) % 5 == 0):
                on_progress(dict(phase='replay' if elapsed < segment.duration else 'settling',
                                 elapsed_s=elapsed, estimated_total_s=segment.duration,
                                 progress=min(1., elapsed/segment.duration),
                                 command_index=row_index+1, command_count=len(rows),
                                 max_arm_error_deg=error, episode_id=spec['replay']['episode_id']))
        return snap

    try:
        if cancel.is_set():
            return result
        live_check(a, segment, start=True)
        for key in KEYS:
            if cancel.is_set():
                return result
            a.transport.prepare(key); prepared.append(key)
        live_check(a, segment, start=True)
        if cancel.is_set():
            return result
        last_sent = {}
        start = a.clock()
        for due, key, i, value in schedule:
            while not cancel.is_set() and a.clock()-start+1e-9 < due:
                cancel.wait(min(.01, due-(a.clock()-start)))
            if cancel.is_set():
                break
            elapsed = a.clock()-start
            late = elapsed-due; max_late = max(max_late, late)
            if late > gap or elapsed > maximum:
                raise ValueError('回放下发超过节拍预算，停止而不跳帧追赶')
            if key == KEYS[0] and key in last_sent and elapsed-last_sent[key] > gap:
                raise ValueError('回放命令间隔超过已有节拍预算')
            monitor(elapsed, max(0, i-1))
            if cancel.is_set():
                break
            dispatched = a.clock()-start
            # Recorded DDS call times can be only tens of microseconds apart.
            # They are deadlines to measure, not a second, variable timeout.
            # Use the unchanged absolute lateness and inter-arm gap budgets.
            if dispatched-due > gap or (key == KEYS[0] and key in last_sent and dispatched-last_sent[key] > gap):
                raise ValueError('回放检查耗时超过命令节拍，已停止')
            dispatch_lateness.append((dispatched-due)*1000)
            sent.add(key)
            a.transport.publish(key, value)
            last_sent[key] = a.clock()-start; output_count += 1
        else:
            arrival = None
            deadline = min(start+maximum, start+segment.duration+a.profile['settle_timeout_s'])
            while not cancel.is_set():
                now = a.clock(); elapsed = now-start
                if now > deadline:
                    raise ValueError('命令已结束，但尚未确认实测到位')
                snap = monitor(elapsed, len(rows)-1)
                items, parts = feedback(a, snap, final)
                if all(p['status'] == 'at_target' for p in parts):
                    receipts = {n:(v.get('receipt_seq'), now-v['age_s']) for n, v in items.items()}
                    if any(seq is None for seq, _ in receipts.values()):
                        raise ValueError('缺少新反馈序号，不能确认回放完成')
                    if arrival is None:
                        arrival = receipts
                    elif all(seq != arrival[n][0] and at-arrival[n][1] >= .15 for n, (seq, at) in receipts.items()):
                        result.update(status='succeeded', feedback_verified=True,
                                      arrival_feedback=arrival_feedback(a, final, items, now))
                        return result
                else:
                    arrival = None
                cancel.wait(.01)
        hold()
        return result
    except Exception as exc:
        if sent:
            hold()
        result.update(status='cancelled' if cancel.is_set() else 'failed' if sent else 'rejected', error=str(exc))
        exc.commands_sent = bool(sent); exc.hold_sent = bool(held); exc.physical_stop_confirmed = False
        raise
    finally:
        result.update(commands_sent=bool(sent), hold_sent=bool(held), output_count=output_count,
                      elapsed_s=0 if start is None else a.clock()-start,
                      max_arm_error_deg=max_error, max_schedule_lateness_ms=max_late*1000)
        for key in prepared:
            try:
                a.transport.release(key)
            except Exception:
                pass
        a._active_cancel = None; a._action_lock.release()
        result['dispatch_timing'] = dict(samples=len(dispatch_lateness), max_lateness_ms=max(dispatch_lateness,default=0.), mean_lateness_ms=sum(dispatch_lateness)/max(1,len(dispatch_lateness)), scope='application_dispatch_not_motor_delivery')
        comparison = compare(segment, samples, max_gap_s=a.profile['feedback_max_age_s'], execution_status=result['status'])
        if spec.get('_report_path'):
            path = Path(spec['_report_path'])
            temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open('x') as f:
                    json.dump(dict(result=result, request=spec['replay'], comparison=comparison, samples=samples), f, ensure_ascii=False)
                    f.flush(); os.fsync(f.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                result['report_error'] = str(exc)

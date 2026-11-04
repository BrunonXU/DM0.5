"""Target-faithful replay with bounded monitoring and recoverable scheduling jitter.

No source filtering/resampling, background resume, or implicit initial movement.
"""
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
    reschedule_after = number(profile.get('replay_reschedule_after_s', .025), '回放顺延触发间隔')
    if not 1 <= maximum <= 1800 or not 0 < reschedule_after <= .025:
        raise ValueError('回放时限/顺延配置无效')
    return maximum, reschedule_after


def check_values(profile, q, mask=(True,)*16):
    for name, value in zip(ARMS, q[:14]):
        low, high = profile['joint_limits_deg'][name]
        if not low <= value <= high:
            raise ValueError('回放目标越界: ' + name)
    for offset in (0, 7):
        if 1.025*abs(q[offset+5])+abs(q[offset+6]) > 110.5+1e-7:
            raise ValueError('回放目标超过 F1 耦合腕部限位')
    if any(not 0 <= q[i] <= 100 for i in (14, 15) if mask[i]):
        raise ValueError('回放夹爪目标超出 0–100')


def validate(profile, segment):
    if not isinstance(segment, Segment):
        raise ValueError('回放只能执行后端已核对的录制')
    rec = segment.recording
    if rec.robot != profile.get('id', profile.get('robot_id')):
        raise ValueError('录制机器人身份与本机不符')
    if not rec.profile_sha256 or rec.profile_sha256 != profile.get('profile_sha256'):
        raise ValueError('录制绑定的机器人配置与当前配置不一致，不能自动套用')
    maximum, reschedule_after = limits(profile)
    if segment.duration + profile['settle_timeout_s'] > maximum:
        raise ValueError('所选片段超过回放总任务预算，请缩短区间')
    rows = segment.commands
    max_speed, max_acceleration, max_recorded_gap = 0., 0., 0.
    previous_velocity, previous_dt = None, None
    for i, row in enumerate(rows):
        check_values(profile, row.values)
        if not i:
            continue
        dt = (row.publications[0][1]-rows[i-1].publications[0][1])/1e9
        if dt <= 0:
            raise ValueError('原始手臂命令发布时间必须严格递增')
        # Recorded spacing is part of the source timeline, not scheduler delay.
        # No interpolation, repeated keepalive targets, or gap compression.
        max_recorded_gap = max(max_recorded_gap, dt)
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
                max_recorded_command_gap_ms=max_recorded_gap*1000,
                reschedule_after_ms=reschedule_after*1000,
                basis='publication_samples_diagnostic_only_not_motor_derivatives')


def start_deviations(profile, segment, actual):
    """Measured initial errors; force-start never substitutes target feedback."""
    arms, grippers = [], []
    for i, (value, target) in enumerate(zip(actual, segment.commands[0].values)):
        if not segment.recording.valid_mask[i]:
            continue
        error = value-target
        if i < 14:
            tolerance = profile['joint_tolerance_deg']
            arms.append(dict(name=ARMS[i], actual=value, target=target,
                             error_deg=error, tolerance_deg=tolerance,
                             outside_tolerance=abs(error) > tolerance))
        else:
            tolerance = profile['gripper_tolerance']
            grippers.append(dict(side=('left', 'right')[i-14], actual=value, target=target,
                                 error=error, tolerance=tolerance,
                                 outside_tolerance=abs(error) > tolerance))
    return dict(arms=arms, grippers=grippers,
                max_arm_error_deg=max(abs(row['error_deg']) for row in arms),
                misaligned_arms=[row['name'] for row in arms if row['outside_tolerance']])


def check_start(profile, segment, actual, *, force_start=False):
    deviations = start_deviations(profile, segment, actual)
    mismatches = [] if force_start else list(deviations['misaligned_arms'])
    mismatches += [row['side']+'夹爪' for row in deviations['grippers'] if row['outside_tolerance']]
    if mismatches:
        raise ValueError('回放起点尚未对齐: ' + ', '.join(mismatches))
    return deviations


def live_check(a, segment, *, start=False, cached=False, force_start=False):
    if not all(a.profile.get(k) for k in ('joints_enabled', 'limits_verified', 'gripper_verified')):
        raise ValueError('本机双臂/夹爪控制合同尚未启用')
    guard = a.transport.assert_exclusive_cached if cached else a.transport.assert_exclusive
    for key in segment.recording.command_keys:
        guard(key)
    snap = a._common()
    actual = []
    for name in ARMS:
        item = a._fresh(snap['joints'].get(name), name)
        actual.append(number(item['position'], name))
    for i, side in enumerate(('left', 'right'), 14):
        if not segment.recording.valid_mask[i]:
            actual.append(None)
            continue
        item = a._fresh(snap['grippers'].get(side), side)
        actual.append(number(item['position'], side))
    check_values(a.profile, actual, segment.recording.valid_mask)
    mismatches = []
    for name, target in zip(CONTEXT, segment.context):
        value = number(a._fresh(snap['joints'].get(name), name)['position'], name)
        if abs(value-target) > a.profile['joint_tolerance_deg']:
            mismatches.append(name)
    if mismatches:
        raise ValueError('身体/头部与录制上下文不一致: ' + ', '.join(mismatches))
    if start:
        check_start(a.profile, segment, actual, force_start=force_start)
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
    for key in segment.recording.command_keys[1:]:
        if key not in dict(segment.commands[0].publications):
            schedule.append((0., key, 0, segment.commands[0].values[14 if key == KEYS[1] else 15]))
    return sorted(schedule, key=lambda x: x[0])

def execute(a, spec, cancel, on_progress=None):
    segment = spec.get('_segment')
    # Only ReplayService creates this private field from a validated request;
    # display metadata and arbitrary truthy values cannot enable the exception.
    force_start = spec.get('_force_start', False)
    if type(force_start) is not bool:
        raise ValueError('强行回放标记必须为布尔值')
    validate(a.profile, segment)
    schedule = prepare_schedule(segment)
    maximum, reschedule_after = limits(a.profile)
    continuity_limit = number(a.profile['feedback_max_age_s'], '反馈监控连续性时限')
    if continuity_limit <= 0:
        raise ValueError('反馈监控连续性时限无效')
    if not a._action_lock.acquire(blocking=False):
        raise ValueError('已有动作正在执行')
    a._active_cancel = cancel
    prepared, sent, held = [], set(), []
    samples, output_count, last_progress = [], 0, -math.inf
    max_error, max_late = 0., 0.
    dispatch_lateness = []
    start = None
    result = dict(status='cancelled', commands_sent=False, feedback_verified=False,
                  physical_stop_confirmed=False, replay=segment.summary(), force_start=force_start)
    rows = segment.commands
    gripper_indices = segment.recording.gripper_indices
    gripper_sides = tuple(('left', 'right')[i-14] for i in gripper_indices)
    timeline_shift, last_monitor_at = 0., 0.
    reschedules = []
    final = dict(targets=dict(zip(ARMS, rows[-1].values[:14])),
                 grippers={('left', 'right')[i-14]:rows[-1].values[i] for i in gripper_indices},
                 parts=[dict(id=p, label=p) for p in ('left_arm', 'right_arm') +
                        tuple(s+'_gripper' for s in gripper_sides)])

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

    grip_changed_at = {j:0. for j in gripper_indices}
    grip_targets = {j:rows[0].values[j] for j in gripper_indices}

    def check_continuity(elapsed):
        if elapsed > maximum:
            raise ValueError('回放超过总任务时限，已停止')
        if elapsed-last_monitor_at > continuity_limit:
            raise ValueError('回放程序长时间未能检查机器人状态，已停止；请检查设备负载后重新开始')

    def monitor(elapsed, row_index):
        nonlocal last_progress, max_error, last_monitor_at
        check_continuity(a.clock()-start)
        snap, actual = live_check(a, segment, cached=True)
        check_continuity(a.clock()-start)
        last_monitor_at = a.clock()-start
        # Arm following error is evidence for the post-run comparison, not a
        # stop condition. live_check still enforces fresh feedback and limits.
        # A gripper event gets its existing actuator timeout; never demand an
        # instantaneous jump or repeatedly resend a held close/open command.
        for j in gripper_indices:
            if elapsed-grip_changed_at[j] > a.profile['gripper_timeout_s']:
                if abs(actual[j]-grip_targets[j]) > a.profile['gripper_tolerance']:
                    raise ValueError('回放夹爪未在已有时限内到位')
        error = max(abs(x-y) for x, y in zip(actual[:14], rows[row_index].values[:14]))
        max_error = max(max_error, error)
        if elapsed-last_progress >= .02:
            observed = snap.get('snapshot_monotonic_s', a.clock())-start
            items = [snap['joints'][n] for n in ARMS]+[
                snap['grippers'][s] if s in gripper_sides else None for s in ('left','right')]
            sample = dict(elapsed_s=observed, feedback_times_s=[observed-v['age_s'] if v else None for v in items], sequence=rows[row_index].sequence,
                          actual=actual, target=list(rows[row_index].values), max_arm_error_deg=error)
            samples.append(sample); last_progress = elapsed
            if on_progress and (len(samples) == 1 or len(samples) % 5 == 0):
                trajectory_elapsed = max(0., elapsed-timeline_shift)
                on_progress(dict(phase='replay' if trajectory_elapsed < segment.duration else 'settling',
                                 elapsed_s=elapsed, estimated_total_s=segment.duration+timeline_shift,
                                 progress=min(1., trajectory_elapsed/segment.duration),
                                 command_index=row_index+1, command_count=len(rows),
                                 max_arm_error_deg=error, episode_id=spec['replay']['episode_id']))
        return snap

    def schedule_delay(elapsed, due, phase):
        nonlocal max_late, timeline_shift
        check_continuity(elapsed)
        late = elapsed-(due+timeline_shift)
        max_late = max(max_late, late)
        if late > reschedule_after:
            # Pause the remaining schedule instead of catching up through old
            # targets or treating ordinary host scheduling jitter as bad data.
            timeline_shift += late
            reschedules.append(dict(source_time_s=due, elapsed_s=elapsed,
                                    shift_s=late, total_shift_s=timeline_shift, phase=phase))
            if segment.duration+timeline_shift+a.profile['settle_timeout_s'] > maximum:
                raise ValueError('回放顺延后超过总任务时限，已停止')
        return late

    try:
        if cancel.is_set():
            return result
        live_check(a, segment, start=True, force_start=force_start)
        for key in segment.recording.command_keys:
            if cancel.is_set():
                return result
            a.transport.prepare(key); prepared.append(key)
        _, actual = live_check(a, segment, start=True, force_start=force_start)
        result['start_deviations'] = start_deviations(a.profile, segment, actual)
        if cancel.is_set():
            return result
        last_command_index = 0
        start = a.clock()
        for due, key, i, value in schedule:
            while not cancel.is_set() and a.clock()-start+1e-9 < due+timeline_shift:
                cancel.wait(min(.01, due+timeline_shift-(a.clock()-start)))
                if not cancel.is_set() and a.clock()-start+1e-9 < due+timeline_shift:
                    # A recorded pause must not pause feedback/exclusivity checks.
                    # Monitor only the last sent arm target, never a future row.
                    monitor(a.clock()-start, last_command_index)
            if cancel.is_set():
                break
            elapsed = a.clock()-start
            late = schedule_delay(elapsed, due, 'schedule')
            monitor(elapsed, last_command_index)
            if cancel.is_set():
                break
            dispatched = a.clock()-start
            # Recorded DDS call times can be only tens of microseconds apart.
            # They are deadlines to measure, not a second, variable timeout.
            late = max(late, schedule_delay(dispatched, due, 'after_monitor'))
            dispatch_lateness.append(late*1000)
            sent.add(key)
            a.transport.publish(key, value)
            output_count += 1
            if key == KEYS[0]:
                last_command_index = i
            else:
                j = 14 if key == KEYS[1] else 15
                if value != grip_targets[j]:
                    grip_targets[j], grip_changed_at[j] = value, dispatched
            # Also catch a blocked publish, including the final event. A command
            # already handed to transport cannot be retracted by this check.
            schedule_delay(a.clock()-start, due, 'publish_return')
        else:
            arrival = None
            at_target_since = None
            result['commands_completed'] = True
            deadline = min(start+maximum, start+segment.duration+timeline_shift+a.profile['settle_timeout_s'])
            while not cancel.is_set():
                now = a.clock(); elapsed = now-start
                if now > deadline:
                    raise ValueError('命令已结束，但尚未确认夹爪到位或收到足够的新反馈')
                snap = monitor(elapsed, len(rows)-1)
                items, parts = feedback(a, snap, final)
                receipts = {n:(v.get('receipt_seq'), now-v['age_s']) for n, v in items.items()}
                if any(seq is None for seq, _ in receipts.values()):
                    raise ValueError('缺少新反馈序号，不能确认回放完成')
                at_target = all(p['status'] == 'at_target' for p in parts)
                grippers_at_target = all(p['status'] == 'at_target' for p in parts if p['id'].endswith('_gripper'))
                if not at_target:
                    at_target_since = None
                elif at_target_since is None:
                    at_target_since = receipts
                if arrival is None:
                    arrival = receipts
                elif grippers_at_target and all(seq != arrival[n][0] and at-arrival[n][1] >= .15 for n, (seq, at) in receipts.items()):
                    verified = at_target_since is not None and all(
                        seq != at_target_since[n][0] and at-at_target_since[n][1] >= .15
                        for n, (seq, at) in receipts.items())
                    result.update(status='succeeded', feedback_verified=verified,
                                  final_target_reached=at_target, post_run_feedback_observed=True,
                                  final_feedback=arrival_feedback(a, final, items, now))
                    if verified:
                        result['arrival_feedback'] = result['final_feedback']
                    return result
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
        result['playback_timing'] = dict(mode='preserve_targets_shift_remaining_schedule',
                total_shift_s=timeline_shift, reschedule_count=len(reschedules),
                reschedules=reschedules, monitor_continuity_limit_s=continuity_limit,
                comparison_time_basis='wall_time_unchanged; no_posthoc_alignment')
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
                try:
                    from replay_reports import record_latest
                    record_latest(path, spec['replay'], result)
                except Exception as exc:
                    result['report_index_error'] = str(exc)
            except OSError as exc:
                result['report_error'] = str(exc)

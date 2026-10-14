"""Optional one-target head boundary return followed by HAL observation.

This is NOT a smooth or speed-controlled recovery: the first legal setpoint can
be discontinuous from an exceptional actual position. It requires an enabled, driver-bound profile and explicit acknowledgement in
the pose execution path. The 3-degree admission cap is a recovery envelope,
not a physical joint limit. Physical acceptance is recorded separately.

The caller owns the action lock, complete final-target preflight, prepared
joint_command publisher, lease and overall deadline. It must re-plan the whole
pose from actual feedback after ``recovered``. No publisher is created here;
there is no retry, hold, calibration change, direct UDP or motion-mode change.
HAL receipt verification does not establish fresh EtherCAT PDO samples.
"""
import math

JOINT = 'head_j2'
DRIVER_SHA256 = '898ef58fb7118ecbf416af134e01700ed38ffdd3480293cf06fe95da14bce0c8'
LIMITS = (-25.0, 25.0)
MAX_OUTSIDE_DEG = 3.0
MAX_TIMEOUT_S = 3.0
OBSERVE_S = .15
REVERSE_DEG = .15
NO_PROGRESS_S = .75
PROGRESS_DEG = .02
POLL_S = .02


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (float, int)) and math.isfinite(value)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _settings(profile):
    config = profile.get('head_boundary_recovery')
    if config is None:
        return None
    _require(isinstance(config, dict), '头部边界试用配置必须为对象')
    if config.get('enabled', False) is False:
        return None
    _require(config.get('enabled') is True, '头部边界试用必须明确启用')
    _require(profile.get('controller_binary_sha256') == DRIVER_SHA256, '头部边界试用未绑定已审计驱动')
    for field in ('joint_limits_deg', 'limits'):
        bounds = profile.get(field, {}).get(JOINT)
        _require(isinstance(bounds, (list, tuple)) and len(bounds) == 2
                 and all(_number(x) for x in bounds) and tuple(bounds) == LIMITS,
                 '头部边界试用要求两份驱动命令范围均为 -25..25')
    cap, timeout = config.get('max_outside_deg'), config.get('timeout_s')
    _require(_number(cap) and 0 < cap <= MAX_OUTSIDE_DEG, '头部边界试用幅度必须大于0且不超过3度')
    _require(_number(timeout) and OBSERVE_S <= timeout <= MAX_TIMEOUT_S,
             '头部边界试用时限必须为0.15至3秒')
    return float(cap), float(timeout)


def _position(snap, fresh):
    row = fresh(snap.get('joints', {}).get(JOINT), JOINT)
    position = row.get('position')
    _require(_number(position), '头部边界试用反馈角度无效')
    return float(position), row


def inspect(profile, targets, snap, fresh):
    """Return an exceptional-origin plan, or None without any control output.

    Only an explicitly requested J2 may trigger this optional candidate. Final
    targets stay inside the current driver range; URDF ranges are not consulted.
    ``fresh`` is the adapter's ordinary feedback freshness check.
    """
    if not isinstance(targets, dict) or JOINT not in targets:
        return None
    settings = _settings(profile)
    if settings is None:
        return None
    cap, timeout = settings
    target = targets[JOINT]
    _require(_number(target) and LIMITS[0] <= target <= LIMITS[1], '头部最终目标必须在驱动 -25..25 范围内')
    origin, _ = _position(snap, fresh)
    if LIMITS[0] <= origin <= LIMITS[1]:
        return None
    edge, direction = (LIMITS[0], 1) if origin < LIMITS[0] else (LIMITS[1], -1)
    distance = abs(edge - origin)
    _require(distance <= cap, '头部反馈超过本次边界试用幅度；不能继续')
    return {'joint': JOINT, 'origin': origin, 'edge': edge, 'direction': direction,
            'final_target': float(target), 'outside_deg': distance, 'limits': list(LIMITS),
            'max_outside_deg': cap, 'timeout_s': timeout, 'driver_sha256': DRIVER_SHA256,
            'speed_controlled': False, 'scope': 'experimental_boundary_target_not_smooth_recovery'}


def _validate_plan(profile, plan):
    _require(isinstance(plan, dict), '头部边界试用计划无效')
    settings = _settings(profile)
    _require(settings is not None, '头部边界试用尚未启用')
    origin, target = plan.get('origin'), plan.get('final_target')
    _require(_number(origin) and not LIMITS[0] <= origin <= LIMITS[1], '头部边界试用必须保留真实越界起点')
    _require(_number(target) and LIMITS[0] <= target <= LIMITS[1], '头部最终目标必须在驱动 -25..25 范围内')
    cap, timeout = settings
    edge, direction = (LIMITS[0], 1) if origin < LIMITS[0] else (LIMITS[1], -1)
    _require(abs(edge-origin) <= cap, '头部反馈超过本次边界试用幅度；不能继续')
    _require(plan.get('joint') == JOINT and plan.get('edge') == edge
             and type(plan.get('direction')) is int and plan['direction'] == direction
             and plan.get('driver_sha256') == DRIVER_SHA256 and plan.get('limits') == list(LIMITS)
             and plan.get('speed_controlled') is False
             and plan.get('max_outside_deg') == cap and plan.get('timeout_s') == timeout,
             '头部边界试用计划与当前合同不一致')
    return cap, timeout


def _feedback(a, snap, now):
    position, row = _position(snap, a._fresh)
    sequence, age = row.get('receipt_seq'), row.get('age_s')
    _require(type(sequence) is int and sequence > 0, '头部反馈缺少有效接收序号')
    _require(_number(age) and age >= 0, '头部反馈接收时间无效')
    return position, sequence, now-age


def execute(a, plan, cancel, on_progress=None, deadline=None, guard_keys=()):
    """Send at most one J2 boundary target, then observe strict range re-entry.

    Caller must already hold the action lock and prepare joint_command. This
    function never creates/releases endpoints or emits a hold. Cancellation can
    stop further software output, but cannot revoke an accepted edge setpoint.
    ``deadline`` is an optional absolute monotonic task deadline.

    Errors retain commands_sent/boundary_command_sent metadata. A publish that
    raises is marked delivery_uncertain and commands_sent conservatively true;
    boundary_command_sent only states that the publish call returned normally.
    """
    start = a.clock()
    attempted = sent = False
    observed_origin = position = None

    def outcome(cancelled=False, recovered=False):
        return {'status': 'cancelled' if cancelled else 'recovered', 'cancelled': cancelled,
                'recovered': recovered, 'commands_sent': attempted, 'boundary_command_sent': sent,
                'boundary_command_attempted': attempted, 'delivery_uncertain': attempted and not sent,
                'hold_sent': False, 'physical_stop_confirmed': False, 'speed_controlled': False,
                'feedback_verified': recovered, 'feedback_scope': 'hal_receipt_not_pdo_validated',
                'origin': observed_origin, 'feedback_position_deg': position, 'elapsed_s': a.clock()-start}

    try:
        if cancel.is_set():
            return outcome(cancelled=True)
        cap, timeout = _validate_plan(a.profile, plan)
        expires = start + timeout
        if deadline is not None:
            _require(_number(deadline), '头部边界试用任务截止时间无效')
            expires = min(expires, deadline)
        _require(expires-start >= OBSERVE_S, '头部边界试用任务剩余时间不足')
        max_age = a.profile.get('feedback_max_age_s')
        _require(_number(max_age) and max_age > 0, '头部反馈新鲜度配置无效')
        a._common()
        for key in dict.fromkeys(('joint_command', *guard_keys)):
            a.transport.assert_exclusive(key)
        snap = a._common()
        now = a.clock()
        _require(expires-now >= OBSERVE_S, '头部边界试用任务剩余时间不足')
        current_plan = inspect(a.profile, {JOINT: plan['final_target']}, snap, a._fresh)
        position, sequence, receipt = _feedback(a, snap, now)
        observed_origin = position
        if current_plan is not None:
            _require(current_plan['edge'] == plan['edge'] and current_plan['direction'] == plan['direction'],
                     '准备期间头部越界方向已改变，请重新执行')
        if cancel.is_set():
            return outcome(cancelled=True)
        if current_plan is not None:
            attempted = True
            a.transport.publish('joint_command', {JOINT: plan['edge']})
            sent = True

        last_sequence, last_receipt, last_position = sequence, receipt, position
        last_new = last_progress = now
        peak_advance = progress_mark = 0.0
        arrival = None if current_plan is not None else (sequence, receipt)
        while not cancel.is_set():
            a._common()
            for key in dict.fromkeys(('joint_command', *guard_keys)):
                a.transport.assert_exclusive_cached(key)
            snap = a._common()
            now = a.clock()
            _require(now < expires, '头部未在边界试用时限内回到驱动范围')
            position, sequence, receipt = _feedback(a, snap, now)
            _require(sequence >= last_sequence and receipt >= last_receipt-1e-8, '头部反馈接收序号或时间倒退')
            new_sample = sequence != last_sequence
            if not new_sample:
                _require(position == last_position, '头部反馈变化缺少新的接收序号')
            else:
                last_new = now
                last_sequence, last_receipt, last_position = sequence, receipt, position
            _require(now-last_new <= max_age, '头部未收到新的反馈接收序号')
            advance = plan['direction'] * (position-observed_origin)
            _require(advance >= peak_advance-REVERSE_DEG, '头部反馈明显朝外运动，已终止边界试用')
            inside = LIMITS[0] <= position <= LIMITS[1]
            if new_sample:
                peak_advance = max(peak_advance, advance)
                # Accumulate small samples without treating oscillation as progress.
                if advance >= progress_mark+PROGRESS_DEG:
                    progress_mark = advance
                    last_progress = now
            if inside:
                if arrival is None:
                    arrival = (sequence, receipt)
                confirmed = new_sample and sequence != arrival[0] and receipt-arrival[1] >= OBSERVE_S
            else:
                _require(sent, '确认期间头部再次越界，本次没有发送恢复动作')
                _require(abs(plan['edge']-position) <= cap, '头部反馈超出边界试用幅度')
                arrival = None
                confirmed = False
                _require(now-last_progress < NO_PROGRESS_S, '头部反馈没有持续向内进展，已终止边界试用')
            if on_progress:
                on_progress({'phase': 'head_boundary_observation', 'elapsed_s': now-start,
                             'feedback_position_deg': position, 'feedback_arrived': confirmed,
                             'speed_controlled': False, 'boundary_command_sent': sent})
            if cancel.is_set():
                return outcome(cancelled=True)
            if confirmed:
                return outcome(recovered=True)
            cancel.wait(min(POLL_S, max(0., expires-a.clock())))
        return outcome(cancelled=True)
    except Exception as error:
        details = outcome()
        for key in ('commands_sent', 'boundary_command_sent', 'boundary_command_attempted',
                    'delivery_uncertain', 'hold_sent', 'physical_stop_confirmed', 'speed_controlled'):
            setattr(error, key, details[key])
        error.recovery = {**details, 'status': 'failed', 'recovered': False, 'feedback_verified': False}
        raise

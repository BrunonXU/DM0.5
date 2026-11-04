"""Offline candidate only: no ROS, robot connection, enabling or deployment."""
from copy import deepcopy

import pytest

from robot.head_boundary_recovery import DRIVER_SHA256, inspect, execute
from robot.adapter import ActionRejected
from test_body_stream import stream
from test_ros_adapter import Event, joints


def candidate(origin=-27.644617):
    a, clock, transport = stream()
    a.profile.update(controller_binary_sha256=DRIVER_SHA256,
                     head_boundary_recovery={'enabled': True, 'max_outside_deg': 3., 'timeout_s': 3.})
    a.profile['joint_limits_deg']['head_j2'] = [-25, 25]
    a.profile['limits'] = {'head_j2': [-25, 25]}
    a.store.update_joints(joints(['head_j2'], [origin]))
    # The function must neither prepare nor release a publisher. Movement and
    # receipt updates come only from the scripted Event.
    transport.mirror = False
    transport.refresh_feedback = False
    return a, clock, transport


def plan(a, **targets):
    return inspect(a.profile, targets or {'head_j2': 0}, a._common(), a._fresh)


class Feedback(Event):
    def __init__(self, a, clock, position, *, after=None, hook=None, receive=True):
        super().__init__(clock, after=after)
        self.a, self.position, self.hook = a, position, hook
        self.start, self.receive = clock(), receive

    def wait(self, seconds):
        stopped = super().wait(seconds)
        elapsed = self.clock()-self.start
        if self.receive:
            self.a.store.update_joints(joints(['head_j2'], [self.position(elapsed)]))
        if self.hook:
            self.hook(elapsed)
        return stopped


def test_import_and_inspect_are_read_only_and_default_disabled():
    a, clock, transport = candidate()
    original = deepcopy(a.store.snapshot())
    p = plan(a)
    assert p['origin'] == -27.644617 and p['edge'] == -25 and p['direction'] == 1
    assert p['speed_controlled'] is False and 'experimental' in p['scope']
    assert original == a.store.snapshot()
    assert plan(a, arm_l_j1=0) is None
    del a.profile['head_boundary_recovery']
    assert plan(a) is None
    a.profile['head_boundary_recovery'] = {'enabled': False}
    assert plan(a) is None
    assert not transport.sent and not transport.prepared and not transport.released


@pytest.mark.parametrize('origin', [-25., 0., 25.])
def test_legal_actual_does_not_need_recovery(origin):
    a, _, _ = candidate(origin)
    assert plan(a) is None


@pytest.mark.parametrize('origin,edge,direction', [(-27.644617, -25., 1), (27.5, 25., -1)])
def test_success_uses_one_legal_joint_only_and_real_new_feedback(origin, edge, direction):
    a, clock, transport = candidate(origin)
    p = plan(a)
    assert p['edge'] == edge and p['direction'] == direction
    feedback = Feedback(a, clock, lambda t: origin+(edge-origin)*min(t/.3, 1.))
    result = execute(a, p, feedback)
    assert result['recovered'] and result['status'] == 'recovered' and result['feedback_verified']
    assert result['origin'] == origin and result['feedback_position_deg'] == edge
    assert result['elapsed_s'] >= .45
    assert transport.sent == [('joint_command', {'head_j2': edge})]
    assert result['boundary_command_sent'] and result['commands_sent']
    assert not result['physical_stop_confirmed'] and not result['speed_controlled'] and not result['hold_sent']
    assert not transport.prepared and not transport.released


@pytest.mark.parametrize('change', [
    lambda p: p.update(controller_binary_sha256='0'*64),
    lambda p: p['joint_limits_deg'].update(head_j2=[-28, 28]),
    lambda p: p['limits'].update(head_j2=[-28, 28]),
    lambda p: p.pop('limits'),
    lambda p: p['head_boundary_recovery'].update(enabled=1),
    lambda p: p['head_boundary_recovery'].update(max_outside_deg=3.01),
    lambda p: p['head_boundary_recovery'].update(max_outside_deg=float('nan')),
    lambda p: p['head_boundary_recovery'].update(timeout_s=3.01),
    lambda p: p['head_boundary_recovery'].update(timeout_s=.14),
])
def test_bad_configuration_refuses_before_output(change):
    a, _, transport = candidate()
    change(a.profile)
    with pytest.raises((ValueError, ActionRejected)):
        plan(a)
    assert not transport.sent


@pytest.mark.parametrize('origin', [-28.01, 28.01, float('nan'), float('inf')])
def test_trial_cap_and_invalid_feedback_are_not_broadened(origin):
    a, _, transport = candidate(origin)
    with pytest.raises((ValueError, ActionRejected)):
        plan(a)
    assert not transport.sent


@pytest.mark.parametrize('target', [-25.01, 25.01, True, float('nan')])
def test_illegal_final_target_never_starts_recovery(target):
    a, _, transport = candidate()
    with pytest.raises((ValueError, ActionRejected)):
        plan(a, head_j2=target)
    assert not transport.sent


def test_urdf_range_has_no_authority_over_this_driver_boundary():
    a, _, _ = candidate()
    a.profile['urdf_limits'] = {'head_j2': [-1, 1]}
    assert plan(a)['edge'] == -25
    a.profile['urdf_limits']['head_j2'] = [-360, 360]
    assert plan(a)['edge'] == -25


def test_exact_range_reentry_not_existing_one_degree_start_tolerance():
    a, clock, transport = candidate()
    a.profile['feedback_limit_recovery_deg'] = 1.
    with pytest.raises(ValueError, match='进展') as error:
        execute(a, plan(a), Feedback(a, clock, lambda t: -25.01))
    assert error.value.commands_sent and error.value.boundary_command_sent
    assert not error.value.physical_stop_confirmed and not error.value.hold_sent
    assert transport.sent == [('joint_command', {'head_j2': -25})]


@pytest.mark.parametrize('receive,match', [(True, '进展'), (False, '过期|新的反馈')])
def test_frozen_position_and_old_samples_never_recover(receive, match):
    a, clock, transport = candidate()
    with pytest.raises((ValueError, ActionRejected), match=match) as error:
        execute(a, plan(a), Feedback(a, clock, lambda t: -27.644617, receive=receive))
    assert error.value.commands_sent and not error.value.recovery['recovered']
    assert len(transport.sent) == 1


def test_receipt_age_refresh_without_new_sequence_cannot_confirm_arrival():
    a, clock, transport = candidate()
    def old_value(t):
        a.store.joints['head_j2'].update(position=-25., received_at=clock())
    with pytest.raises(ValueError, match='接收序号'):
        execute(a, plan(a), Feedback(a, clock, lambda t: -25, receive=False, hook=old_value))
    assert len(transport.sent) == 1


def test_outward_motion_and_reversal_after_inward_progress_abort():
    for trajectory in (lambda t: -27.9, lambda t: -26. if t < .1 else -26.16):
        a, clock, transport = candidate()
        with pytest.raises(ValueError, match='朝外') as error:
            execute(a, plan(a), Feedback(a, clock, trajectory))
        assert error.value.commands_sent and not error.value.hold_sent
        assert len(transport.sent) == 1


def test_reentry_observation_restarts_after_leaving_exact_range():
    a, clock, transport = candidate()
    trace = lambda t: -25.01 if .12 <= t < .16 else -25.
    result = execute(a, plan(a), Feedback(a, clock, trace))
    assert result['recovered'] and result['elapsed_s'] >= .31
    assert len(transport.sent) == 1


def test_short_deadline_and_forged_plan_are_zero_output():
    a, clock, transport = candidate()
    with pytest.raises(ValueError, match='剩余时间'):
        execute(a, plan(a), Event(clock), deadline=clock()+.1)
    p = plan(a)
    p['edge'] = -24.
    with pytest.raises(ValueError, match='合同'):
        execute(a, p, Event(clock))
    assert not transport.sent


def test_deadline_bounds_slow_progress_without_retries():
    a, clock, transport = candidate()
    start = clock()
    with pytest.raises(ValueError, match='时限'):
        execute(a, plan(a), Feedback(a, clock, lambda t: -27.644617+.2*t), deadline=start+.4)
    assert clock()-start <= .400001 and len(transport.sent) == 1


def test_state_change_before_dispatch_revalidates_direction_and_trial_cap():
    for position in (27., -29.):
        a, clock, transport = candidate()
        p = plan(a)
        a.store.update_joints(joints(['head_j2'], [position]))
        with pytest.raises((ValueError, ActionRejected)):
            execute(a, p, Event(clock))
        assert not transport.sent


def test_already_legal_before_dispatch_observes_without_command_or_clamping():
    a, clock, transport = candidate()
    p = plan(a)
    a.store.update_joints(joints(['head_j2'], [-24.8]))
    result = execute(a, p, Feedback(a, clock, lambda t: -24.8))
    assert result['recovered'] and result['elapsed_s'] >= .15
    assert result['origin'] == -24.8 and not result['commands_sent'] and not transport.sent


def test_cancellation_before_after_and_at_confirmation_never_continues():
    a, clock, transport = candidate()
    event = Event(clock); event.set()
    result = execute(a, plan(a), event)
    assert result['cancelled'] and not result['commands_sent'] and not transport.sent
    a, clock, transport = candidate()
    event = Feedback(a, clock, lambda t: -27.6, after=clock()+.08)
    result = execute(a, plan(a), event)
    assert result['cancelled'] and not result['recovered'] and not result['physical_stop_confirmed']
    assert len(transport.sent) == 1 and not result['hold_sent']
    a, clock, transport = candidate()
    event = Feedback(a, clock, lambda t: -25.)
    def progress(value):
        if value['feedback_arrived']:
            event.set()
    result = execute(a, plan(a), event, progress)
    assert result['cancelled'] and not result['recovered'] and not result['feedback_verified']
    assert len(transport.sent) == 1


@pytest.mark.parametrize('before', [True, False])
def test_external_source_never_triggers_hold_or_retry(before):
    a, clock, transport = candidate()
    p = plan(a)
    transport.conflict = before
    def conflict(t):
        transport.conflict = True
    with pytest.raises(ActionRejected, match='external') as error:
        execute(a, p, Feedback(a, clock, lambda t: -27., hook=conflict))
    assert error.value.commands_sent is (not before)
    assert len(transport.sent) == (0 if before else 1) and not error.value.hold_sent


def test_control_contract_failure_during_observation_aborts():
    a, clock, transport = candidate()
    def disable(t):
        a.profile['control_contract_verified'] = False
    with pytest.raises(ActionRejected, match='合同') as error:
        execute(a, plan(a), Feedback(a, clock, lambda t: -27., hook=disable))
    assert error.value.commands_sent and not error.value.recovery['recovered']
    assert len(transport.sent) == 1


def test_publish_exception_is_delivery_uncertain_without_retry():
    a, clock, transport = candidate()
    def broken(key, value):
        raise RuntimeError('transport delivery unknown')
    transport.publish = broken
    with pytest.raises(RuntimeError, match='delivery unknown') as error:
        execute(a, plan(a), Event(clock))
    assert error.value.commands_sent and error.value.delivery_uncertain
    assert not error.value.boundary_command_sent and not error.value.hold_sent


def test_disabled_after_plan_is_rejected_before_any_output():
    a, clock, transport = candidate()
    p = plan(a)
    a.profile['head_boundary_recovery']['enabled'] = False
    with pytest.raises(ValueError, match='尚未启用'):
        execute(a, p, Event(clock))
    assert not transport.sent


def test_cancel_arrives_during_exclusivity_check_before_publish():
    a, clock, transport = candidate()
    event = Event(clock)
    original = transport.assert_exclusive
    def check(key):
        original(key)
        event.set()
    transport.assert_exclusive = check
    result = execute(a, plan(a), event)
    assert result['cancelled'] and not result['commands_sent'] and not transport.sent


def test_new_receipt_with_invalid_feedback_aborts_without_hold():
    a, clock, transport = candidate()
    with pytest.raises(ActionRejected, match='缺失') as error:
        execute(a, plan(a), Feedback(a, clock, lambda t: float('nan')))
    assert error.value.commands_sent and not error.value.hold_sent and len(transport.sent) == 1


def test_missing_receipt_sequence_before_publish_is_zero_output():
    a, clock, transport = candidate()
    p = plan(a)
    a.store.joints['head_j2'].pop('receipt_seq')
    with pytest.raises(ValueError, match='接收序号'):
        execute(a, p, Event(clock))
    assert not transport.sent


def test_stale_sequence_with_fake_fresh_age_does_not_reset_watchdog():
    a, clock, transport = candidate()
    def fake_age(t):
        a.store.joints['head_j2']['received_at'] = clock()
    with pytest.raises(ValueError, match='新的反馈接收序号'):
        execute(a, plan(a), Feedback(a, clock, lambda t: -27.644617, receive=False, hook=fake_age))
    assert len(transport.sent) == 1


def test_snapshot_processing_delay_does_not_make_same_receipt_go_backwards(monkeypatch):
    a, clock, transport = candidate()
    p = plan(a)
    original = a.snapshot
    snapshots = []

    def delayed_snapshot():
        snap = original()
        snapshots.append(snap)
        # A real StateStore snapshot has already measured age. Processing that
        # snapshot can take longer than the next read, without a new HAL frame.
        if len(snapshots) == 2:
            clock.now += .002
        return snap

    monkeypatch.setattr(a, 'snapshot', delayed_snapshot)
    origin, edge = p['origin'], p['edge']
    result = execute(a, p, Feedback(a, clock, lambda t: origin+(edge-origin)*min(t/.3, 1.)))
    first, repeated = snapshots[1], snapshots[3]
    assert first['joints']['head_j2']['receipt_seq'] == repeated['joints']['head_j2']['receipt_seq']
    assert repeated['snapshot_monotonic_s'] > first['snapshot_monotonic_s']
    assert result['recovered'] and result['elapsed_s'] >= .45
    assert transport.sent == [('joint_command', {'head_j2': edge})]


@pytest.mark.parametrize('invalid_time', [None, True, float('nan'), float('inf'), 1e9])
def test_invalid_snapshot_time_is_rejected_before_output(monkeypatch, invalid_time):
    a, clock, transport = candidate()
    p = plan(a)
    original = a.snapshot

    def invalid_snapshot():
        snap = original()
        snap['snapshot_monotonic_s'] = invalid_time
        return snap

    monkeypatch.setattr(a, 'snapshot', invalid_snapshot)
    with pytest.raises(ValueError, match='快照时间无效'):
        execute(a, p, Event(clock))
    assert not transport.sent


def test_snapshot_that_expires_during_processing_is_rejected_before_output(monkeypatch):
    a, clock, transport = candidate()
    p = plan(a)
    original = a.snapshot
    reads = 0

    def delayed_snapshot():
        nonlocal reads
        snap = original()
        reads += 1
        if reads == 2:
            clock.now += a.profile['feedback_max_age_s']+.001
        return snap

    monkeypatch.setattr(a, 'snapshot', delayed_snapshot)
    with pytest.raises((ValueError, ActionRejected), match='过期'):
        execute(a, p, Event(clock))
    assert not transport.sent


@pytest.mark.parametrize('regression', ['sequence', 'time'])
def test_genuine_receipt_regression_still_aborts_after_one_boundary_command(regression):
    a, clock, transport = candidate()
    p = plan(a)
    previous = dict(a.store.joints['head_j2'])

    def regress(t):
        row = a.store.joints['head_j2']
        if regression == 'sequence':
            row['receipt_seq'] = previous['receipt_seq']-1
        else:
            row['received_at'] = previous['received_at']-.001

    with pytest.raises(ValueError, match='接收序号或时间倒退') as error:
        execute(a, p, Feedback(a, clock, lambda t: p['origin']+.1, hook=regress))
    assert error.value.boundary_command_sent and not error.value.hold_sent
    assert transport.sent == [('joint_command', {'head_j2': p['edge']})]


def test_legal_before_publish_drifting_outside_cannot_turn_into_recovery():
    a, clock, transport = candidate()
    p = plan(a)
    a.store.update_joints(joints(['head_j2'], [-25.]))
    with pytest.raises(ValueError, match='再次越界') as error:
        execute(a, p, Feedback(a, clock, lambda t: -25.01))
    assert not error.value.commands_sent and not transport.sent

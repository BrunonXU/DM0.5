"""Candidate integration checks using the existing fake ROS transport only."""
from copy import deepcopy

import pytest

from robot.adapter import ActionRejected
from robot import head_boundary_recovery as boundary
from robot.pose_action import build
from test_pose_action import setup, live_feedback_event
from test_ros_adapter import Event, joints


def recovery_setup(origin=-27.644617):
    a, clock, transport = setup()
    a.profile.update(controller_binary_sha256=boundary.DRIVER_SHA256,
                     head_boundary_recovery={'enabled': True, 'max_outside_deg': 3., 'timeout_s': 3.})
    a.profile['limits'] = deepcopy(a.profile['joint_limits_deg'])
    a.store.update_joints(joints(['head_j2'], [origin]))
    return a, clock, transport


def pose(**targets):
    return {'kind': 'pose', 'targets': targets or {'head_j2': 0}, 'head_boundary_recovery_ack': True}


def test_preview_retains_exceptional_actual_but_does_not_create_outputs():
    a, clock, transport = recovery_setup()
    public, trajectory, _, _, keys = build(a, pose(head_j2=0, arm_l_j1=10), a._common())
    assert public['head_boundary_recovery']['origin'] == -27.644617
    assert public['head_boundary_recovery']['speed_controlled'] is False
    assert trajectory.origin['head_j2'] == -27.644617
    assert not public['no_motion'] and 'joint_command' in keys
    assert a.store.snapshot()['joints']['head_j2']['position'] == -27.644617
    assert not transport.prepared and not transport.sent


def test_j2_boundary_recovery_does_not_bypass_invalid_j1_origin():
    from robot.pose_review import review
    a, clock, transport = recovery_setup(-27.56555938720703)
    a.store.update_joints(joints(['head_j1'], [-134.1064453125]))
    spec = pose(head_j1=.001723, head_j2=-15., head_j3=-.004631, arm_l_j1=10.)
    report = review(a, spec)
    assert not report['executable']
    assert len(report['issues']) == 1
    issue = report['issues'][0]
    assert issue['code'] == 'origin_out_of_range' and issue['joint'] == 'head_j1'
    assert issue['current'] == -134.1064453125 and issue['limits'] == [-40, 40]
    with pytest.raises(ActionRejected, match='head_j1'):
        a.execute(spec, Event(clock))
    assert not transport.prepared and not transport.sent


def test_explicit_ack_is_required_before_preparing_or_publishing():
    for ack in (None, False, 1, 'true'):
        a, clock, transport = recovery_setup()
        spec = pose()
        if ack is None:
            spec.pop('head_boundary_recovery_ack')
        else:
            spec['head_boundary_recovery_ack'] = ack
        assert a.plan(spec)['head_boundary_recovery']
        with pytest.raises((ValueError, ActionRejected)):
            a.execute(spec, Event(clock))
        assert not transport.sent and not transport.prepared


def test_boundary_needed_even_when_old_joint_tolerance_would_call_no_motion():
    a, clock, transport = recovery_setup(-25.1)
    a.profile['feedback_limit_recovery_deg'] = 1.
    public = a.plan(pose(head_j2=-25))
    assert public['head_boundary_recovery']['origin'] == -25.1 and not public['no_motion']
    result = a.execute(pose(head_j2=-25), live_feedback_event(a, clock))
    assert result['status'] == 'succeeded' and result['feedback_verified']
    assert result['commands_sent'] and not result.get('no_motion', False)
    assert result['head_boundary_recovery']['boundary_command_sent']
    assert transport.sent == [('joint_command', {'head_j2': -25.})]


def test_entire_pose_starts_only_after_single_edge_command_and_confirmed_reentry():
    a, clock, transport = recovery_setup()
    rows = []
    publish = transport.publish
    def capture(key, values):
        rows.append((clock(), key, deepcopy(values)))
        publish(key, values)
    transport.publish = capture
    result = a.execute(pose(head_j2=0, body_j1=5, arm_l_j1=10), Event(clock))
    assert result['status'] == 'succeeded' and result['feedback_verified']
    assert result['head_boundary_recovery']['recovered']
    assert rows[0][1:] == ('joint_command', {'head_j2': -25.})
    assert rows[1][0]-rows[0][0] >= .15
    assert rows[1][2]['head_j2'] == -25.
    assert set(rows[1][2]).issuperset({'arm_l_j1', 'body_j1', 'head_j2'})
    assert all(-25 <= values['head_j2'] <= 25 for _, key, values in rows
               if key == 'joint_command' and 'head_j2' in values)
    assert transport.prepared.count('joint_command') == 1
    assert transport.released == ['joint_command'] and not a._action_lock.locked()


def test_post_recovery_trajectory_is_rebuilt_from_latest_actual_of_all_parts():
    a, clock, transport = recovery_setup()
    outputs = []
    publish = transport.publish
    def capture(key, values):
        outputs.append((key, deepcopy(values)))
        publish(key, values)
    transport.publish = capture
    class DriftDuringObservation(Event):
        changed = False
        def wait(self, seconds):
            result = super().wait(seconds)
            if not self.changed and outputs:
                self.changed = True
                a.store.update_joints(joints(['head_j2', 'arm_l_j1'], [-24.8, 4.]))
            return result
    result = a.execute(pose(head_j2=0, arm_l_j1=10), DriftDuringObservation(clock))
    assert result['status'] == 'succeeded'
    assert outputs[0] == ('joint_command', {'head_j2': -25.})
    assert outputs[1][1]['head_j2'] == -24.8
    assert outputs[1][1]['arm_l_j1'] == 4.


@pytest.mark.parametrize('change', [
    lambda spec: spec['targets'].update(arm_l_j1=10000),
    lambda spec: spec['targets'].update(head_j2=26),
    lambda spec: spec.update(grippers={'left': 101}),
])
def test_invalid_other_target_refuses_before_any_boundary_output(change):
    a, clock, transport = recovery_setup()
    spec = pose(head_j2=0, arm_l_j1=10)
    change(spec)
    with pytest.raises((ValueError, ActionRejected)):
        a.execute(spec, Event(clock))
    assert not transport.sent and not transport.prepared


def test_boundary_stall_reports_sent_and_never_publishes_other_parts_or_hold():
    a, clock, transport = recovery_setup()
    transport.mirror = False
    with pytest.raises((ValueError, ActionRejected)) as error:
        a.execute(pose(head_j2=0, arm_l_j1=10, body_j1=5), Event(clock))
    assert error.value.commands_sent and error.value.recovery['boundary_command_sent']
    assert not error.value.recovery['recovered'] and not error.value.hold_sent
    assert transport.sent == [('joint_command', {'head_j2': -25.})]
    assert transport.released == ['joint_command'] and not a._action_lock.locked()


def test_cancel_during_boundary_observation_never_turns_into_normal_hold():
    a, clock, transport = recovery_setup()
    transport.mirror = False
    result = a.execute(pose(head_j2=0, arm_l_j1=10), Event(clock, after=clock()+.08))
    assert result['status'] == 'cancelled' and result['commands_sent']
    assert not result['feedback_verified'] and not result.get('hold_sent', False)
    assert result['head_boundary_recovery']['boundary_command_sent']
    assert not result['head_boundary_recovery']['physical_stop_confirmed']
    assert transport.sent == [('joint_command', {'head_j2': -25.})]


def test_cancel_at_transition_cannot_emit_any_ordinary_part(monkeypatch):
    a, clock, transport = recovery_setup()
    event = Event(clock)
    original = boundary.execute
    def cancelling(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result
    monkeypatch.setattr(boundary, 'execute', cancelling)
    result = a.execute(pose(head_j2=0, arm_l_j1=10), event)
    assert result['status'] == 'cancelled' and result['commands_sent']
    assert result['head_boundary_recovery']['recovered']
    assert transport.sent == [('joint_command', {'head_j2': -25.})]


def test_other_part_becoming_invalid_after_boundary_prevents_normal_output(monkeypatch):
    a, clock, transport = recovery_setup()
    original = boundary.execute
    def change_other(*args, **kwargs):
        result = original(*args, **kwargs)
        a.store.update_joints(joints(['arm_l_j1'], [999.]))
        return result
    monkeypatch.setattr(boundary, 'execute', change_other)
    with pytest.raises((ValueError, ActionRejected)) as error:
        a.execute(pose(head_j2=0, arm_l_j1=10), Event(clock))
    assert error.value.commands_sent and error.value.recovery['recovered']
    assert not error.value.hold_sent
    assert transport.sent == [('joint_command', {'head_j2': -25.})]


def test_leaving_range_at_transition_never_retries_or_clamps_start(monkeypatch):
    a, clock, transport = recovery_setup()
    a.profile['feedback_limit_recovery_deg'] = 1.
    original = boundary.execute
    calls = []
    def fall_outside(*args, **kwargs):
        calls.append(True)
        result = original(*args, **kwargs)
        a.store.update_joints(joints(['head_j2'], [-25.01]))
        return result
    monkeypatch.setattr(boundary, 'execute', fall_outside)
    with pytest.raises((ValueError, ActionRejected)) as error:
        a.execute(pose(head_j2=0, arm_l_j1=10), Event(clock))
    assert error.value.commands_sent and error.value.recovery['recovered']
    assert len(calls) == 1 and transport.sent == [('joint_command', {'head_j2': -25.})]


def test_existing_head1_and_legacy_interfaces_cannot_implicitly_enable_recovery():
    for spec in (pose(head_j1=0), {'kind': 'joints', 'targets': {'head_j2': 0},
                                  'head_boundary_recovery_ack': True,
                                  'exceptional_origins': {'head_j2': -27.644617}}):
        a, clock, transport = recovery_setup()
        with pytest.raises((ValueError, ActionRejected)):
            a.execute(spec, Event(clock))
        assert not transport.sent


def test_disabled_recovery_preserves_original_whole_pose_rejection():
    a, clock, transport = recovery_setup()
    a.profile['head_boundary_recovery']['enabled'] = False
    with pytest.raises((ValueError, ActionRejected)):
        a.execute(pose(head_j2=0, arm_l_j1=10), Event(clock))
    assert not transport.sent and not transport.prepared


def test_exclusivity_conflict_before_boundary_is_zero_output():
    a, clock, transport = recovery_setup()
    transport.conflict = True
    with pytest.raises(ActionRejected, match='external'):
        a.execute(pose(head_j2=0, arm_l_j1=10), Event(clock))
    assert not transport.sent


def test_external_source_during_post_boundary_noop_observation_cannot_succeed():
    a, clock, transport = recovery_setup()
    start = clock()
    # The boundary confirms around 0.16s. The following no-motion observation
    # must retain source exclusion because this task has already commanded J2.
    class ConflictAfterBoundary(Event):
        def wait(self, seconds):
            result = super().wait(seconds)
            row = a.store.snapshot()['joints']
            a.store.update_joints(joints(list(row), [v['position'] for v in row.values()]))
            if clock()-start >= .20:
                transport.conflict = True
            return result
    with pytest.raises(ActionRejected, match='external') as error:
        a.execute(pose(head_j2=-25), ConflictAfterBoundary(clock))
    assert error.value.commands_sent and error.value.recovery['recovered']
    assert not error.value.hold_sent
    assert transport.sent == [('joint_command', {'head_j2': -25.})]

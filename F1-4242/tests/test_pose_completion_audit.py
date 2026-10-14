"""Reproductions of pose budget, selected-source and terminal-state races."""
import threading
from types import SimpleNamespace

import pytest

from core import ActionManager
from robot.adapter import ActionRejected
from test_pose_action import setup, live_feedback_event
from test_ros_adapter import Event, joints


def refresh_feedback(a, clock):
    snapshot = a.store.snapshot()
    row = snapshot['joints']
    a.store.update_joints(joints(list(row), [v['position'] for v in row.values()]))
    for side, value in snapshot['grippers'].items():
        a.store.update_gripper(side, joints([side+'_gripper_position'], [value['position']]))
    a.store.graph_at = clock()


def test_longer_second_plan_updates_budget_before_starting_mixed_outputs():
    a, clock, transport = setup()
    spec = {'kind': 'pose', 'targets': {'body_j3': -5}, 'grippers': {'left': 30}}
    assert a.plan(spec)['max_action_s'] == 6
    prepare = transport.prepare
    changed = False
    def different_actual(key):
        nonlocal changed
        prepare(key)
        if not changed:
            changed = True
            a.store.update_joints(joints(['body_j3'], [-70]))
    transport.prepare = different_actual
    start = clock()
    result = a.execute(spec, Event(clock))
    assert result['status'] == 'succeeded' and result['feedback_verified']
    assert 8 < clock()-start < 12
    assert a.store.snapshot()['joints']['body_j3']['position'] == pytest.approx(-5)
    assert a.store.snapshot()['grippers']['left']['position'] == pytest.approx(30)
    assert {'joint_command', 'left_gripper_command'} == {key for key, _ in transport.sent}


def test_preparation_time_cannot_reset_the_absolute_sixty_second_budget():
    a, clock, transport = setup()
    prepare = transport.prepare
    def delayed_prepare(key):
        prepare(key)
        clock.now += 59.9
        refresh_feedback(a, clock)
    transport.prepare = delayed_prepare
    with pytest.raises((ValueError, ActionRejected)):
        a.execute({'kind': 'pose', 'targets': {'body_j1': 10}}, Event(clock))
    assert not transport.sent
    assert not a._action_lock.locked() and transport.released == ['joint_command']


def test_arrival_during_long_preparation_does_not_restart_noop_deadline():
    a, clock, transport = setup()
    prepare = transport.prepare
    def delayed_arrival(key):
        prepare(key)
        clock.now += 59.9
        a.store.update_joints(joints(['body_j1'], [10]))
        refresh_feedback(a, clock)
    transport.prepare = delayed_arrival
    start = clock()
    with pytest.raises((ValueError, ActionRejected)):
        a.execute({'kind': 'pose', 'targets': {'body_j1': 10}}, live_feedback_event(a, clock))
    assert clock()-start < 60.05 and not transport.sent


def test_initial_noop_planning_time_is_part_of_absolute_task_budget():
    a, clock, transport = setup()
    joint_target = a._joint_target
    def delayed_plan(*args, **kwargs):
        result = joint_target(*args, **kwargs)
        clock.now += 59.95
        refresh_feedback(a, clock)
        return result
    a._joint_target = delayed_plan
    start = clock()
    with pytest.raises((ValueError, ActionRejected)):
        a.execute({'kind': 'pose', 'targets': {'body_j1': 0}}, live_feedback_event(a, clock))
    assert clock()-start < 60.05 and not transport.sent and not transport.prepared


@pytest.mark.parametrize('spec,guard_key', [
    ({'kind': 'pose', 'targets': {'body_j1': 10}, 'grippers': {'left': 10}}, 'left_gripper_command'),
    ({'kind': 'pose', 'targets': {'body_j1': 0}, 'grippers': {'left': 30}}, 'joint_command'),
    ({'kind': 'pose', 'targets': {'body_j1': 0}, 'grippers': {'left': 10}}, 'left_gripper_command'),
])
def test_selected_stationary_part_conflict_rejects_before_other_parts_move(spec, guard_key):
    a, clock, transport = setup()
    check = transport.assert_exclusive
    def selected_conflict(key):
        if key == guard_key:
            raise ActionRejected('external selected part')
        check(key)
    transport.assert_exclusive = transport.assert_exclusive_cached = selected_conflict
    with pytest.raises(ActionRejected, match='external selected part'):
        a.execute(spec, Event(clock))
    assert not transport.sent and not transport.prepared


def test_stationary_gripper_becoming_externally_controlled_cancels_moving_body():
    a, clock, transport = setup()
    start = clock()
    check = transport.assert_exclusive
    def later_conflict(key):
        if key == 'left_gripper_command' and clock()-start >= .1:
            raise ActionRejected('external stationary gripper')
        check(key)
    transport.assert_exclusive = transport.assert_exclusive_cached = later_conflict
    with pytest.raises(ActionRejected, match='external stationary gripper') as error:
        a.execute({'kind': 'pose', 'targets': {'body_j1': 10}, 'grippers': {'left': 10}}, Event(clock))
    assert error.value.commands_sent and clock()-start < .2
    assert 'left_gripper_command' not in transport.prepared
    assert {key for key, _ in transport.sent} == {'joint_command'}
    assert a.store.snapshot()['joints']['body_j1']['position'] < 10


def test_stop_after_terminal_result_cannot_leave_finished_action_stopping():
    logging, finish_log = threading.Event(), threading.Event()
    class BlockingResultLog:
        def add(self, event, *args, **kwargs):
            if event == 'action_result':
                logging.set()
                assert finish_log.wait(2), 'test did not release completion log'
    adapter = SimpleNamespace(execute=lambda *args: {
        'status': 'succeeded', 'commands_sent': True, 'feedback_verified': True})
    actions = ActionManager(adapter, BlockingResultLog())
    actions.start({'kind': 'pose', 'targets': {'head_j2': 0}}, 'operator')
    try:
        assert logging.wait(1)
        assert actions.latest()['status'] == 'succeeded' and actions.busy()
        actions.stop()
    finally:
        finish_log.set()
        actions.thread.join(1)
    assert not actions.busy()
    # The completed and verified pose must not become an immortal "stopping"
    # entry merely because its result log was still being written.
    assert actions.latest()['status'] == 'succeeded'
    assert actions.latest()['result']['feedback_verified']

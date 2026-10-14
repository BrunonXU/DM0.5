"""Arrival evidence uses the confirming ROS samples, never a later read/target."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from core import ActionManager, EventLog
from robot import pose_action
from test_pose_action import setup
from test_ros_adapter import Event, joints


def observed_adapter(joint_errors=None, gripper_errors=None):
    a, clock, transport = setup()
    a.profile.update(id='arrival-test', version='test-only')
    joint_errors = joint_errors or {}
    gripper_errors = gripper_errors or {}
    transport.mirror = False
    publish = transport.publish

    def tracking(key, values):
        publish(key, values)
        if key == 'joint_command':
            a.store.update_joints(joints(list(values), [v+joint_errors.get(n, 0.) for n, v in values.items()]))
        elif key.endswith('_gripper_command'):
            side = key.split('_')[0]
            a.store.update_gripper(side, joints([side+'_gripper_position'], [values+gripper_errors.get(side, 0.)]))
    transport.publish = tracking
    check = transport.assert_exclusive

    def receive(key):
        check(key)
        # Model independently arriving groups: their receipt ages need not be
        # zero or equal, even on the final confirming observation.
        for value in a.store.joints.values():
            value['received_at'] = clock()-.02
        for value in a.store.grippers.values():
            value['received_at'] = clock()-.03
    transport.assert_exclusive = transport.assert_exclusive_cached = receive
    return a, clock, transport


def capture_feedback(monkeypatch, clock):
    observed = {}
    original = pose_action.feedback

    def capture(adapter, snap, public):
        items, parts = original(adapter, snap, public)
        observed.update(items=deepcopy(items), refs=items, public=deepcopy(public), now=clock())
        return items, parts
    monkeypatch.setattr(pose_action, 'feedback', capture)
    return observed


def assert_observation(result, observed, profile):
    assert result['status'] == 'succeeded' and result['feedback_verified']
    evidence = result['arrival_feedback']
    assert evidence['scope'] == 'selected_targets_only'
    assert evidence['basis'] == 'received_ros_feedback'
    assert evidence['freshness_scope'] == 'gateway_receipt_only'
    assert evidence['error_convention'] == 'actual_minus_target'
    assert evidence['observed_monotonic_s'] == observed['now']
    assert evidence['minimum_confirmation_s'] == .15
    assert evidence['profile'] == {'id': 'arrival-test', 'version': 'test-only'}
    assert set(evidence['joints']) == set(observed['public']['targets'])
    assert set(evidence['grippers']) == set(observed['public']['grippers'])
    for kind, unit, tolerance in [('joints', 'deg', profile['joint_tolerance_deg']),
                                  ('grippers', '0-100', profile['gripper_tolerance'])]:
        targets = observed['public']['targets' if kind == 'joints' else 'grippers']
        for name, value in evidence[kind].items():
            source = observed['items'][name if kind == 'joints' else name+'_gripper']
            assert value['target'] == targets[name]
            assert value['actual'] == source['position']
            assert value['error'] == pytest.approx(source['position']-targets[name])
            assert value['unit'] == unit and value['tolerance'] == tolerance
            assert value['age_s'] == source['age_s']
            assert value['receipt_seq'] == source['receipt_seq']
            assert value.get('source_stamp') == source.get('source_stamp')
    return evidence


def test_moving_pose_records_nonzero_final_joint_and_gripper_errors(monkeypatch):
    a, clock, transport = observed_adapter(
        {'arm_l_j1': .25, 'arm_r_j1': -.4, 'body_j1': .3, 'head_j2': -.2},
        {'left': 1.5, 'right': -2.})
    spec = {'kind': 'pose', 'targets': {'arm_l_j1': 10, 'arm_r_j1': -12, 'body_j1': 5, 'head_j2': 4},
            'grippers': {'left': 30, 'right': 35}}
    observed = capture_feedback(monkeypatch, clock)
    result = a.execute(spec, Event(clock))
    evidence = assert_observation(result, observed, a.profile)
    assert len(evidence['joints']) == 22
    for name, error in {'arm_l_j1': .25, 'arm_r_j1': -.4, 'body_j1': .3, 'head_j2': -.2}.items():
        assert evidence['joints'][name]['error'] == pytest.approx(error)
        assert evidence['joints'][name]['age_s'] == pytest.approx(.02)
    assert evidence['grippers']['left']['actual'] == pytest.approx(31.5)
    assert evidence['grippers']['right']['actual'] == pytest.approx(33.)
    assert evidence['grippers']['left']['age_s'] == pytest.approx(.03)
    assert result['commands_sent'] and transport.sent


def test_no_motion_records_confirmed_residual_without_output(monkeypatch):
    a, clock, transport = observed_adapter()
    a.store.update_joints(joints(['head_j2'], [-.35]))
    a.store.update_gripper('left', joints(['left_gripper_position'], [12.]))
    observed = capture_feedback(monkeypatch, clock)
    result = a.execute({'kind': 'pose', 'targets': {'head_j2': 0}, 'grippers': {'left': 10}}, Event(clock))
    evidence = assert_observation(result, observed, a.profile)
    assert evidence['joints']['head_j2']['error'] == pytest.approx(-.35)
    assert evidence['grippers']['left']['error'] == pytest.approx(2.)
    assert set(evidence['joints']) == {'head_j1', 'head_j2', 'head_j3'}
    assert result['no_motion'] and not result['commands_sent']
    assert not transport.prepared and not transport.sent


def test_mixed_pose_records_already_arrived_parts_as_well_as_moving_parts(monkeypatch):
    a, clock, transport = observed_adapter({'arm_l_j1': .25}, {'right': -2.})
    a.store.update_joints(joints(['head_j2'], [-.35]))
    a.store.update_gripper('left', joints(['left_gripper_position'], [12.]))
    observed = capture_feedback(monkeypatch, clock)
    result = a.execute({'kind': 'pose', 'targets': {'arm_l_j1': 10, 'head_j2': 0},
                        'grippers': {'left': 10, 'right': 35}}, Event(clock))
    evidence = assert_observation(result, observed, a.profile)
    assert evidence['joints']['arm_l_j1']['error'] == pytest.approx(.25)
    assert evidence['joints']['head_j2']['error'] == pytest.approx(-.35)
    assert evidence['grippers']['left']['error'] == pytest.approx(2.)
    assert evidence['grippers']['right']['error'] == pytest.approx(-2.)
    assert 'left_gripper_command' not in transport.prepared
    assert not any(n.startswith('head_') for key, values in transport.sent if key == 'joint_command' for n in values)


@pytest.mark.parametrize('no_motion', [False, True])
def test_record_is_from_confirming_frame_and_owns_its_nested_stamp(monkeypatch, no_motion):
    a, clock, transport = observed_adapter({'arm_l_j1': .25})
    observed = capture_feedback(monkeypatch, clock)
    first, changed = {}, []

    def progress(row):
        if not row['feedback_arrived']:
            return
        receipts = {n: (v['receipt_seq'], observed['now']-v['age_s']) for n, v in observed['items'].items()}
        if not first:
            first.update(receipts)
        confirmed = all(seq != first[n][0] and at-first[n][1] >= .15 for n, (seq, at) in receipts.items())
        # no_motion's progress flag already means the entire observation passed.
        if no_motion or confirmed:
            changed.append(True)
            a.store.update_joints(joints(['arm_l_j1'], [99.]))
            monkeypatch.setattr(a, '_common', lambda: pytest.fail('arrival evidence reread a later snapshot'))
    result = a.execute({'kind': 'pose', 'targets': {'arm_l_j1': .1 if no_motion else 10}}, Event(clock), progress)
    assert changed
    evidence = assert_observation(result, observed, a.profile)
    saved = deepcopy(evidence)
    assert evidence['joints']['arm_l_j1']['actual'] != 99.
    for value in observed['refs'].values():
        value['position'] = 123.
        value['source_stamp']['sec'] = 1
    for value in a.store.joints.values():
        value['position'] = 123.
        value['source_stamp']['sec'] = 2
    a.profile['id'] = 'later-profile'
    assert evidence == saved


def test_missing_source_stamp_is_not_invented(monkeypatch):
    a, clock, transport = observed_adapter()
    receive = transport.assert_exclusive

    def missing_stamp(key):
        receive(key)
        for value in a.store.grippers.values():
            value['source_stamp'] = None
    transport.assert_exclusive = transport.assert_exclusive_cached = missing_stamp
    observed = capture_feedback(monkeypatch, clock)
    result = a.execute({'kind': 'pose', 'grippers': {'left': 11}}, Event(clock))
    evidence = assert_observation(result, observed, a.profile)
    assert evidence['joints'] == {}
    assert 'source_stamp' not in evidence['grippers']['left']
    assert evidence['grippers']['left']['actual'] == 10.


@pytest.mark.parametrize('no_motion', [False, True])
def test_adapter_cancel_does_not_attach_success_evidence(no_motion):
    a, clock, transport = observed_adapter({'arm_l_j1': .25})
    result = a.execute({'kind': 'pose', 'targets': {'arm_l_j1': .1 if no_motion else 10}},
                       Event(clock, after=clock()+.1))
    assert result['status'] == 'cancelled' and not result['feedback_verified']
    assert 'arrival_feedback' not in result


def test_failed_arrival_does_not_attach_success_evidence():
    a, clock, transport = observed_adapter()
    transport.publish = lambda key, value: transport.sent.append((key, deepcopy(value)))
    with pytest.raises(ValueError, match='全部到位|未跟上') as error:
        a.execute({'kind': 'pose', 'targets': {'head_j2': 5}}, Event(clock))
    assert error.value.commands_sent and not hasattr(error.value, 'arrival_feedback')


def test_action_manager_persists_complete_arrival_evidence(tmp_path):
    a, clock, transport = observed_adapter({'head_j2': -.2})
    # Run the real pose executor on a virtual clock, while retaining the actual
    # ActionManager and EventLog result-writing path. No robot or wall-time wait.
    virtual = SimpleNamespace(execute=lambda spec, cancel, progress:
                              a.execute(spec, Event(clock), progress))
    path = tmp_path/'events.jsonl'
    actions = ActionManager(virtual, EventLog(path))
    actions.start({'kind': 'pose', 'targets': {'head_j2': 5}}, 'operator')
    actions.thread.join(2)
    assert not actions.busy()
    current = actions.latest()
    assert current['status'] == 'succeeded' and current['result']['feedback_verified']
    persisted = [json.loads(line) for line in path.read_text().splitlines()]
    terminal = [row for row in persisted if row['kind'] == 'action_result']
    assert len(terminal) == 1 and terminal[0]['message'] == 'succeeded'
    evidence = terminal[0]['result']['arrival_feedback']
    assert evidence == current['result']['arrival_feedback']
    assert evidence['joints']['head_j2']['target'] == 5.
    assert evidence['joints']['head_j2']['actual'] == pytest.approx(4.8)
    assert evidence['joints']['head_j2']['error'] == pytest.approx(-.2)

from types import SimpleNamespace
from dataclasses import replace
import numpy as np
import pytest
from f1dagger.robot_io import F1Owner
from f1dagger.teleop import GripperIntent
from test_real_observation_shadow import packet

class Output:
    def __init__(self):self.reset()
    def reset(self):self.position=None;self.fault=None
    def push(self,target,now,enabled):
        if self.position is None:self.position=np.array(target)
        self.target=np.array(target);self.enabled=enabled
    def sample(self,now):
        self.position=self.target.copy();return self.position.copy()

class Transport:
    rmw_gids={k:'aa' for k in ('joint_command','left_gripper_command','right_gripper_command')}
    own_gids={}
    def __init__(self):self.prepared=[];self.sent=[];self.conflict=False
    def prepare(self,key):self.prepared.append(key)
    def assert_exclusive_cached(self,key):
        if self.conflict:raise RuntimeError('external command')
    def publish(self,key,value):self.sent.append((key,value))
    def release(self,key):pass

def make_owner(powered=True):
    t=Transport();state=np.r_[np.zeros(14),32.,66.]
    source=SimpleNamespace(feedback=lambda:(np.zeros(22),state,(1,)*16))
    mapping=SimpleNamespace(check_action=lambda x:np.array(x))
    gate=SimpleNamespace(check=lambda *a:True)
    owner=F1Owner(t,source,mapping,Output(),gate,powered=powered)
    owner.initialize(1.);return owner,t

def test_shadow_and_initialization_never_publish():
    o,t=make_owner(False)
    o.prepare();o.approve(o.last_submitted,1.);o.publish(1.01)
    assert not t.prepared and not t.sent
    o,t=make_owner(True)
    assert not t.prepared and not t.sent

def test_actual_publication_and_gripper_hold_timestamps():
    o,t=make_owner();o.prepare()
    o.publish(1.01)
    old=list(o.field_times)
    assert len(t.sent)==3
    o.publish(1.02)
    assert len(t.sent)==4 and o.field_times[14:]==old[14:]
    t.conflict=True
    with pytest.raises(RuntimeError,match='external'):o.publish(1.03)
    assert len(t.sent)==4

def test_handoff_flushes_old_trajectory_target():
    o,t=make_owner();o.prepare()
    target=o.last_submitted.copy();target[0]=20
    o.approve(target,1.01)
    o.hold(1.02)
    command,_,_=o.publish(1.03)
    assert command[0]==0  # Old pending target cannot leak through smoothing.

def test_rt_does_not_replay_latched_state_on_takeover():
    g=GripperIntent();p=replace(packet(0),active_side='left',grip_target=1.)
    g.anchor(np.r_[np.zeros(14),43.,66.],p)
    assert list(g.update(p))==[43,66]
    assert list(g.update(replace(p,grip_target=0.)))==[0,66]


@pytest.mark.parametrize('failure', ['feedback', 'stale_target', 'output_fault', 'invalid_target'])
def test_failed_prepublication_check_does_not_reuse_previous_success(failure):
    owner, transport = make_owner()
    owner.prepare()
    _, _, successful = owner.publish(1.01)
    assert successful['publication_complete']
    if failure == 'feedback':
        owner.source.feedback = lambda: (_ for _ in ()).throw(RuntimeError('stale feedback'))
    elif failure == 'output_fault':
        owner.output.fault = 'injected fault'
    elif failure == 'invalid_target':
        owner.mapping.check_action = lambda _: (_ for _ in ()).throw(ValueError('invalid target'))
    now = 2. if failure == 'stale_target' else 1.02
    with pytest.raises((RuntimeError, ValueError)):
        owner.publish(now)
    evidence = owner.last_evidence
    assert evidence is not successful
    assert not evidence['publication_complete'] and not evidence['publications']
    assert evidence['previous_command_sequence'] == 1
    assert 'completed_monotonic_ns' not in evidence
    assert len(transport.sent) == 3 and owner.sequence == 1
    assert successful['publication_complete']  # Already queued evidence stays immutable.


def test_partial_publication_keeps_exact_completed_topics_and_failed_stage():
    owner, transport = make_owner()
    owner.prepare()
    owner.publish(1.01)
    previous = owner.last_submitted.copy()
    target = previous.copy()
    target[0], target[14:] = 2., (0., 100.)
    owner.approve(target, 1.02)
    publish = transport.publish
    def fail_right(key, value):
        if key == 'right_gripper_command':
            raise OSError('injected ROS failure')
        publish(key, value)
    transport.publish = fail_right
    with pytest.raises(OSError, match='injected'):
        owner.publish(1.03)
    evidence = owner.last_evidence
    assert not evidence['publication_complete']
    assert evidence['stage'] == 'publish' and evidence['topic_key'] == 'right_gripper_command'
    assert [p['topic_key'] for p in evidence['publications']] == ['joint_command', 'left_gripper_command']
    assert evidence['attempted_command'] == target.tolist()
    assert evidence['action_source_timestamps_ns'] == owner.field_times
    assert owner.sequence == 1
    np.testing.assert_array_equal(owner.last_submitted, previous)


def test_output_fault_retains_pre_sample_dynamics_after_hold_resets_planner():
    owner, transport = make_owner()
    owner.prepare()
    owner.publish(1.01)
    owner.output.sampled, owner.output.updated = 1.01, 1.0
    owner.output.planner = SimpleNamespace(position=[1.] * 14, velocity=[2.] * 14,
        acceleration=[3.] * 14, filtered_target=[4.] * 14,
        max_velocity=30., max_acceleration=100., max_jerk=600., recovery_events=2)

    def failed_sample(now):
        owner.output.planner.velocity[:] = [0.] * 14
        owner.output.planner.acceleration[:] = [0.] * 14
        owner.output.fault = 'planner failure: captured Ruckig error'
        owner.output.timed_out = False
        return owner.output.position.copy()

    owner.output.sample = failed_sample
    with pytest.raises(RuntimeError, match='latched: planner failure: captured Ruckig error'):
        owner.publish(1.014)
    evidence = owner.last_evidence
    assert evidence['stage'] == 'output_sample'
    assert evidence['output_fault'] == 'planner failure: captured Ruckig error'
    diagnostics = evidence['output_diagnostics']
    assert diagnostics['output_gap_ms'] == pytest.approx(4.)
    assert diagnostics['target_age_ms'] == pytest.approx(14.)
    assert diagnostics['planner']['velocity'] == [2.] * 14
    assert diagnostics['planner']['acceleration'] == [3.] * 14
    assert diagnostics['planner']['max_jerk'] == 600.
    assert diagnostics['timed_out'] is False
    assert owner.output.fault is None  # hold reset cannot erase retained evidence.
    assert not evidence['publication_complete'] and evidence['publications'] == []
    assert owner.sequence == 1 and len(transport.sent) == 3

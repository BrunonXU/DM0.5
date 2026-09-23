"""Offline contract tests; never import ROS or contact the robot."""
from copy import deepcopy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot.adapter import ARM_NAMES, ActionRejected, RosAdapter, RosTransport, StateStore


class Clock:
    def __init__(self): self.now = 10.0
    def __call__(self): return self.now


def joints(names, positions):
    return NS(name=names, position=positions, header=NS(stamp=NS(sec=946600000, nanosec=7)))


def base():
    return NS(speed=NS(linear=NS(x=0.0, y=0.0), angular=NS(z=0.0)),
              odometry=NS(position=NS(x=2.0, y=3.0), orientation=NS(w=0.7)),
              is_soft_auto=False, is_hard_auto=True, is_parking_mode=False, is_avoid=False)


class Event:
    def __init__(self, clock, after=None): self.clock, self.after, self.flag = clock, after, False
    def is_set(self): return self.flag or (self.after is not None and self.clock() >= self.after)
    def set(self): self.flag = True
    def wait(self, seconds): self.clock.now += seconds; return self.is_set()


class Transport:
    def __init__(self):
        self.prepared, self.sent, self.released, self.own_gids = [], [], [], {}
        self.conflict, self.mirror, self.refresh_feedback = False, True, True
    def start(self): self.adapter.store.connected = True
    def close(self): self.adapter.store.connected = False
    def assert_exclusive(self, key):
        self.adapter.store.graph_at = self.adapter.clock()
        if self.conflict: raise ActionRejected('unknown external publisher')
        if self.refresh_feedback:
            for state in self.adapter.store.joints.values(): state['received_at'] = self.adapter.clock()
            for state in self.adapter.store.grippers.values(): state['received_at'] = self.adapter.clock()
            if self.adapter.store.base: self.adapter.store.base['received_at'] = self.adapter.clock()
    def prepare(self, key): self.prepared.append(key)
    def publish(self, key, value):
        self.sent.append((key, deepcopy(value)))
        if self.mirror and key == 'joint_command':
            self.adapter.store.update_joints(joints(list(value), list(value.values())))
        if self.mirror and 'gripper' in key:
            side = key.split('_')[0]
            self.adapter.store.update_gripper(side, joints([side+'_gripper_position'], [value]))
    def release(self, key): self.released.append(key)


def adapter():
    clock, transport = Clock(), Transport()
    profile = {'motion_enabled': True, 'control_contract_verified': True, 'joints_enabled': True,
               'limits_verified': True, 'gripper_verified': True, 'base_verified': True,
               'graph_observation_s': 0, 'joint_limits_deg': {n: [-170, 170] for n in ARM_NAMES}}
    a = RosAdapter(profile, clock=clock, transport=transport)
    transport.adapter = a
    a.start()
    a.store.graph_at = clock()
    a.store.update_joints(joints(list(ARM_NAMES), [0.0]*14))
    a.store.update_gripper('left', joints(['left_gripper_position'], [10.0]))
    a.store.update_base(base())
    return a, clock, transport


class FeedbackTests(unittest.TestCase):
    def test_per_joint_age_and_clock_domains(self):
        clock, store = Clock(), None
        store = StateStore(clock)
        store.update_joints(joints(['arm_l_j1'], [23.0]))
        clock.now += 1
        store.update_joints(joints(['body_j3'], [-10.0]))
        snap = store.snapshot()
        self.assertEqual(snap['joints']['arm_l_j1']['age_s'], 1)
        self.assertEqual(snap['joints']['body_j3']['age_s'], 0)
        self.assertEqual(snap['joints']['arm_l_j1']['source_stamp']['sec'], 946600000)
        snap['joints']['arm_l_j1']['position'] = 99
        self.assertEqual(store.snapshot()['joints']['arm_l_j1']['position'], 23)

    def test_invalid_values_remove_only_affected_feedback(self):
        store = StateStore()
        store.update_joints(joints(['arm_l_j1','arm_l_j2'], [1.0,2.0]))
        store.update_joints(joints(['arm_l_j1','arm_l_j1'], [4.0,5.0]))
        self.assertNotIn('arm_l_j1', store.snapshot()['joints'])
        self.assertEqual(store.snapshot()['joints']['arm_l_j2']['position'], 2)
        store.update_gripper('left', joints(['left_gripper_position'], [float('nan')]))
        self.assertNotIn('left', store.snapshot()['grippers'])

    def test_base_yaw_scalar_and_camera_bytes_not_in_json(self):
        store = StateStore()
        store.update_base(base())
        self.assertEqual(store.snapshot()['base']['pose']['yaw'], .7)
        store.update_camera('head', NS(data=b'\xff\xd8fake', format='jpeg'))
        self.assertEqual(store.camera('head')[:2], (b'\xff\xd8fake', 'image/jpeg'))
        self.assertNotIn('data', store.snapshot()['cameras']['head'])
        store.update_camera('head', NS(data=b'not-a-jpeg', format='jpeg'))
        self.assertEqual(store.camera('head')[0], b'\xff\xd8fake')


class ActionTests(unittest.TestCase):
    def test_start_creates_no_command_and_cancelled_action_sends_nothing(self):
        a, clock, t = adapter()
        self.assertEqual(t.prepared, [])
        event = Event(clock); event.set()
        result = a.execute({'kind':'base', 'vx':.1,'vy':0,'wz':0}, event)
        self.assertFalse(result['commands_sent']); self.assertEqual(t.sent, [])

    def test_missing_or_stale_feedback_never_becomes_zero_target(self):
        a, clock, t = adapter()
        del a.store.joints['arm_l_j7']
        with self.assertRaisesRegex(ActionRejected, '缺失'):
            a.execute({'kind':'joints','targets':{'arm_l_j1':1}, 'duration_s':2}, Event(clock))
        self.assertEqual(t.sent, [])
        a.store.update_joints(joints(['arm_l_j7'], [0.0]))
        clock.now += 1
        a.store.graph_at = clock()
        with self.assertRaisesRegex(ActionRejected, '过期'):
            a.execute({'kind':'joints','targets':{'arm_l_j1':1}, 'duration_s':2}, Event(clock))

    def test_nan_limits_delta_rejected_before_publisher(self):
        for target in (float('nan'), 171.0, 9.0):
            a, clock, t = adapter()
            with self.assertRaises(ActionRejected):
                a.execute({'kind':'joints','targets':{'arm_l_j1':target}, 'duration_s':2}, Event(clock))
            self.assertEqual(t.prepared, [])

    def test_unknown_publisher_refuses_gripper_and_releases_outputs(self):
        a, clock, t = adapter(); t.conflict = True
        with self.assertRaisesRegex(ActionRejected, 'unknown'):
            a.execute({'kind':'gripper','side':'left','position':20}, Event(clock))
        self.assertEqual(t.sent, [])
        self.assertEqual(t.released, ['left_gripper_command'])

    def test_partial_target_expands_arm_from_measured_and_arrival_verified(self):
        a, clock, t = adapter()
        a.store.update_joints(joints(['arm_l_j7'], [7.0]))
        result = a.execute({'kind':'joints','targets':{'arm_l_j1':1.0},'duration_s':1.0}, Event(clock))
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['feedback_verified'])
        self.assertEqual(set(t.sent[0][1]), set(ARM_NAMES[:7]))
        self.assertEqual(t.sent[0][1]['arm_l_j7'], 7.0)
        self.assertEqual(t.released, ['joint_command'])

    def test_sent_but_never_arrived_is_failure_not_success(self):
        a, clock, t = adapter(); t.mirror = False
        with self.assertRaisesRegex(ActionRejected, '未在时限内到位'):
            a.execute({'kind':'joints','targets':{'arm_l_j1':4.0},'duration_s':2.0}, Event(clock))
        self.assertTrue(t.sent); self.assertEqual(t.released, ['joint_command'])

    def test_base_ttl_zero_and_no_zero_if_never_commanded(self):
        a, clock, t = adapter()
        result = a.execute({'kind':'base','vx':.1,'vy':0,'wz':0,'ttl_s':.1}, Event(clock))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(t.sent[-3:], [('base_command',(0.,0.,0.))]*3)
        self.assertEqual(t.released, ['base_command'])
        a, clock, t = adapter()
        with self.assertRaises(ActionRejected):
            a.execute({'kind':'base','vx':.1,'vy':0,'wz':0,'ttl_s':10}, Event(clock))
        self.assertEqual(t.sent, [])

    def test_cancel_joint_holds_measured_not_zero(self):
        a, clock, t = adapter()
        a.store.update_joints(joints(list(ARM_NAMES[:7]), [10.]*7))
        result = a.execute({'kind':'joints','targets':{'arm_l_j1':11}, 'duration_s':1}, Event(clock, clock()+.15))
        self.assertEqual(result['status'], 'cancelled')
        self.assertTrue(result['hold_sent'])
        self.assertEqual(t.sent[-1][1]['arm_l_j7'], 10)

    def test_renew_base_only_while_unexpired_same_action(self):
        a, clock, t = adapter()
        spec = {'kind':'base','vx':.1,'vy':0,'wz':0,'ttl_s':.2}
        self.assertFalse(a.renew_base(spec))
        a._base_request = ((.1,0,0), clock()+.1)
        self.assertTrue(a.renew_base(spec))
        self.assertAlmostEqual(a._base_request[1], clock()+.2)
        clock.now += .3
        a.store.graph_at = clock()
        a.store.update_base(base())
        self.assertFalse(a.renew_base(spec))

    def test_readonly_and_contract_gates_do_not_create_publishers(self):
        for key in ('motion_enabled', 'control_contract_verified', 'limits_verified'):
            a, clock, t = adapter(); a.profile[key] = False
            with self.assertRaises(ActionRejected):
                a.execute({'kind':'joints','targets':{'arm_l_j1':1.0},'duration_s':1.0}, Event(clock))
            self.assertEqual(t.prepared, [])

    def test_slow_graph_cannot_publish_after_base_deadline(self):
        a, clock, t = adapter()
        original=t.assert_exclusive; calls=[0]
        def slow(key):
            calls[0]+=1
            if calls[0]==2: clock.now+=.5
            original(key)
        t.assert_exclusive=slow
        result=a.execute({'kind':'base','vx':.1,'vy':0,'wz':0,'ttl_s':.1},Event(clock))
        self.assertFalse(result['commands_sent']);self.assertEqual(t.sent,[])

    def test_stop_during_graph_check_sends_no_extra_gripper_command(self):
        a, clock, t = adapter(); event=Event(clock)
        original=t.assert_exclusive; calls=[0]
        def stop_in_discovery(key):
            calls[0]+=1
            if calls[0]==3:event.set()
            original(key)
        t.assert_exclusive=stop_in_discovery
        result=a.execute({'kind':'gripper','side':'left','position':20},event)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(len(t.sent),1)

    def test_same_cached_feedback_cannot_verify_gripper_arrival(self):
        a, clock, t = adapter();t.mirror=False;a.profile['gripper_timeout_s']=.3
        with self.assertRaises(ActionRejected) as caught:
            a.execute({'kind':'gripper','side':'left','position':10},Event(clock))
        self.assertTrue(caught.exception.commands_sent)
        self.assertFalse(caught.exception.hold_sent)

    def test_same_cached_feedback_cannot_verify_joint_arrival(self):
        a, clock, t = adapter();t.mirror=False;a.profile['settle_timeout_s']=.3
        with self.assertRaises(ActionRejected) as caught:
            a.execute({'kind':'joints','targets':{'arm_l_j1':0},'duration_s':.25},Event(clock))
        self.assertTrue(caught.exception.commands_sent)

    def test_publish_exception_reports_possible_delivery_and_unlocks(self):
        a, clock, t = adapter()
        def failure(key,value):raise RuntimeError('delivery unknown')
        t.publish=failure
        with self.assertRaises(RuntimeError) as caught:
            a.execute({'kind':'gripper','side':'left','position':20},Event(clock))
        self.assertTrue(caught.exception.commands_sent)
        self.assertTrue(a._action_lock.acquire(blocking=False));a._action_lock.release()

    def test_endpoint_identity_uses_gid_not_duplicate_node_name(self):
        a, _, _ = adapter()
        transport = RosTransport(a.profile, a.store)
        transport.own_gids['joint_command'] = 'aa'
        transport.refresh_graph = lambda: None
        a.store.graph = {'joint_command': {'subscribers':[{}], 'publishers':[
            {'name':'same_name', 'gid':'aa'}, {'name':'same_name','gid':'bb'}]}}
        with self.assertRaises(ActionRejected): transport.assert_exclusive('joint_command')
        a.store.graph['joint_command']['publishers'] = [{'name':'unknown','gid':None}]
        with self.assertRaises(ActionRejected): transport.assert_exclusive('joint_command')


class RuntimeContractTests(unittest.TestCase):
    def profile(self):
        from robot.runtime_contract import AUDITED_BINARY_SHA256
        return {'expected_machine_id':Path('/etc/machine-id').read_text().strip(),
                'controller_binary_sha256':next(iter(AUDITED_BINARY_SHA256)),
                'expected_motor_parameters':{'arm_control_mode':3,'arm_impedance_type':1,
                    'left_gripper_type':'omnipicker','right_gripper_type':'omnipicker',
                    'enable_udp_motors':True,'udp_motor_host':'127.0.0.1','udp_motor_send_port':50001}}

    def test_identity_mismatch_never_calls_ros(self):
        from unittest.mock import patch
        from robot.runtime_contract import verify_runtime_contract
        profile=self.profile();profile['expected_machine_id']='0'*32
        with patch('robot.runtime_contract.read_motor_parameters') as read:
            report=verify_runtime_contract(profile)
            self.assertFalse(report['ok']);self.assertFalse(read.called)

    def test_executable_mismatch_prevents_parameter_read_and_control(self):
        from unittest.mock import patch
        from robot.runtime_contract import verify_runtime_contract
        with patch('robot.runtime_contract.verify_process',side_effect=RuntimeError('hash changed')), patch('robot.runtime_contract.read_motor_parameters') as read:
            report=verify_runtime_contract(self.profile())
            self.assertFalse(report['ok']);self.assertFalse(read.called)
            self.assertFalse(report['control_publishers_created'])

    def test_joint_impedance_three_is_supported_but_cartesian_is_not(self):
        from unittest.mock import patch
        from robot.runtime_contract import verify_runtime_contract
        profile=self.profile()
        with patch('robot.runtime_contract.verify_process',return_value={'sha256':'audited'}):
            with patch('robot.runtime_contract.read_motor_parameters',return_value={'values':dict(profile['expected_motor_parameters'])}):
                self.assertTrue(verify_runtime_contract(profile)['ok'])
            changed=dict(profile['expected_motor_parameters'],arm_impedance_type=2)
            with patch('robot.runtime_contract.read_motor_parameters',return_value={'values':changed}):
                self.assertFalse(verify_runtime_contract(profile)['ok'])


if __name__ == '__main__': unittest.main()

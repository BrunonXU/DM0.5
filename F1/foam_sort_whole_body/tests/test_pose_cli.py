"""Onsite preparation failure gates and durable backups, without ROS/hardware."""
from contextlib import ExitStack, nullcontext, redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / 'robot'))
from f1_replay import pose_cli
from f1_replay.bundle import ARM_NAMES, BODY_NAMES
from f1_replay.pose_backend import PoseBackend
from f1_replay.pose_model import JOINT_NAMES, capture_pose
from f1_replay.ros_backend import COMMAND_TYPES
from f1_replay.runtime import ReplayError, run_preparation_schedule, stop_motion
from test_replay_runtime import fresh_snapshot, sample_frame


class Clock:
    def __init__(self):
        self.value = 10.0

    def monotonic(self):
        return self.value

    def monotonic_ns(self):
        return round(self.value * 1e9)

    def sleep(self, seconds):
        self.value += max(0.0, seconds)


class Terminal(io.StringIO):
    def __init__(self, text, events):
        super().__init__(text)
        self.events = events

    def isatty(self):
        return True

    def readline(self, *args):
        self.events.append('onsite_input')
        return super().readline(*args)


class Backend:
    node_name = 'pose_test'
    node_namespace = '/'

    def __init__(self, profile, frame, clock, events):
        self.profile, self.clock, self.events = profile, clock, events
        self.feedback = fresh_snapshot(frame, clock.monotonic_ns())
        self.commands_enabled = False
        self.conflict = False
        self.fail_publish = False
        self.command_count = 0
        self.validator = PoseBackend(profile)
        self.params = {'arm_control_mode': profile['arm_control_mode'],
                       'arm_impedance_type': profile['arm_impedance_type'],
                       '_controller_binary_sha256': profile['controller_binary_sha256'],
                       'left_gripper_type': 'omnipicker', 'right_gripper_type': 'omnipicker',
                       'enable_udp_motors': True, 'udp_motor_host': '127.0.0.1', 'udp_motor_send_port': 50001}
        for name, bounds in profile['joint_limits_deg'].items():
            self.params[name+'_min'], self.params[name+'_max'] = bounds

    def open(self):
        self.events.append('open_readonly')

    def close(self):
        self.events.append('closed')

    def poll(self, timeout):
        self.clock.sleep(max(timeout, .00001))
        stamp = self.clock.monotonic_ns()
        for group in ('joints', 'grippers'):
            for item in self.feedback[group].values():
                item['received_ns'] = stamp
        self.feedback['base']['received_ns'] = stamp

    def snapshot(self):
        return deepcopy(self.feedback)

    def command_peers(self):
        self.events.append('peers_checked')
        return {self.profile['topics'][key]: {'subscribers': [{'name': 'controller', 'type': kind}],
                'publishers': ([{'name': 'other_operator', 'namespace': '/', 'type': kind}] if self.conflict else [])}
                for key, kind in COMMAND_TYPES.items()}

    def read_controller_parameters(self):
        self.events.append('parameters_read')
        return {'parameters': deepcopy(self.params)}

    def check_body_parameters(self, params):
        self.validator.check_body_parameters(params)

    def validate_pose_target(self, joints, grips):
        return self.validator.validate_pose_target(joints, grips)

    def enable_commands(self):
        self.events.append('enabled')
        self.commands_enabled = True

    def publish_pose(self, joints, grips):
        self.command_count += 1
        self.events.append(('pose_command', deepcopy(joints), deepcopy(grips)))
        if self.fail_publish:
            raise ReplayError('synthetic transport failure')
        self.validate_pose_target(joints, grips)
        for name, value in joints.items():
            self.feedback['joints'][name]['position'] = value
        for name, value in grips.items():
            self.feedback['grippers'][name]['position'] = value
        return {'kind': 'pose', 'base_velocity': [0, 0, 0]}

    def stop_base(self):
        self.events.append('base_zero')
        return {'velocity': [0, 0, 0]}

    def hold_arms(self):
        self.events.append(('hold_pose', list(JOINT_NAMES)))
        return {'name': list(JOINT_NAMES)}


class PoseCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = json.loads((TASK/'configs/replay_f1.json').read_text())
        self.profile['require_body_control'] = True
        self.clock = Clock()
        self.events = []
        self.frame = sample_frame()
        self.frame.state[16:21] = [-.1, -.1, -40, -60, -20]
        self.backend = Backend(self.profile, self.frame, self.clock, self.events)
        self.profile_path = self.root/'profile.json'
        self.profile_path.write_text(json.dumps(self.profile))
        self.baseline_path = self.root/'baseline.json'
        baseline = capture_pose(self.backend.snapshot(), self.profile,
                                now_ns=self.clock.monotonic_ns(), host=self.profile['expected_host'])
        self.baseline_path.write_text(json.dumps(baseline))
        self.original_baseline = self.baseline_path.read_bytes()
        self.bundle = SimpleNamespace(path=self.root/'bundle', sha256='b'*64, frames=[self.frame])
        self.config_path = self.root/'config.json'
        self.config_path.write_text(json.dumps({
            'profile': str(self.profile_path), 'baseline_pose': str(self.baseline_path),
            'baseline_pose_sha256': pose_cli.digest(self.baseline_path), 'bundle': str(self.bundle.path),
            'motion_policy': {'arm_rate_deg_s': 3, 'body_rate_deg_s': 1, 'gripper_rate_s': 10,
                              'arm_tracking_tolerance_deg': 5, 'body_tracking_tolerance_deg': 2,
                              'gripper_tracking_tolerance': 10, 'settle_timeout_s': 1,
                              'settle_stable_s': .05}}))
        self.runs = []

    def create_run(self):
        out = self.root/f'run{len(self.runs)}'
        out.mkdir()
        self.runs.append(out)
        return out

    def run_main(self, mode, input_text='\n'):
        def schedule(bundle, send, tick, log, late, **kwargs):
            return run_preparation_schedule(bundle, send, tick, log, late, now=self.clock.monotonic_ns,
                                sleep=self.clock.sleep, **kwargs)

        def stop(backend, profile, log):
            return stop_motion(backend, profile, log, now=self.clock.monotonic_ns, sleep=self.clock.sleep)

        with ExitStack() as stack:
            for target, replacement in (
                    ('PoseBackend', lambda profile: self.backend), ('load_bundle', lambda path: self.bundle),
                    ('create_run', self.create_run), ('runtime_lock', lambda out: nullcontext()),
                    ('time', self.clock), ('run_preparation_schedule', schedule), ('stop_motion', stop)):
                stack.enter_context(patch.object(pose_cli, target, replacement))
            stack.enter_context(patch.object(pose_cli.socket, 'gethostname', return_value=self.profile['expected_host']))
            stack.enter_context(patch.object(pose_cli.sys, 'stdin', Terminal(input_text, self.events)))
            stack.enter_context(patch.object(pose_cli.select, 'select', side_effect=lambda *a: ([pose_cli.sys.stdin], [], [])))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            return pose_cli.main([mode, '--config', str(self.config_path)])

    def report(self):
        return json.loads((self.runs[-1]/'report.json').read_text())

    def assert_backup_preserved(self):
        self.assertEqual(self.baseline_path.read_bytes(), self.original_baseline)
        self.assertTrue((self.runs[-1]/'before_move_pose.json').is_file())

    def test_conflicting_controller_blocks_before_any_publish_in_both_directions(self):
        self.backend.conflict = True
        for mode in ('to-start', 'restore'):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_main(mode), 1)
                self.assertFalse(self.backend.commands_enabled)
                self.assertFalse(self.report()['motion_started'])
                self.assertEqual(self.backend.command_count, 0)
                self.assertNotIn('onsite_input', self.events)
                self.assert_backup_preserved()

    def test_terminal_close_before_enter_keeps_outputs_disabled(self):
        self.assertEqual(self.run_main('to-start', input_text=''), 1)
        self.assertIn('onsite_input', self.events)
        self.assertNotIn('enabled', self.events)
        self.assertNotIn('base_zero', self.events)
        self.assertFalse(self.report()['motion_started'])
        self.assert_backup_preserved()

    def test_to_start_can_bootstrap_without_old_robot_baseline_and_saves_current(self):
        config = json.loads(self.config_path.read_text())
        config.pop('baseline_pose')
        config.pop('baseline_pose_sha256')
        config['bundle_sha256'] = self.bundle.sha256
        self.config_path.write_text(json.dumps(config))
        self.assertEqual(self.run_main('to-start'), 0)
        self.assertIsNone(self.report()['baseline_pose'])
        self.assertTrue((self.runs[-1]/'before_move_pose.json').is_file())
        self.assertTrue(self.report()['target_reached'])

    def test_restore_without_baseline_and_wrong_bundle_fail_before_open(self):
        config = json.loads(self.config_path.read_text())
        config.pop('baseline_pose')
        config.pop('baseline_pose_sha256')
        self.config_path.write_text(json.dumps(config))
        self.assertEqual(self.run_main('restore'), 2)
        self.assertNotIn('open_readonly', self.events)
        config['bundle_sha256'] = 'a'*64
        self.config_path.write_text(json.dumps(config))
        self.assertEqual(self.run_main('to-start'), 2)
        self.assertNotIn('open_readonly', self.events)

    def test_absent_hal_feedback_never_opens_control_publishers(self):
        self.backend.feedback['joints'] = {}
        self.assertEqual(self.run_main('to-start'), 2)
        self.assertNotIn('enabled', self.events)
        self.assertNotIn('onsite_input', self.events)

    def test_moving_base_refuses_even_though_arm_pose_is_valid(self):
        self.backend.feedback['base']['vx'] = .2
        self.assertEqual(self.run_main('to-start'), 1)
        self.assertFalse(self.backend.commands_enabled)
        self.assertIn('底盘必须保持静止', ';'.join(self.report()['blockers']))
        self.assert_backup_preserved()

    def test_confirmed_motion_preserves_21_layout_and_body_stop_hold(self):
        for mode in ('to-start', 'restore'):
            with self.subTest(mode=mode):
                self.events.clear()
                self.backend.commands_enabled = False
                self.assertEqual(self.run_main(mode), 0)
                self.assertLess(self.events.index('onsite_input'), self.events.index('enabled'))
                self.assertLess(self.events.index('parameters_read'), self.events.index('enabled'))
                self.assertLess(self.events.index('peers_checked'), self.events.index('enabled'))
                commands = [e for e in self.events if isinstance(e, tuple) and e[0] == 'pose_command']
                self.assertTrue(commands)
                for _, joints, grips in commands:
                    self.assertEqual(set(joints), set(ARM_NAMES+BODY_NAMES))
                    self.assertEqual(set(grips), {'left', 'right'})
                    self.assertEqual(joints['body_j4'], -60)
                    self.assertEqual(grips['left'], self.frame.state[14])
                self.assertIn('base_zero', self.events)
                self.assertIn(('hold_pose', list(JOINT_NAMES)), self.events)
                self.assertTrue(self.report()['target_reached'])
                self.assertTrue(self.report()['stop']['zero_velocity_feedback_confirmed'])
                self.assert_backup_preserved()

    def test_publish_failure_stops_base_and_holds_fresh_body_and_arms(self):
        self.backend.fail_publish = True
        self.assertEqual(self.run_main('to-start'), 1)
        report = self.report()
        self.assertTrue(report['motion_started'])
        self.assertFalse(report['target_reached'])
        self.assertIn('synthetic transport failure', report['error'])
        self.assertIn('base_zero', self.events)
        self.assertIn(('hold_pose', list(JOINT_NAMES)), self.events)
        self.assertEqual(report['stop']['hold_scope'], 'arms14_and_body5_fresh_feedback')
        self.assert_backup_preserved()

    def test_baseline_digest_mismatch_rejected_before_backend_open(self):
        self.baseline_path.write_text(self.baseline_path.read_text()+' ')
        self.assertEqual(self.run_main('restore'), 2)
        self.assertNotIn('open_readonly', self.events)
        self.assertEqual(self.runs, [])

    def test_readonly_status_never_creates_command_publishers(self):
        self.assertEqual(self.run_main('status'), 0)
        self.assertNotIn('enabled', self.events)
        self.assertNotIn('onsite_input', self.events)
        self.assertEqual(self.backend.command_count, 0)
        self.assert_backup_preserved()

    def test_body_route_parameter_mismatch_blocks_before_confirmation(self):
        self.backend.params['enable_udp_motors'] = False
        self.assertEqual(self.run_main('to-start'), 1)
        self.assertNotIn('enabled', self.events)
        self.assertNotIn('onsite_input', self.events)
        self.assertIn('enable_udp_motors', ';'.join(self.report()['blockers']))

    def test_hal_tolerance_alone_cannot_claim_ready_when_first_action_is_farther(self):
        # Controller settles 1.5 degrees above HAL, but the first lead action is
        # 1.2 degrees below HAL. This fails the replay's shared 2 degree gate.
        self.frame.action[0] = self.frame.state[0] - 1.2
        publish = self.backend.publish_pose

        def offset_feedback(joints, grips):
            result = publish(joints, grips)
            self.backend.feedback['joints'][ARM_NAMES[0]]['position'] += 1.5
            return result

        self.backend.publish_pose = offset_feedback
        self.assertEqual(self.run_main('to-start'), 1)
        self.assertFalse(self.report()['target_reached'])
        self.assertIn('目标姿态未稳定到达', self.report()['error'])
        self.assertIn(('hold_pose', list(JOINT_NAMES)), self.events)
        self.assert_backup_preserved()


if __name__ == '__main__':
    unittest.main()

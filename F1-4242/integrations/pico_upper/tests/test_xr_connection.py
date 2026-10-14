import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from pico_f1.xr_readiness import XrReadiness

ROOT = Path(__file__).resolve().parents[1]
POSES = [[0, 0, 1, 0, 0, 0, 1] for _ in range(3)]


class ReadinessTest(unittest.TestCase):
    def test_start_before_during_or_after_connection(self):
        for delay in (0, .5, 100):
            gate = XrReadiness()
            self.assertFalse(gate.update(0, [], False, 0))
            for i in range(15):
                ready = gate.update(100 + i, POSES, True, delay + i * .02)
            self.assertTrue(ready)

    def test_static_cached_pose_never_counts_as_stream(self):
        gate = XrReadiness()
        for i in range(100):
            self.assertFalse(gate.update(123, POSES, True, i * .02))

    def test_loss_rollback_and_held_controls_require_rearm(self):
        for rollback in (False, True):
            gate = XrReadiness()
            for i in range(20):
                gate.update(100 + i, POSES, True, i * .02)
            self.assertTrue(gate.ready)
            self.assertFalse(gate.update(1 if rollback else 119, POSES, False, 1))
            for i in range(20):
                self.assertFalse(gate.update(200+i, POSES, False, 1.02+i*.02))
            for i in range(20):
                gate.update(300+i, POSES, True, 2+i*.02)
            self.assertTrue(gate.ready)

    def test_invalid_headset_blocks_even_if_both_controllers_valid(self):
        for i in range(30):
            self.assertFalse(XrReadiness().update(i+1, [*POSES[:2], [0]*7], True, i))

    def test_diagnostics_distinguish_stale_rollback_invalid_and_sdk_errors(self):
        gate=XrReadiness()
        for i in range(20):gate.update(100+i,POSES,True,i*.02)
        self.assertFalse(gate.update(119,POSES,False,.7))
        self.assertEqual(gate.last_fault,'timestamp_stalled')
        self.assertAlmostEqual(gate.last_fault_age_s,.32)
        self.assertFalse(gate.update(1,POSES,False,.71))
        self.assertEqual(gate.last_fault,'timestamp_rollback')
        self.assertFalse(gate.update(2,[*POSES[:2],[0]*7],False,.72))
        self.assertEqual(gate.last_fault,'invalid_timestamp_or_pose')

    def test_client_neutralizes_motion_but_holds_gripper_intent(self):
        fake = SimpleNamespace(init=lambda: None, close=lambda: None)
        path = ROOT/'vendor/xrobotoolkit/xrobotoolkit_teleop/common/xr_client.py'
        spec = importlib.util.spec_from_file_location('isolated_xr_client', path)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'xrobotoolkit_sdk': fake}):
            spec.loader.exec_module(mod)
            client = mod.XrClient()
            client._last_triggers['left_trigger'] = .7
            self.assertEqual(client.get_key_value_by_name('left_trigger'), .7)
            self.assertEqual(client.get_key_value_by_name('left_grip'), 0)
            self.assertFalse(client.get_button_state_by_name('A'))
            self.assertEqual(client.get_joystick_state('right'), [0, 0])
            self.assertEqual(client.get_timestamp_ns(), 0)

    def test_reconnect_rebases_mapping_without_changing_xy_home(self):
        # Load only the real method AST, avoiding ROS/hardware construction.
        import ast
        import threading
        import time
        path = ROOT/'vendor/xrobotoolkit/xrobotoolkit_teleop/common/base_teleop_controller_ik_mixin.py'
        tree = ast.parse(path.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == '_handle_xr_connection')
        scope = {'np': np, 'time': time}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
        c = SimpleNamespace(xr_client=SimpleNamespace(input_ready=lambda: True),
            _xr_rebase_pending=True, _hal_joint_states_lock=threading.Lock(),
            _trajectory_lock=threading.RLock(), hal_full_body_joint_states=np.arange(34.),
            _hal_joint_states_last_monotonic=time.monotonic(), _feedback_timeout_s=.5,
            left_q_origin=[80]*7, right_q_origin=[90]*7,
            left_q_reset=[80]*7, right_q_reset=[90]*7,
            placo_robot=SimpleNamespace(state=SimpleNamespace(q=np.arange(34.))),
            left_q_delta_init_and_solver=np.zeros(7), right_q_delta_init_and_solver=np.zeros(7),
            real_arm_command_sender=SimpleNamespace(angle_map=lambda l,r:(l,r)),
            sync_placo_to_real=lambda _:None, _finish_motion_reset=lambda measured:None)
        self.assertTrue(scope['_handle_xr_connection'](c))
        np.testing.assert_allclose(c.placo_robot.state.q[12:19]-c.origin_delta_left, np.arange(7.))
        np.testing.assert_allclose(c.placo_robot.state.q[19:26]-c.origin_delta_right, np.arange(7.,14.))
        self.assertEqual(c.left_q_origin, [80]*7)
        self.assertEqual(c.left_q_reset, [80]*7)


if __name__ == '__main__':
    unittest.main()

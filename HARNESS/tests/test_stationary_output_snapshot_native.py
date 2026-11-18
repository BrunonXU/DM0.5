"""Native state serialization checks; no owner, ROS, device or transport."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from copy import deepcopy
import numpy as np

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from stationary_output_snapshot import capture, restore, UnsupportedSnapshot
from output_branch import fork_output
from f1_harness.contracts import digest


@unittest.skipUnless(importlib.util.find_spec('ruckig') is not None, 'requires native environment')
class StationarySnapshotTests(unittest.TestCase):
    def factory(self):
        from f1dagger.robot_io import make_output
        output = make_output({'_infra_root': str(ROOT.parent / 'F1-4242')})
        output.planner.set_limits([[-170., 170.]] * 14)
        return output

    def fixture(self, enabled=(False, True)):
        output = self.factory()
        q = np.linspace(-31., 28., 14)
        output.push(q, 1., enabled=enabled)
        output.sample(1.02)
        context = dict(owner_instance_id='owner', session_id='session', task_hash='task',
            epoch=2, request_id='request', calibration_id='calibration',
            source_plan_sha256='plan', control_profile_sha256='profile',
            numerical_asset_id='assets', control_source_sha256='source')
        measured = np.r_[q + .02, 0., 100.]
        snapshot = capture(output, context=context, measured16=measured, measured_at_ns=1_019_000_000)
        return output, context, snapshot

    def pair(self, source, branch, force_fallback=False):
        target = source.position.copy(); target[7] += 4.; target[9] -= 3.
        if force_fallback:
            source.planner._trajectory_safe = branch.planner._trajectory_safe = lambda _: False
        for step in range(1, 100):
            now = 1.02 + step * .02
            for output in (source, branch):
                output.push(target, now, enabled=(False, True))
            np.testing.assert_array_equal(source.sample(now), branch.sample(now))
            for key in ('position', 'velocity', 'acceleration', 'filtered_target'):
                np.testing.assert_array_equal(getattr(source.planner._radians, key),
                                              getattr(branch.planner._radians, key))
            self.assertIsNone(source.fault); self.assertIsNone(branch.fault)

    def test_json_roundtrip_preserves_native_state_and_distinct_measurement(self):
        output, context, snapshot = self.fixture()
        before = fork_output(output)[1]
        raw = json.dumps(snapshot, allow_nan=False)
        self.assertLess(len(raw), 65536)
        branch = restore(json.loads(raw), factory=self.factory, expected_context=context)
        self.assertEqual(before, fork_output(output)[1])
        self.assertNotEqual(snapshot['payload']['feedback']['measured16'][:14], output.position.tolist())
        self.pair(output, branch)

    def test_disabled_stationary_state_future_outputs_exact(self):
        output, context, snapshot = self.fixture(enabled=(False, False))
        branch = restore(snapshot, factory=self.factory, expected_context=context)
        self.pair(output, branch)

    def test_terminal_fallback_preserved_without_native_certificate_pointer(self):
        output, context, snapshot = self.fixture()
        branch = restore(snapshot, factory=self.factory, expected_context=context)
        self.assertIsNot(output.planner._certificate[0], branch.planner._certificate[0])
        self.pair(output, branch, force_fallback=True)
        self.assertEqual(branch.planner.limit_diagnostics['mode'], 'limit_braking')

    def test_elapsed_nonzero_curve_retains_its_exact_terminal_fallback(self):
        import ruckig
        output, context, snapshot = self.fixture()
        native=output.planner._radians
        inp=ruckig.InputParameter(14)
        inp.current_position=list(native.position)
        inp.current_velocity=inp.current_acceleration=[0.]*14
        target=list(native.position);target[7]+=.01;inp.target_position=target
        inp.target_velocity=inp.target_acceleration=[0.]*14
        for name in ('max_velocity','max_acceleration','max_jerk'):
            setattr(inp,name,[getattr(native,name)]*14)
        trajectory=ruckig.Trajectory(14)
        self.assertGreaterEqual(int(ruckig.Ruckig(14).calculate(inp,trajectory)),0)
        self.assertGreater(trajectory.duration,0.)
        position=trajectory.at_time(trajectory.duration)[0]
        native.position=list(position);native.filtered_target=list(position)
        native.velocity=native.acceleration=[0.]*14
        output.position=np.rad2deg(position);output.target=output.position.copy()
        output.planner._position_degrees=output.position.tolist()
        output.planner._certificate=(trajectory,trajectory.duration+.02)
        snapshot=capture(output,context=context,**snapshot['payload']['feedback'])
        branch=restore(snapshot,factory=self.factory,expected_context=context)
        with self.assertRaisesRegex(UnsupportedSnapshot,'no_earlier_curve'):
            branch.planner._certificate[0].at_time(0.)
        self.pair(output,branch,force_fallback=True)

    def test_no_certificate_initialization_roundtrip(self):
        output = self.factory(); output.push(np.zeros(14), 1., enabled=(False, False))
        _, context, _ = self.fixture()
        snapshot = capture(output, context=context, measured16=[0.] * 16, measured_at_ns=999_000_000)
        branch = restore(snapshot, factory=self.factory, expected_context=context)
        self.assertIsNone(branch.planner._certificate)
        self.assertEqual(snapshot, capture(branch, context=context, **snapshot['payload']['feedback']))

    def test_moving_or_filtered_state_never_silently_reset(self):
        for field in ('velocity', 'acceleration', 'filtered_target'):
            output, context, snapshot = self.fixture()
            getattr(output.planner._radians, field)[7] += .001
            before = deepcopy(getattr(output.planner._radians, field))
            with self.assertRaisesRegex(UnsupportedSnapshot, 'exact_stationary'):
                capture(output, context=context, **snapshot['payload']['feedback'])
            self.assertEqual(getattr(output.planner._radians, field), before)

    def test_unelapsed_certificate_is_not_a_stationary_snapshot(self):
        output, context, snapshot = self.fixture()
        class MovingCertificate:
            duration = 1.
        output.planner._certificate = (MovingCertificate(), .5)
        with self.assertRaisesRegex(UnsupportedSnapshot, 'moving_certificate'):
            capture(output, context=context, **snapshot['payload']['feedback'])

    def test_fault_or_uncommitted_sample_is_not_serialized(self):
        for case in ('fault', 'pending', 'unsampled'):
            output, context, snapshot = self.fixture()
            if case == 'fault': output.fault = 'injected'
            if case == 'pending': output.planner.propose(output.target, dt=.02)
            if case == 'unsampled': output.push(output.target, 1.04, enabled=(False, True))
            with self.assertRaises(ValueError): capture(output, context=context, **snapshot['payload']['feedback'])

    def test_delayed_restore_does_not_refresh_watchdog_or_clock(self):
        output, context, snapshot = self.fixture()
        branch = restore(snapshot, factory=self.factory, expected_context=context)
        branch.sample(1.5)
        self.assertTrue(branch.timed_out)
        self.assertIsNotNone(branch.fault)
        self.assertIsNone(output.fault)

    def test_digest_context_and_factory_mismatch(self):
        output, context, snapshot = self.fixture()
        changed = deepcopy(snapshot); changed['payload']['feedback']['measured16'][7] += 1.
        with self.assertRaisesRegex(UnsupportedSnapshot, 'digest'):
            restore(changed, factory=self.factory, expected_context=context)
        for field in context:
            with self.subTest(field=field), self.assertRaisesRegex(UnsupportedSnapshot, 'context'):
                restore(snapshot, factory=self.factory, expected_context=dict(context, **{field: 'changed'}))
        def wrong_factory():
            value = self.factory(); value.timeout += .01; return value
        with self.assertRaisesRegex(UnsupportedSnapshot, 'profile'):
            restore(snapshot, factory=wrong_factory, expected_context=context)
        with self.assertRaisesRegex(UnsupportedSnapshot, 'not_fresh'):
            restore(snapshot, factory=lambda: output, expected_context=context)

    def test_malformed_resigned_payload_is_not_trusted(self):
        _, context, snapshot = self.fixture()
        for field in ('velocity', 'enable', 'scope', 'feedback', 'epoch'):
            changed = deepcopy(snapshot); p = changed['payload']
            if field == 'velocity': p['native']['velocity'][7] = .01
            if field == 'enable': p['enabled_axes'][7] = False
            if field == 'scope': p['execution_authorized'] = True
            if field == 'feedback': p['feedback']['measured16'].pop()
            if field == 'epoch': p['context']['epoch'] = -1
            changed['sha256'] = digest(p)
            with self.subTest(field=field), self.assertRaises(UnsupportedSnapshot):
                restore(changed, factory=self.factory, expected_context=context)

    def test_restored_native_and_feedback_lists_do_not_alias_message(self):
        _, context, snapshot = self.fixture()
        original = deepcopy(snapshot)
        branch = restore(snapshot, factory=self.factory, expected_context=context)
        branch.planner._radians.position[7] += .1
        branch.target[7] += 1.
        self.assertEqual(snapshot, original)


if __name__ == '__main__': unittest.main()

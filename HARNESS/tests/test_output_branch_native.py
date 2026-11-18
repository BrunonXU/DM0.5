"""Native Ruckig branch state fidelity; no robot, transport, sockets or ROS.

Run with the existing numerical Python via unittest; pytest is not required in
that environment. The normal non-numerical environment explicitly skips this.
"""
import importlib.util
from pathlib import Path
import unittest

import numpy as np

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('output_branch', ROOT / 'scripts/output_branch.py')
branch_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(branch_module)
HAS_RUCKIG = importlib.util.find_spec('ruckig') is not None


@unittest.skipUnless(HAS_RUCKIG, 'requires existing native numerical environment')
class OutputBranchTests(unittest.TestCase):
    def setup_output(self):
        from f1dagger.robot_io import make_output
        output = make_output({'_infra_root': str(ROOT.parent / 'F1-4242')})
        output.planner.set_limits([[-170., 170.]] * 14)
        q = np.zeros(14)
        output.push(q, 0., enabled=(False, True))
        target = q.copy(); target[7] = 25.
        for tick in range(1, 40):
            output.push(target, tick * .004, enabled=(False, True))
            output.sample(tick * .004)
        self.assertIsNone(output.fault)
        self.assertGreater(max(abs(np.array(output.planner.velocity))), .1)
        return output, target

    def advance_pair(self, source, branch, target, start=40, stop=100):
        for tick in range(start, stop):
            now = tick * .004
            source.push(target, now, enabled=(False, True))
            branch.push(target, now, enabled=(False, True))
            np.testing.assert_array_equal(source.sample(now), branch.sample(now))
            for field in ('position', 'velocity', 'acceleration', 'filtered_target'):
                np.testing.assert_array_equal(getattr(source.planner._radians, field),
                                              getattr(branch.planner._radians, field))
            self.assertIsNone(source.fault); self.assertIsNone(branch.fault)

    def test_moving_branch_preserves_native_state_and_future_output(self):
        output, target = self.setup_output()
        branch, state = branch_module.fork_output(output)
        self.assertTrue(state['certificate_present'])
        self.assertFalse(state['serializable_controller'])
        self.advance_pair(output, branch, target)

    def test_certified_braking_keeps_momentum_and_exact_future(self):
        output, target = self.setup_output()
        branch, _ = branch_module.fork_output(output)
        # Force the existing certified-fallback branch, without inventing a
        # collision or changing limits. Both outputs must keep the old curve.
        output.planner._trajectory_safe = lambda trajectory: False
        branch.planner._trajectory_safe = lambda trajectory: False
        self.advance_pair(output, branch, target, stop=41)
        self.assertEqual(branch.planner.limit_diagnostics['mode'], 'limit_braking')
        self.assertGreater(max(abs(np.array(branch.planner.velocity))), .1)
        self.advance_pair(output, branch, target, start=41, stop=800)
        np.testing.assert_array_equal(branch.planner.velocity, np.zeros(14))

    def test_branch_mutations_do_not_change_source_or_its_certificate(self):
        output, target = self.setup_output()
        branch, before = branch_module.fork_output(output)
        old_certificate = output.planner._certificate
        old_endpoint = old_certificate[0].at_time(old_certificate[0].duration)
        target[7] = -25.
        for tick in range(40, 70):
            branch.push(target, tick * .004, enabled=(False, True)); branch.sample(tick * .004)
        _, after = branch_module.fork_output(output)
        self.assertEqual(before, after)
        self.assertIs(output.planner._certificate, old_certificate)
        self.assertEqual(old_endpoint, old_certificate[0].at_time(old_certificate[0].duration))
        self.assertIsNot(output.target, branch.target)
        self.assertIsNot(output.planner.bounds, branch.planner.bounds)

    def test_certificate_view_does_not_expose_native_writable_profiles(self):
        output, _ = self.setup_output()
        branch, _ = branch_module.fork_output(output)
        view = branch.planner._certificate[0]
        self.assertFalse(hasattr(view, 'profiles'))
        with self.assertRaises(AttributeError): view.duration = 0
        samples = view.at_time(0.); samples[0][7] = 999.
        self.assertNotEqual(view.at_time(0.)[0][7], 999.)

    def test_rebranch_keeps_prior_certificate_without_mutable_alias(self):
        output, target = self.setup_output()
        branch, _ = branch_module.fork_output(output)
        second, _ = branch_module.fork_output(branch)
        self.advance_pair(output, second, target)
        self.assertNotEqual(output.sampled, branch.sampled)

    def test_uncommitted_proposal_is_rejected(self):
        output, target = self.setup_output()
        output.planner.propose(target, dt=.004)
        with self.assertRaisesRegex(ValueError, 'not_committed'):
            branch_module.fork_output(output)

    def test_unsampled_reference_is_rejected(self):
        output, target = self.setup_output()
        output.push(target, .160, enabled=(False, True))
        with self.assertRaisesRegex(ValueError, 'sample_not_completed'):
            branch_module.fork_output(output)

    def test_fault_cannot_be_cleared_by_branch(self):
        output, _ = self.setup_output()
        output.fault = 'unresolved'
        with self.assertRaisesRegex(ValueError, 'not_ready'):
            branch_module.fork_output(output)
        self.assertEqual(output.fault, 'unresolved')

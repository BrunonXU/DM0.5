from __future__ import annotations

import math
import unittest

from pico_f1.trajectory import JerkLimitedTrajectory


class JerkLimitedTrajectoryTest(unittest.TestCase):
    def make_filter(self) -> JerkLimitedTrajectory:
        try:
            trajectory = JerkLimitedTrajectory(
                size=2,
                dt=0.02,
                max_velocity=18.0,
                max_acceleration=45.0,
                max_jerk=180.0,
                target_cutoff_hz=4.0,
                deadband=0.03,
            )
        except RuntimeError as exc:
            if "ruckig is required" in str(exc):
                self.skipTest(str(exc))
            raise
        trajectory.reset([0.0, 0.0])
        return trajectory

    def test_step_respects_all_derivative_limits(self) -> None:
        trajectory = self.make_filter()
        previous_a = trajectory.acceleration.copy()
        previous_q = trajectory.position.copy()
        for _ in range(500):
            proposal = trajectory.propose([30.0, -30.0])
            trajectory.commit(proposal)
            for index in range(2):
                velocity = (trajectory.position[index] - previous_q[index]) / 0.02
                jerk = (trajectory.acceleration[index] - previous_a[index]) / 0.02
                self.assertLessEqual(abs(velocity), 18.0 + 1e-9)
                self.assertLessEqual(abs(trajectory.acceleration[index]), 45.0 + 1e-9)
                self.assertLessEqual(abs(jerk), 180.0 + 1e-9)
            previous_q = trajectory.position.copy()
            previous_a = trajectory.acceleration.copy()
        self.assertAlmostEqual(trajectory.position[0], 30.0, delta=0.05)
        self.assertAlmostEqual(trajectory.position[1], -30.0, delta=0.05)

    def test_sub_deadband_jitter_does_not_move(self) -> None:
        trajectory = self.make_filter()
        for index in range(300):
            jitter = 0.02 * math.sin(index * 0.41)
            proposal = trajectory.propose([jitter, -jitter])
            trajectory.commit(proposal)
        self.assertEqual(trajectory.position, [0.0, 0.0])
        self.assertEqual(trajectory.velocity, [0.0, 0.0])

    def test_rejected_axis_holds_with_zero_derivatives(self) -> None:
        trajectory = self.make_filter()
        proposal = trajectory.propose([10.0, 10.0])
        trajectory.commit(proposal, [True, False])
        self.assertGreater(trajectory.position[0], 0.0)
        self.assertEqual(trajectory.position[1], 0.0)
        self.assertEqual(trajectory.velocity[1], 0.0)
        self.assertEqual(trajectory.acceleration[1], 0.0)

    def test_invalid_target_fails_closed(self) -> None:
        trajectory = self.make_filter()
        with self.assertRaises(ValueError):
            trajectory.propose([float("nan"), 0.0])
        with self.assertRaises(ValueError):
            trajectory.propose([1.0])

    def test_sync_retry_preserves_velocity_and_acceleration(self) -> None:
        trajectory = self.make_filter()
        real_ruckig = trajectory._ruckig_type

        class FailOnceRuckig:
            calls = 0

            def __init__(self, size, dt):
                self._delegate = real_ruckig(size, dt)

            def update(self, inp, out):
                type(self).calls += 1
                if type(self).calls == 1:
                    raise RuntimeError("synthetic infeasible dynamic state")
                self.assert_dynamic_state_preserved(inp)
                return self._delegate.update(inp, out)

            @staticmethod
            def assert_dynamic_state_preserved(inp):
                if list(inp.current_velocity) != [2., -2.] or list(inp.current_acceleration) != [4., -4.]:
                    raise AssertionError("retry must preserve dynamic state")

        trajectory.velocity = [2.0, -2.0]
        trajectory.acceleration = [4.0, -4.0]
        trajectory._ruckig_type = FailOnceRuckig
        proposal = trajectory.propose([1.0, -1.0])
        self.assertEqual(FailOnceRuckig.calls, 2)
        self.assertLess(max(abs(proposal.velocity[i] - trajectory.velocity[i]) for i in range(2)), .9)
        self.assertLessEqual(max(abs(proposal.acceleration[i] - trajectory.acceleration[i]) for i in range(2)), 3.6 + 1e-8)

    def test_body_j3_50hz_waypoints_are_resampled_at_1khz(self) -> None:
        trajectory = JerkLimitedTrajectory(
            size=1,
            dt=0.001,
            max_velocity=6.0,
            max_acceleration=15.0,
            max_jerk=60.0,
            target_cutoff_hz=30.0,
            deadband=0.001,
        )
        trajectory.reset([-10.0])
        previous_position = -10.0
        previous_acceleration = 0.0
        max_step = 0.0
        for tick in range(5000):
            # CollisionGuard approves a new waypoint every 20 ms. The servo
            # must interpolate all intermediate 1 ms commands continuously.
            target = -10.0 - min(15.0, (tick // 20) * 0.12)
            proposal = trajectory.propose([target])
            trajectory.commit(proposal)
            max_step = max(max_step, abs(trajectory.position[0] - previous_position))
            self.assertLessEqual(abs(trajectory.velocity[0]), 6.0 + 1e-8)
            self.assertLessEqual(abs(trajectory.acceleration[0]), 15.0 + 1e-8)
            self.assertLessEqual(
                abs(trajectory.acceleration[0] - previous_acceleration) / 0.001,
                60.0 + 1e-6,
            )
            previous_position = trajectory.position[0]
            previous_acceleration = trajectory.acceleration[0]
        self.assertLessEqual(max_step, 0.006 + 1e-8)
        self.assertLess(trajectory.position[0], -20.0)


if __name__ == "__main__":
    unittest.main()

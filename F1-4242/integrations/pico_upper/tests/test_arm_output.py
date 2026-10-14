import unittest
from unittest.mock import patch
import numpy as np

from pico_f1.arm_output import ApprovedArmOutput


class ArmOutputTest(unittest.TestCase):
    def test_first_pose_and_inactive_arm_remain_exact(self):
        output = ApprovedArmOutput()
        initial = np.arange(14.)
        output.push(initial, 0.)
        np.testing.assert_array_equal(output.sample(0.), initial)
        for i in range(1, 501):
            now = i * .004
            target = initial.copy()
            target[7:] += min(10., now * 10.)
            if i % 5 == 0:
                output.push(target, now)
            q = output.sample(now)
            np.testing.assert_array_equal(q[:7], initial[:7])
        self.assertGreater(q[7], initial[7] + 9.9)

    def test_ramp_respects_dynamics_with_measured_following_delay(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        previous = np.zeros(14)
        max_step = 0.
        for i in range(1, 501):
            now = i * .004
            if i % 5 == 0:
                output.push(np.ones(14) * 20. * now, now)
            q = output.sample(now)
            max_step = max(max_step, np.max(np.abs(q - previous)))
            self.assertLessEqual(np.max(np.abs(q - previous)), 30. * .004 + 1e-10)
            self.assertLessEqual(q[0], 20. * now)
            previous = q.copy()
        self.assertLess(max_step, .121)
        # Position-target braking under the same 75/300 limits adds lag.
        # Do not retain the old unsupported <50 ms promise.
        self.assertLess(40. - q[0], 6.)  # <300 ms equivalent lag at 20 deg/s

    def test_stop_settles_without_overshoot_or_long_tail(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        for i in range(1, 501):
            now = i * .004
            output.push(np.ones(14) * min(20., 20. * now), now)
            q = output.sample(now)
            self.assertGreaterEqual(q.min(), 0.)
            self.assertLessEqual(q.max(), 20.)
            if now >= 1.65:
                self.assertLess(20. - q[0], .001)

    def test_steady_waypoints_do_not_create_periodic_speed_pulses(self):
        for period in (.02, .024, .04):
            output = ApprovedArmOutput()
            output.push(np.zeros(14), 0.)
            positions = []
            next_target, n = 0., 0
            for i in range(2500):
                now = i * .004
                if now >= next_target:
                    output.push(np.ones(14) * .5 * n, now)
                    next_target += period
                    n += 1
                old_a = np.asarray(output.planner.acceleration)
                positions.append(output.sample(now)[0])
                self.assertLessEqual(np.max(np.abs(np.asarray(output.planner.acceleration)-old_a)), 300*.004 + 1e-8)
                self.assertLessEqual(np.max(np.abs(output.planner.acceleration)), 75 + 1e-8)
                self.assertIsNone(output.fault)
            v = np.diff(positions)/.004
            self.assertLess(np.ptp(v[500:]), .25)

    def test_actual_variable_intervals_preserve_all_dynamic_limits(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        now = 0.
        for i in range(2000):
            dt = (.003, .005, .004, .007, .001)[i % 5]
            now += dt
            if i % 5 == 0:
                output.push(np.ones(14)*30*now, now)
            a = np.asarray(output.planner.acceleration)
            output.sample(now)
            self.assertIsNone(output.fault)
            self.assertLessEqual(np.max(np.abs(output.planner.velocity)), 30+1e-8)
            self.assertLessEqual(np.max(np.abs(output.planner.acceleration)), 75+1e-8)
            self.assertLessEqual(np.max(np.abs(np.asarray(output.planner.acceleration)-a))/dt, 300+1e-6)

    def test_release_clears_pending_motion_only_for_that_arm(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        for i in range(1, 251):
            output.push(np.ones(14)*i*.08, i*.004)
            output.sample(i*.004)
        held = output.position.copy()
        for i in range(251, 351):
            output.push(np.ones(14)*i*.08, i*.004, [False, True])
            q = output.sample(i*.004)
            np.testing.assert_array_equal(q[:7], held[:7])
        self.assertGreater(q[7], held[7])

    def test_timeout_scheduler_stall_and_reset_do_not_replay(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        output.push(np.ones(14), .02)
        held = output.sample(.024)
        np.testing.assert_array_equal(output.sample(.3), held)
        self.assertTrue(output.timed_out)
        output.reset()
        self.assertIsNone(output.sample(.31))
        home = np.ones(14) * -20.
        output.push(home, .32)
        np.testing.assert_array_equal(output.sample(.32), home)

    def test_jitter_and_reversal_stay_bounded(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        previous = np.zeros(14)
        for i in range(1, 1001):
            now = i * .004
            if i % 5 == 0:
                output.push(np.ones(14) * np.sin(now * 2), now)
            q = output.sample(now)
            self.assertLessEqual(np.abs(q).max(), 1.)
            self.assertLessEqual(np.abs(q-previous).max(), .12 + 1e-10)
            previous = q

    def test_large_multijoint_reversals_keep_dynamic_limits(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        phases = np.arange(14)*.31
        for i in range(1, 10001):
            now = i*.004
            if i % 5 == 0:
                output.push(60*np.sin(now*1.7+phases), now)
            old_a = np.asarray(output.planner.acceleration)
            output.sample(now)
            self.assertIsNone(output.fault)
            self.assertLessEqual(np.max(np.abs(output.planner.velocity)), 30+1e-8)
            self.assertLessEqual(np.max(np.abs(output.planner.acceleration)), 75+1e-8)
            self.assertLessEqual(np.max(np.abs(np.asarray(output.planner.acceleration)-old_a)), 300*.004+1e-8)

    def test_new_packet_cannot_restart_latched_output_fault(self):
        output = ApprovedArmOutput()
        output.push(np.zeros(14), 0.)
        held = output.sample(.2)
        for i in range(51, 101):
            output.push(np.ones(14)*20, i*.004)
            np.testing.assert_array_equal(output.sample(i*.004), held)
        self.assertIsNotNone(output.fault)

    def test_invalid_positions_rejected(self):
        for q in ([0.] * 13, [float('nan')] * 14):
            with self.assertRaises(ValueError):
                ApprovedArmOutput().push(q, 0.)

    def test_timing_fault_neutral_rearm_holds_position_and_discards_old_goal(self):
        output = ApprovedArmOutput()
        output.push(np.arange(14.), 0.)
        held = output.sample(.2)
        for i in range(1, 81):
            now=.2+i*.004
            if i % 5 == 0:
                output.push(np.ones(14)*100, now, [False, False])
            np.testing.assert_array_equal(output.sample(now), held)
            if i <= 50: self.assertIsNotNone(output.fault)
        self.assertIsNone(output.fault)
        self.assertFalse(output.timed_out)
        np.testing.assert_array_equal(output.planner.velocity,np.zeros(14))
        np.testing.assert_array_equal(output.planner.acceleration,np.zeros(14))
        output.push(held+1., .524, [False, True])
        q=output.sample(.524)
        np.testing.assert_array_equal(q[:7],held[:7])
        self.assertLess(np.max(np.abs(q-held)),.001)

    def test_active_input_or_clock_stall_interrupts_neutral_recovery(self):
        for interrupt in ('active','gap','duplicate'):
            output=ApprovedArmOutput();output.push(np.zeros(14),0.)
            held=output.sample(.2)
            for i in range(1,41):
                now=.2+i*.004
                if i%5==0: output.push(np.ones(14),now,[False,False])
                output.sample(now)
            if interrupt=='active':
                output.push(np.ones(14),.364,[False,True]);output.sample(.364)
            elif interrupt=='gap': output.sample(.43)
            else: output.push(np.ones(14),.36,[False,False])
            for i in range(1,41):
                now=.44+i*.004
                if i%5==0: output.push(np.ones(14),now,[False,False])
                np.testing.assert_array_equal(output.sample(now),held)
            self.assertIsNotNone(output.fault)

    def test_planner_failure_and_backwards_clock_require_reset(self):
        for fault in ('planner','clock'):
            output=ApprovedArmOutput();output.push(np.zeros(14),0.)
            if fault=='planner':
                with patch.object(output.planner,'propose',side_effect=ValueError('injected')):
                    output.sample(.004)
            else: output.sample(-.004)
            for i in range(1,201):
                now=i*.004
                if i%5==0:output.push(np.ones(14),now,[False,False])
                np.testing.assert_array_equal(output.sample(now),np.zeros(14))
            self.assertIsNotNone(output.fault)

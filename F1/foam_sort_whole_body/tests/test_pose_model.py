"""Pure pose preservation and preparation trajectory contract tests; no ROS."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "robot"))
from f1_replay.bundle import ARM_NAMES, BODY_NAMES
from f1_replay.pose_model import (capture_pose, compare_to_start, plan_transition,
                                  restore_target, start_target, validate_pose)
from f1_replay.runtime import ReplayError
from test_replay_runtime import fresh_snapshot, sample_frame, verified_profile


class PoseTests(unittest.TestCase):
    def setUp(self):
        self.profile = verified_profile()
        self.profile["arm_limit_policy"] = "clip"
        self.frame = sample_frame()
        self.now = 1_000_000_000
        self.raw = fresh_snapshot(self.frame, self.now)
        self.raw["joints"]["head_j1"] = {"position": 12.0, "received_ns": self.now}
        self.pose = self.capture()

    def capture(self):
        return capture_pose(self.raw, self.profile, now_ns=self.now, host="test-fixture",
                            source_bundle={"episode": "synthetic-only"},
                            utc="2026-09-16T00:00:00+00:00", extra_feedback={"waist": [1, 2]})

    def test_save_is_detached_and_preserves_optional_head_and_raw(self):
        original = deepcopy(self.pose)
        self.raw["joints"][ARM_NAMES[0]]["position"] = 99
        self.assertEqual(self.pose, original)
        self.assertEqual(self.pose["joint_positions_deg"]["head_j1"], 12)
        self.assertEqual(self.pose["extra_feedback"], {"waist": [1, 2]})
        self.assertFalse(self.pose["readback_evidence"]["base_pose_available"])

    def test_required_missing_or_old_rejected_optional_old_documented(self):
        for group, name in (("joints", ARM_NAMES[0]), ("joints", BODY_NAMES[4]), ("grippers", "right")):
            with self.subTest(group=group, name=name):
                raw = deepcopy(self.raw)
                del raw[group][name]
                with self.assertRaises(ReplayError):
                    capture_pose(raw, self.profile, now_ns=self.now, host="test")
                raw = deepcopy(self.raw)
                raw[group][name]["received_ns"] = 0
                with self.assertRaises(ReplayError):
                    capture_pose(raw, self.profile, now_ns=self.now, host="test")
        self.raw["joints"]["head_j1"]["received_ns"] = 0
        saved = self.capture()
        self.assertNotIn("head_j1", saved["joint_positions_deg"])
        self.assertEqual(saved["omitted_optional_joints"], ["head_j1"])
        self.assertIn("head_j1", saved["raw_snapshot"]["joints"])

    def test_base_missing_and_nonfinite_values_rejected(self):
        for mutate in (lambda r: r["base"].pop("received_ns"),
                       lambda r: r["joints"][ARM_NAMES[0]].update(position=float("nan")),
                       lambda r: r["grippers"]["left"].update(position=True)):
            raw = deepcopy(self.raw)
            mutate(raw)
            with self.assertRaises(ReplayError):
                capture_pose(raw, self.profile, now_ns=self.now, host="test")

    def test_saved_restore_missing_or_tampered_values_rejected(self):
        for modify in (lambda p: p["joint_positions_deg"].pop(BODY_NAMES[3]),
                       lambda p: p["joint_positions_deg"].update(arm_l_j1=9),
                       lambda p: p["grippers"].update(left=200),
                       lambda p: p.update(schema="unknown")):
            saved = deepcopy(self.pose)
            modify(saved)
            with self.assertRaises(ReplayError):
                restore_target(saved, self.profile)

    def test_compare_includes_body_and_both_start_arm_references(self):
        report = compare_to_start(self.pose, self.frame, self.profile)
        self.assertEqual(len(report["fields"]), 21)
        self.assertTrue(report["all_within_tolerance"])
        self.frame.state[19] += 3
        report = compare_to_start(self.pose, self.frame, self.profile)
        self.assertFalse(report["all_within_tolerance"])
        mismatch = [r for r in report["fields"] if not r["within_tolerance"]]
        self.assertEqual([r["name"] for r in mismatch], [BODY_NAMES[3]])

    def test_candidate_body_derivatives_and_final_auxiliary_alignment(self):
        target_pose = deepcopy(self.pose)
        target_pose['joint_positions_deg']['body_j4'] -= 10
        target_pose['raw_snapshot']['joints']['body_j4']['position'] -= 10
        target = restore_target(target_pose, self.profile)
        profile = dict(self.profile, max_command_hz=201)
        plan = plan_transition(self.pose, target, profile, rate_hz=200,
            body_rate_deg_s=3, body_acceleration_deg_s2=1, body_jerk_deg_s3=2, sample_stride=8)
        self.assertEqual((len(plan['frames'])-1) % 8, 0)
        values = [f['joint_positions_deg']['body_j4'] for f in plan['frames']]
        velocities = [(b-a)*200 for a,b in zip(values,values[1:])]
        accelerations = [(b-a)*200 for a,b in zip(velocities,velocities[1:])]
        jerks = [(b-a)*200 for a,b in zip(accelerations,accelerations[1:])]
        self.assertLessEqual(max(map(abs, velocities)), 3+1e-6)
        self.assertLessEqual(max(map(abs, accelerations)), 1+1e-6)
        self.assertLessEqual(max(map(abs, jerks)), 2+1e-6)
        self.assertEqual(values[-1], target['joint_positions_deg']['body_j4'])

    def test_impossible_hal_first_action_pair_is_not_silently_centered(self):
        self.frame.action[0] += 3.0
        with self.assertRaisesRegex(ReplayError, "不自动选择折中"):
            start_target(self.frame, self.profile)

    def test_explicit_intersection_policy_preserves_both_tolerances(self):
        state_before = list(self.frame.state)
        self.frame.action[0] += 3.0
        action_before = list(self.frame.action)
        target = start_target(self.frame, self.profile, policy="feasible_intersection_v1")
        value = target['joint_positions_deg'][ARM_NAMES[0]]
        self.assertAlmostEqual(value, (self.frame.state[0]+self.frame.action[0])/2)
        for ref in (self.frame.state[0], self.frame.action[0]):
            self.assertLessEqual(abs(value-ref), self.profile['start_arm_tolerance_deg'])
        self.assertEqual(self.frame.state, state_before)
        self.assertEqual(self.frame.action, action_before)
        self.assertEqual(len(target['start_pose_selection']['adjustments']), 1)

    def test_intersection_rejects_disjoint_tolerances(self):
        self.frame.action[0] += 2*self.profile['start_arm_tolerance_deg']+.1
        with self.assertRaisesRegex(ReplayError, '没有交集'):
            start_target(self.frame, self.profile, policy="feasible_intersection_v1")

    def test_intersection_respects_hardware_limits_with_clipped_command(self):
        self.profile['joint_limits_deg'][ARM_NAMES[0]] = [-5, 5]
        self.frame.state[0] = 6.5
        self.frame.action[0] = 1.5
        with self.assertRaisesRegex(ReplayError, '没有交集'):
            start_target(self.frame, self.profile, policy="feasible_intersection_v1")

    def test_unknown_start_policy_rejected(self):
        with self.assertRaisesRegex(ReplayError, '未知起姿'):
            start_target(self.frame, self.profile, policy="arbitrary")

    def test_restore_clips_target_and_logs_difference_preserves_backup(self):
        self.profile["joint_limits_deg"][ARM_NAMES[0]] = [-5, 5]
        before = deepcopy(self.pose)
        target = restore_target(self.pose, self.profile)
        self.assertEqual(target["joint_positions_deg"][ARM_NAMES[0]], -5)
        self.assertFalse(target["exact_requested_pose"])
        self.assertEqual(target["arm_projection"][0]["source_deg"], -7.5)
        self.assertEqual(self.pose, before)
        self.profile["arm_limit_policy"] = "reject"
        with self.assertRaises(ReplayError):
            restore_target(self.pose, self.profile)

    def test_hal_clipping_that_breaks_start_tolerance_refused(self):
        self.profile["joint_limits_deg"][ARM_NAMES[0]] = [-4, 5]
        with self.assertRaises(ReplayError):
            start_target(self.frame, self.profile)

    def test_plan_endpoints_rate_bounds_body_shared_interpolation_and_no_base(self):
        self.frame.state[0] += 15
        self.frame.action[0] += 15
        self.frame.state[16] += 4
        self.frame.state[17] += 8
        self.frame.state[14] += 10
        self.frame.action[14] += 10
        target = start_target(self.frame, self.profile)
        before = deepcopy(self.pose)
        plan = plan_transition(self.pose, target, self.profile)
        frames = plan["frames"]
        self.assertEqual(self.pose, before)
        self.assertEqual(frames[0]["joint_positions_deg"], {k: self.pose["joint_positions_deg"][k] for k in ARM_NAMES + BODY_NAMES})
        self.assertEqual(frames[-1]["joint_positions_deg"], target["joint_positions_deg"])
        self.assertEqual(frames[-1]["grippers"], target["grippers"])
        self.assertGreaterEqual(plan["duration_s"], 1.875 * 8)
        for left, right in zip(frames, frames[1:]):
            dt = (right["time_ns"] - left["time_ns"]) / 1e9
            for name in ARM_NAMES + BODY_NAMES:
                rate = abs(right["joint_positions_deg"][name] - left["joint_positions_deg"][name]) / dt
                self.assertLessEqual(rate, (3 if name in ARM_NAMES else 1) + 1e-6)
            self.assertLessEqual(abs(right["grippers"]["left"] - left["grippers"]["left"]) / dt, 10 + 1e-6)
        for item in frames:
            self.assertEqual(set(item), {"time_ns", "joint_positions_deg", "grippers"})
            self.assertNotIn("head_j1", item["joint_positions_deg"])
            alpha = (item["joint_positions_deg"][BODY_NAMES[0]] - self.pose["joint_positions_deg"][BODY_NAMES[0]]) / 4
            beta = (item["joint_positions_deg"][BODY_NAMES[1]] - self.pose["joint_positions_deg"][BODY_NAMES[1]]) / 8
            self.assertAlmostEqual(alpha, beta, places=12)
        self.assertFalse(plan["base_commands_included"])
        self.assertFalse(plan["collision_checked"])
        self.assertTrue(plan["body_adapter_required"])

    def test_plan_logs_initial_projection_and_invalid_rate_refused(self):
        self.profile["joint_limits_deg"][ARM_NAMES[0]] = [-5, 5]
        target = restore_target(self.pose, self.profile)
        plan = plan_transition(self.pose, target, self.profile)
        self.assertEqual(plan["initial_arm_projection"][0]["command_deg"], -5)
        self.assertEqual(plan["frames"][0]["joint_positions_deg"][ARM_NAMES[0]], -5)
        for kwargs in ({"rate_hz": 101}, {"arm_rate_deg_s": 0}, {"body_rate_deg_s": float("nan")}):
            with self.assertRaises(ReplayError):
                plan_transition(self.pose, target, self.profile, **kwargs)


if __name__ == "__main__":
    unittest.main()

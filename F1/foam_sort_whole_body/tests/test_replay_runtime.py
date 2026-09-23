"""Replay failure scenarios with synthetic data and a clock; no ROS or robot."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
from f1_replay.bundle import ARM_NAMES, BODY_NAMES
from f1_replay.runtime import (
    COMMAND_TYPES, POSITIVE_FIELDS, ReplayError, ReplayInterrupted, base_stopped,
    check_command_peers, check_controller_parameters, check_feedback,
    profile_issues, run_schedule, stop_motion, validate_actions,
    prepare_action, action_projection_summary,
)


class FakeClock:
    def __init__(self):
        self.ns = 10_000_000_000
        self.sleeps = []

    def now(self):
        return self.ns

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.ns += round(seconds * 1e9)

    def advance(self, seconds):
        self.ns += round(seconds * 1e9)


def verified_profile():
    """Artificial test values, explicitly not F1 hardware specifications."""
    return {
        "schema_version": 1, "status": "verified",
        "verification_reference": "synthetic test fixture only",
        "feedback_max_age_s": 0.1, "start_arm_tolerance_deg": 2.0,
        "start_gripper_tolerance": 3.0, "body_tolerance_deg": 1.0,
        "base_stopped_linear_m_s": 0.01, "base_stopped_angular_rad_s": 0.02,
        "max_command_hz": 100.0, "max_source_gap_s": 1.0,
        "max_lateness_s": 0.01, "stop_timeout_s": 0.2,
        "stop_publish_hz": 50.0, "gripper_max_effort": {"left": 2.0, "right": 4.0},
        "base_speed_limits": {"vx": 0.5, "vy": 0.5, "wz": 0.5},
        "expected_hard_auto": True, "arm_control_mode": 1,
        "arm_impedance_type": 1, "base_frame": "base_link",
        "joint_limits_deg": {name: [-180.0, 180.0] for name in ARM_NAMES},
        "topics": {
            "arms_command": "/motion_ctl/joint_ctl",
            "left_gripper_command": "/motion_ctl/gripper/left",
            "right_gripper_command": "/motion_ctl/gripper/right",
            "base_command": "/move/ManualMoveCmd",
            "joint_state": "/hal/joint_states",
            "left_gripper_state": "/motion_ctl/gripper/left/state",
            "right_gripper_state": "/motion_ctl/gripper/right/state",
            "base_state": "/move/State",
        },
    }


def sample_frame(time_ns=0):
    arms = [-7.5 + i for i in range(14)]
    return SimpleNamespace(
        time_ns=time_ns, source_time_ns=123_000_000_000 + time_ns,
        state=arms + [20.5, 99.0] + [1.0, 2.0, 3.0, 4.0, 5.0],
        action=arms + [20.5, 99.0] + [0.2, -0.3, 0.1],
    )


def sample_bundle(times=(0, 40_000_000, 150_000_000), duration=200_000_000):
    return SimpleNamespace(
        frames=[sample_frame(t) for t in times], duration_ns=duration,
        manifest={"status": "replay_ready"},
    )


def fresh_snapshot(frame, received_ns):
    return {
        "joints": {
            name: {"position": value, "received_ns": received_ns}
            for name, value in zip(ARM_NAMES + BODY_NAMES, frame.state[:14] + frame.state[16:])
        },
        "grippers": {
            side: {"position": value, "received_ns": received_ns}
            for side, value in zip(("left", "right"), frame.state[14:16])
        },
        "base": {"received_ns": received_ns, "vx": 0.0, "vy": 0.0, "wz": 0.0,
                 "is_soft_auto": False, "is_hard_auto": True,
                 "is_parking_mode": False, "is_avoid": False},
    }


class FakeBackend:
    node_name = "synthetic_replay"
    node_namespace = "/test"

    def __init__(self, clock, snapshot=None, peers=None):
        self.clock = clock
        self.feedback = snapshot or {}
        self.peers = peers or {}
        self.sent = []
        self.zero_times = []
        self.holds = []
        self.on_poll = lambda: None

    def publish(self, action):
        self.sent.append((self.clock.now(), tuple(action)))
        return {"action": list(action)}

    def command_peers(self):
        return deepcopy(self.peers)

    def snapshot(self):
        return deepcopy(self.feedback)

    def poll(self, timeout):
        self.on_poll()

    def stop_base(self):
        self.zero_times.append(self.clock.now())
        return {"velocity": [0.0, 0.0, 0.0]}

    def hold_arms(self):
        positions = [self.feedback["joints"][name]["position"] for name in ARM_NAMES]
        self.holds.append(positions)
        return {"position": positions}


class ReplayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.profile = verified_profile()
        self.bundle = sample_bundle()
        self.frame = self.bundle.frames[0]
        self.snapshot = fresh_snapshot(self.frame, self.clock.now())
        self.events = []

    def log(self, kind, fields):
        self.events.append((kind, fields))

    def schedule(self, backend, tick=lambda: None, stopped=lambda: False, **kwargs):
        run_schedule(self.bundle, backend.publish, tick, kwargs.get("log", self.log),
                     kwargs.get("max_lateness_s", self.profile["max_lateness_s"]),
                     now=self.clock.now, sleep=self.clock.sleep, stopped=stopped,
                     max_command_hz=kwargs.get("max_command_hz"))

    def feedback(self, initial=True):
        return check_feedback(self.snapshot, self.frame, self.profile,
                              self.clock.now(), initial=initial)

    def stop(self, backend):
        return stop_motion(backend, self.profile, self.log,
                           now=self.clock.now, sleep=self.clock.sleep)

    def test_source_waits_final_interval_and_all_19_values_are_preserved(self):
        backend = FakeBackend(self.clock)
        original = deepcopy(self.bundle)
        origin = self.clock.now()
        validate_actions(self.bundle, self.profile)
        self.schedule(backend, max_command_hz=self.profile["max_command_hz"])
        self.assertEqual([t - origin for t, _ in backend.sent], [0, 40_000_000, 150_000_000])
        self.assertEqual([action for _, action in backend.sent],
                         [tuple(f.action) for f in original.frames])
        self.assertEqual(self.clock.now() - origin, 200_000_000)
        self.assertTrue(self.clock.sleeps)
        commands = [fields for kind, fields in self.events if kind == "command"]
        self.assertEqual([item["source_time_ns"] for item in commands],
                         [f.source_time_ns for f in original.frames])
        self.assertEqual(self.events[-1][0], "source_timeline_complete")
        self.assertEqual(self.bundle.frames[0].action, original.frames[0].action)

    def test_late_tick_aborts_without_catching_up(self):
        backend = FakeBackend(self.clock)
        calls = 0

        def slow_tick():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.clock.advance(0.2)

        with self.assertRaisesRegex(ReplayError, "迟到"):
            self.schedule(backend, tick=slow_tick)
        self.assertEqual(len(backend.sent), 1)
        self.assertNotIn("source_timeline_complete", [kind for kind, _ in self.events])

    def test_30hz_full_episode_tolerates_small_positive_sleep_jitter(self):
        # A dispatch ceiling equal to the source rate accumulates every small
        # oversleep. Use the task's explicit software ceiling, not its sample rate.
        import json
        task_profile = json.loads((TASK / "configs/replay_f1.json").read_text())
        self.assertEqual(task_profile["max_command_hz"], 31.0)
        bundle = sample_bundle(tuple(round(i * 1e9 / 30) for i in range(840)),
                               28_000_000_000)
        backend = FakeBackend(self.clock)
        started = self.clock.now()

        def slightly_late_sleep(seconds):
            self.clock.sleep(seconds)
            self.clock.advance(0.0001)

        run_schedule(bundle, backend.publish, lambda: None, self.log,
                     task_profile["max_lateness_s"], now=self.clock.now,
                     sleep=slightly_late_sleep, max_command_hz=task_profile["max_command_hz"])
        self.assertEqual(len(backend.sent), 840)
        self.assertEqual([a for _, a in backend.sent], [tuple(f.action) for f in bundle.frames])
        commands = [fields for kind, fields in self.events if kind == "command"]
        self.assertEqual([c["scheduled_monotonic_ns"] - started for c in commands],
                         [f.time_ns for f in bundle.frames])
        self.assertLessEqual(max(c["lateness_ns"] for c in commands), 100_001)
        self.assertTrue(all(b[0] - a[0] >= 1e9 / task_profile["max_command_hz"]
                            for a, b in zip(backend.sent, backend.sent[1:])))
        self.assertEqual(self.events[-1][0], "source_timeline_complete")

    def test_expired_frame_is_rejected_even_with_large_lateness_allowance(self):
        backend = FakeBackend(self.clock)
        with self.assertRaisesRegex(ReplayError, "迟到"):
            self.schedule(backend, tick=lambda: self.clock.advance(0.05), max_lateness_s=1.0)
        self.assertEqual(backend.sent, [])

    def test_feedback_work_at_30hz_does_not_accumulate_at_every_deadline(self):
        self.bundle = sample_bundle(tuple(round(i*1e9/30) for i in range(1800)),60_000_000_000)
        backend = FakeBackend(self.clock)
        self.schedule(backend,tick=lambda:self.clock.advance(.003),
                      max_lateness_s=.01,max_command_hz=31.0)
        self.assertEqual(len(backend.sent),1800)
        commands=[d for kind,d in self.events if kind=='command']
        self.assertLessEqual(max(d['lateness_ns'] for d in commands),3_000_001)
        self.assertTrue(all(b[0]-a[0]>=1e9/31 for a,b in zip(backend.sent,backend.sent[1:])))

    def test_slight_first_frame_delay_cannot_compress_the_next_dispatch_period(self):
        backend = FakeBackend(self.clock)
        self.profile["max_command_hz"] = 25.0
        validate_actions(self.bundle, self.profile)  # Every source period is valid.
        calls = 0

        def initially_delayed_tick():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.clock.advance(0.005)

        started = self.clock.now()
        self.schedule(backend, tick=initially_delayed_tick, max_command_hz=25.0)
        self.assertEqual([time - started for time, _ in backend.sent], [5_000_000, 45_000_000, 150_000_000])
        self.assertEqual(self.clock.now() - started, 200_000_000)

    def test_rate_spacing_does_not_override_lateness_budget(self):
        backend = FakeBackend(self.clock)
        with self.assertRaisesRegex(ReplayError, "迟到"):
            self.schedule(backend, max_command_hz=10.0)
        self.assertEqual(len(backend.sent), 1)

    def test_explicit_clip_preserves_source_and_non_arm_values(self):
        self.profile["arm_limit_policy"] = "clip"
        self.frame.action[0] = -190.0
        self.frame.action[13] = 200.0
        original = list(self.frame.action)
        command, changes = prepare_action(self.frame.action, self.profile)
        self.assertEqual((command[0], command[13]), (-180.0, 180.0))
        self.assertEqual(command[14:], tuple(original[14:]))
        self.assertEqual(self.frame.action, original)
        self.assertEqual([c["joint"] for c in changes], [ARM_NAMES[0], ARM_NAMES[13]])
        validate_actions(self.bundle, self.profile)
        summary = action_projection_summary(self.bundle, self.profile)
        self.assertEqual(summary["clipped_frames"], 1)
        self.assertEqual(summary["clipped_joint_values"], 2)

    def test_clipped_dispatch_is_auditable_against_original_source(self):
        self.profile["arm_limit_policy"] = "clip"
        self.frame.action[0] = -190.0
        backend = FakeBackend(self.clock)
        run_schedule(self.bundle, backend.publish, lambda: None, self.log, 0.01,
                     now=self.clock.now, sleep=self.clock.sleep,
                     action_transform=lambda action: prepare_action(action, self.profile))
        self.assertEqual(backend.sent[0][1][0], -180.0)
        event = next(fields for kind, fields in self.events if kind == "command")
        self.assertEqual(event["source_action"][0], -190.0)
        self.assertEqual(event["command_action"][0], -180.0)
        self.assertEqual(event["action_adjustments"][0]["delta_deg"], 10.0)

    def test_first_command_tolerance_uses_clipped_target(self):
        self.profile["arm_limit_policy"] = "clip"
        self.profile["joint_limits_deg"][ARM_NAMES[8]] = [-94.0, 120.0]
        self.frame.action[8] = -96.129463
        self.frame.state[8] = -94.0
        self.snapshot["joints"][ARM_NAMES[8]]["position"] = -94.0
        self.feedback()

    def test_binary_attestation_and_gripper_type_must_match(self):
        actual = {"arm_control_mode": 1, "arm_impedance_type": 1}
        for name in ARM_NAMES:
            actual[name + "_min"], actual[name + "_max"] = [-180.0, 180.0]
        self.profile.update(joint_limit_source="verified_controller_binary",
                            controller_binary_sha256="a" * 64,
                            gripper_effort_policy="ignored_by_verified_omnipicker")
        with self.assertRaisesRegex(ReplayError, "SHA256"):
            check_controller_parameters(actual, self.profile)
        actual["_controller_binary_sha256"] = "a" * 64
        with self.assertRaisesRegex(ReplayError, "夹爪类型"):
            check_controller_parameters(actual, self.profile)
        actual.update(left_gripper_type="omnipicker", right_gripper_type="omnipicker")
        check_controller_parameters(actual, self.profile)

    def test_bundle_approval_binds_explicit_action_mapping(self):
        self.bundle.manifest["execution_contract"] = {"arm_limit_policy": "clip"}
        with self.assertRaisesRegex(ReplayError, "数据包核准"):
            validate_actions(self.bundle, self.profile)
        self.profile["arm_limit_policy"] = "clip"
        validate_actions(self.bundle, self.profile)

    def test_slow_send_or_log_stops_at_the_next_boundary_without_another_tick(self):
        for operation in ("send", "log"):
            for final_frame in (False, True):
                for elapsed in (0.04, 0.05):
                    with self.subTest(operation=operation, final_frame=final_frame, elapsed=elapsed):
                        self.clock = FakeClock()
                        self.events = []
                        self.bundle = sample_bundle((0,), 40_000_000) if final_frame else sample_bundle()
                        backend = FakeBackend(self.clock)
                        normal_send = backend.publish
                        ticks = []

                        def send(action):
                            result = normal_send(action)
                            if operation == "send":
                                self.clock.advance(elapsed)
                            return result

                        def log(kind, fields):
                            self.log(kind, fields)
                            if operation == "log":
                                self.clock.advance(elapsed)

                        backend.publish = send
                        with self.assertRaisesRegex(ReplayError, "发送或日志处理"):
                            self.schedule(backend, tick=lambda: ticks.append(self.clock.now()), log=log,
                                          max_lateness_s=1.0)
                        self.assertEqual(len(backend.sent), 1)
                        self.assertEqual(len(ticks), 1)
                        self.assertEqual([kind for kind, _ in self.events], ["command"])

    def test_stopped_callback_interrupts_during_preserved_final_hold(self):
        self.bundle = sample_bundle((0,), 100_000_000)
        backend = FakeBackend(self.clock)
        started = self.clock.now()
        with self.assertRaises(ReplayInterrupted):
            self.schedule(backend, stopped=lambda: self.clock.now() - started >= 30_000_000)
        self.assertEqual(len(backend.sent), 1)
        self.assertEqual(self.clock.now() - started, 30_000_000)
        self.assertNotIn("source_timeline_complete", [kind for kind, _ in self.events])

    def test_stop_requested_during_tick_prevents_next_publication(self):
        backend = FakeBackend(self.clock)
        state = {"stopped": False}
        with self.assertRaises(ReplayInterrupted):
            self.schedule(backend, tick=lambda: state.update(stopped=True),
                          stopped=lambda: state["stopped"])
        self.assertEqual(backend.sent, [])

    def test_candidate_and_unnormalized_out_of_range_targets_are_rejected_without_clipping(self):
        self.bundle.manifest["status"] = "candidate"
        with self.assertRaisesRegex(ReplayError, "candidate"):
            validate_actions(self.bundle, self.profile)
        self.bundle.manifest["status"] = "replay_ready"
        for index, value in ((0, 180.001), (13, -180.001), (14, -0.001),
                             (15, 100.001), (16, 0.501), (17, -0.501), (18, 0.501)):
            with self.subTest(index=index):
                previous = self.frame.action[index]
                self.frame.action[index] = value
                with self.assertRaises(ReplayError):
                    validate_actions(self.bundle, self.profile)
                self.assertEqual(self.frame.action[index], value)
                self.frame.action[index] = previous

    def test_gap_frequency_and_unknown_final_period_fail_validation(self):
        cases = (
            sample_bundle((0, 2_000_000_000), 2_020_000_000),
            sample_bundle((0, 1_000_000), 21_000_000),
            sample_bundle((0, 40_000_000), 40_000_000),
            sample_bundle((0, 40_000_000), 2_000_000_000),
        )
        for bundle in cases:
            with self.subTest(times=[f.time_ns for f in bundle.frames]), self.assertRaises(ReplayError):
                validate_actions(bundle, self.profile)

    def test_unverified_or_null_profile_values_fail_before_execution(self):
        self.assertEqual(profile_issues(self.profile), [])
        for key in (*POSITIVE_FIELDS, "expected_hard_auto", "arm_control_mode", "arm_impedance_type",
                    "joint_limits_deg", "verification_reference", "base_frame"):
            profile = deepcopy(self.profile)
            profile[key] = None
            with self.subTest(key=key), self.assertRaises(ReplayError):
                validate_actions(self.bundle, profile)
        for side in ("left", "right"):
            profile = deepcopy(self.profile)
            profile["gripper_max_effort"][side] = None
            with self.subTest(side=side), self.assertRaises(ReplayError):
                validate_actions(self.bundle, profile)
        self.profile["status"] = "pending_verification"
        with self.assertRaises(ReplayError):
            validate_actions(self.bundle, self.profile)

    def test_null_profile_containers_produce_validation_errors(self):
        for key in (None, "gripper_max_effort", "base_speed_limits", "topics"):
            profile = deepcopy(self.profile)
            if key is None:
                profile = None
            else:
                profile[key] = None
            with self.subTest(key=key), self.assertRaises(ReplayError):
                validate_actions(self.bundle, profile)

    def test_initial_arms_and_grippers_match_both_recorded_state_and_first_command(self):
        self.feedback()
        for field, index, delta in (("state", 0, 3.0), ("action", 13, 3.0),
                                    ("state", 14, 4.0), ("action", 15, 4.0)):
            values = getattr(self.frame, field)
            original = values[index]
            values[index] += delta
            with self.subTest(field=field, index=index), self.assertRaises(ReplayError):
                self.feedback()
            values[index] = original
        # Later commanded movement need not remain close to the first arm target.
        self.snapshot["joints"][ARM_NAMES[0]]["position"] += 30.0
        self.feedback(initial=False)

    def test_body_stays_close_to_demonstration_during_execution(self):
        self.snapshot["joints"][BODY_NAMES[-1]]["position"] += 1.1
        for initial in (True, False):
            with self.subTest(initial=initial), self.assertRaisesRegex(ReplayError, "身体"):
                self.feedback(initial=initial)

    def test_source_body_movement_cannot_be_ignored_by_a_19d_replay(self):
        self.bundle.frames[-1].state[20] += 1.1
        with self.assertRaisesRegex(ReplayError, "身体"):
            validate_actions(self.bundle, self.profile)

    def test_each_arm_and_body_joint_must_be_fresh_even_if_other_feedback_is_current(self):
        for name in ARM_NAMES + BODY_NAMES:
            joint = self.snapshot["joints"][name]
            for received in (self.clock.now() - 100_000_001, self.clock.now() + 1):
                joint["received_ns"] = received
                with self.subTest(name=name, received=received), self.assertRaisesRegex(ReplayError, name):
                    self.feedback(initial=False)
            joint["received_ns"] = self.clock.now()

    def test_missing_nonfinite_and_stale_gripper_feedback_are_rejected(self):
        for side in ("left", "right"):
            original = self.snapshot["grippers"][side]
            for item in ({}, {"position": float("nan"), "received_ns": self.clock.now()},
                         {"position": 0, "received_ns": self.clock.now() - 100_000_001}):
                self.snapshot["grippers"][side] = item
                with self.subTest(side=side, item=item), self.assertRaises(ReplayError):
                    self.feedback(initial=False)
            self.snapshot["grippers"][side] = original

    def test_base_control_modes_parking_avoidance_and_unknown_flags_stop_replay(self):
        for key, bad in (("is_soft_auto", True), ("is_hard_auto", False),
                         ("is_parking_mode", True), ("is_avoid", True),
                         ("is_soft_auto", None), ("is_parking_mode", None), ("is_avoid", None)):
            original = self.snapshot["base"][key]
            self.snapshot["base"][key] = bad
            with self.subTest(key=key, value=bad), self.assertRaises(ReplayError):
                self.feedback(initial=False)
            self.snapshot["base"][key] = original

    def test_base_feedback_must_be_fresh_finite_and_initially_stopped(self):
        for key, bad in (("received_ns", self.clock.now() - 100_000_001),
                         ("received_ns", self.clock.now() + 1), ("vx", float("inf")),
                         ("vy", None), ("wz", True), ("vx", 0.1)):
            original = self.snapshot["base"][key]
            self.snapshot["base"][key] = bad
            with self.subTest(key=key, value=bad), self.assertRaises(ReplayError):
                self.feedback()
            self.snapshot["base"][key] = original
        self.snapshot["base"].update(vx=0.008, vy=0.008)
        self.assertFalse(base_stopped(self.snapshot, self.profile, self.clock.now()))
        self.feedback(initial=False)  # Movement is expected after playback starts.

    def command_backend(self):
        peers = {
            self.profile["topics"][key]: {
                "publishers": [], "subscribers": [{"name": "controller", "namespace": "/",
                                                   "type": COMMAND_TYPES[key]}],
            }
            for key in ("arms_command", "left_gripper_command", "right_gripper_command", "base_command")
        }
        return FakeBackend(self.clock, peers=peers)

    def test_competing_publisher_is_refused_and_only_exact_own_identity_is_allowed(self):
        backend = self.command_backend()
        check_command_peers(backend, self.profile)
        topic = self.profile["topics"]["arms_command"]
        backend.peers[topic]["publishers"] = [{"name": backend.node_name, "namespace": backend.node_namespace}]
        with self.assertRaisesRegex(ReplayError, "其他命令发布者"):
            check_command_peers(backend, self.profile)
        check_command_peers(backend, self.profile, enabled=True)
        for publisher in ({"name": "existing_replay", "namespace": backend.node_namespace},
                          {"name": backend.node_name, "namespace": "/another"}):
            backend.peers[topic]["publishers"] = [publisher]
            with self.subTest(publisher=publisher), self.assertRaisesRegex(ReplayError, "其他命令发布者"):
                check_command_peers(backend, self.profile, enabled=True)

    def test_recorder_does_not_count_as_a_controller_subscriber(self):
        backend = self.command_backend()
        topic = self.profile["topics"]["base_command"]
        for subscribers in ([], [{"name": "rosbag2_recorder", "namespace": "/",
                                  "type": COMMAND_TYPES["base_command"]}],
                            [{"name": "controller", "namespace": "/", "type": "wrong/msg/Type"}]):
            backend.peers[topic]["subscribers"] = subscribers
            with self.subTest(subscribers=subscribers), self.assertRaisesRegex(ReplayError, "控制器订阅"):
                check_command_peers(backend, self.profile)

    def test_unavailable_publishers_are_not_reported_as_sent_during_stop(self):
        backend = FakeBackend(self.clock, snapshot=self.snapshot)
        def refresh():
            self.clock.advance(0.001)
            backend.feedback = fresh_snapshot(self.frame, self.clock.now())
        backend.on_poll = refresh
        backend.stop_base = lambda: None
        backend.hold_arms = lambda: None
        result = self.stop(backend)
        self.assertFalse(result["zero_velocity_feedback_confirmed"])
        self.assertFalse(result["fresh_arm_hold_published"])

    def test_controller_mode_and_each_joint_limit_must_match_verified_profile(self):
        actual = {"arm_control_mode": 1, "arm_impedance_type": 1}
        for name in ARM_NAMES:
            actual.update({name + "_min": -180.0, name + "_max": 180.0})
        check_controller_parameters(actual, self.profile)
        for key in actual:
            changed = deepcopy(actual)
            changed[key] += 1
            with self.subTest(key=key), self.assertRaises(ReplayError):
                check_controller_parameters(changed, self.profile)

    def test_stop_sends_zero_then_holds_measured_arms_and_waits_for_new_zero_feedback(self):
        backend = FakeBackend(self.clock, snapshot=self.snapshot)
        measured = [value + 0.25 for value in self.frame.state[:14]]
        for name, value in zip(ARM_NAMES, measured):
            backend.feedback["joints"][name]["position"] = value
        backend.feedback["base"]["vx"] = 0.2

        def update_feedback():
            if len(backend.zero_times) >= 3:
                backend.feedback["base"].update(vx=0.0, received_ns=self.clock.now())

        backend.on_poll = update_feedback
        result = self.stop(backend)
        self.assertTrue(result["zero_velocity_feedback_confirmed"])
        self.assertTrue(result["fresh_arm_hold_published"])
        self.assertEqual(backend.holds, [measured])
        self.assertEqual(len(backend.zero_times), 3)
        self.assertEqual(backend.sent, [])
        self.assertEqual(self.events[-1][0], "stop_result")

    def test_old_or_equal_timestamp_zero_sample_does_not_confirm_stopping(self):
        for offset in (-1, 0):
            self.clock = FakeClock()
            snapshot = fresh_snapshot(self.frame, self.clock.now())
            snapshot["base"]["received_ns"] += offset
            backend = FakeBackend(self.clock, snapshot=snapshot)
            with self.subTest(offset=offset):
                result = self.stop(backend)
                self.assertFalse(result["zero_velocity_feedback_confirmed"])
                self.assertTrue(result["fresh_arm_hold_published"])
                self.assertGreater(len(backend.zero_times), 1)

    def test_stop_continues_zero_commands_but_refuses_a_hold_with_one_stale_joint(self):
        self.snapshot["joints"][ARM_NAMES[9]]["received_ns"] -= 100_000_001
        backend = FakeBackend(self.clock, snapshot=self.snapshot)

        def update_base():
            backend.feedback["base"]["received_ns"] = self.clock.now()

        backend.on_poll = update_base
        result = self.stop(backend)
        self.assertTrue(result["zero_velocity_feedback_confirmed"])
        self.assertFalse(result["fresh_arm_hold_published"])
        self.assertEqual(backend.holds, [])
        self.assertGreater(len(backend.zero_times), 1)
        self.assertTrue(result["errors"])

    def test_stop_does_not_keep_a_zero_confirmation_after_new_feedback_shows_movement(self):
        self.snapshot["joints"][ARM_NAMES[0]]["received_ns"] -= 100_000_001
        backend = FakeBackend(self.clock, snapshot=self.snapshot)

        def update_feedback():
            self.clock.ns += 1
            count = len(backend.zero_times)
            backend.feedback["base"].update(
                vx=0.2 if count == 2 else 0.0, received_ns=self.clock.now(),
            )
            if count >= 2:
                for item in backend.feedback["joints"].values():
                    item["received_ns"] = self.clock.now()

        backend.on_poll = update_feedback
        result = self.stop(backend)
        self.assertTrue(result["zero_velocity_feedback_confirmed"])
        self.assertTrue(result["fresh_arm_hold_published"])
        self.assertEqual(len(backend.zero_times), 3)

    def test_stop_sends_base_zero_but_never_holds_out_of_limit_measured_arm_angles(self):
        for name in ARM_NAMES:
            for outside_limit in (-180.001, 180.001):
                with self.subTest(name=name, value=outside_limit):
                    self.clock = FakeClock()
                    snapshot = fresh_snapshot(self.frame, self.clock.now())
                    snapshot["joints"][name]["position"] = outside_limit
                    backend = FakeBackend(self.clock, snapshot=snapshot)

                    def update_feedback():
                        self.clock.ns += 1
                        backend.feedback["base"]["received_ns"] = self.clock.now()
                        for item in backend.feedback["joints"].values():
                            item["received_ns"] = self.clock.now()

                    backend.on_poll = update_feedback
                    result = self.stop(backend)
                    self.assertTrue(result["zero_velocity_feedback_confirmed"])
                    self.assertFalse(result["fresh_arm_hold_published"])
                    self.assertEqual(backend.holds, [])
                    self.assertEqual(backend.sent, [])
                    self.assertGreater(len(backend.zero_times), 1)
                    self.assertTrue(any(name in error and "超限" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()

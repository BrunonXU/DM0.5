"""CLI boundary tests with synthetic objects and mocked transport; no ROS imports."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import io
import itertools
import json
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from f1_replay import cli
from f1_replay.runtime import ReplayError, ReplayInterrupted
from test_replay_runtime import sample_bundle, verified_profile


class ReplayCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = sample_bundle()
        self.bundle.path = self.root / "synthetic_bundle"
        self.bundle.sha256 = "a" * 64
        self.profile = verified_profile()
        self.profile["expected_host"] = "ms-thor"
        self.original_import = builtins.__import__
        self.ros_imports = []

    def refuse_ros_import(self, name, *args, **kwargs):
        if name.endswith("ros_backend") or name.startswith(("rclpy", "sensor_msgs", "control_msgs", "geometry_msgs", "move.msg")):
            self.ros_imports.append(name)
            raise AssertionError(f"Unexpected ROS dependency import: {name}")
        return self.original_import(name, *args, **kwargs)

    def invoke(self, argv):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return cli.main(argv)

    def inputs(self):
        return patch.multiple(cli, read_json=MagicMock(return_value=self.profile),
                              load_bundle=MagicMock(return_value=self.bundle))

    def test_default_and_explicit_inspect_never_import_ros_create_output_or_execute(self):
        for mode in ([], ["inspect"]):
            with self.subTest(mode=mode), self.inputs(), \
                    patch("builtins.__import__", side_effect=self.refuse_ros_import), \
                    patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output:
                self.assertEqual(self.invoke([*mode, "--bundle", str(self.bundle.path)]), 0)
                self.assertIn('"no_ros_or_motion": true', self.stdout.getvalue())
                execute.assert_not_called()
                output.assert_not_called()
        self.assertEqual(self.ros_imports, [])

    def test_dry_run_writes_only_preview_without_ros_or_execute(self):
        output = self.root / "dry_run"
        output.mkdir()
        with self.inputs(), patch("builtins.__import__", side_effect=self.refuse_ros_import), \
                patch.object(cli, "execute") as execute, patch.object(cli, "new_output", return_value=output):
            self.assertEqual(self.invoke(["dry-run", "--bundle", str(self.bundle.path)]), 0)
            execute.assert_not_called()
        self.assertEqual(self.ros_imports, [])
        self.assertEqual({p.name for p in output.iterdir()}, {"commands.jsonl", "report.json"})
        self.assertIn('"hardware_published": false', (output / "commands.jsonl").read_text())

    def test_clip_preview_and_execute_share_projection_preserving_source(self):
        self.profile["arm_limit_policy"] = "clip"
        self.bundle.frames[0].action[0] = 200.0
        original = list(self.bundle.frames[0].action)
        output = self.root / "clip_preview"
        output.mkdir()
        report = cli.preview(self.bundle, self.profile, output)
        row = json.loads((output / "commands.jsonl").read_text().splitlines()[0])
        self.assertEqual(row["source_action"], original)
        self.assertEqual(row["command_action"][0], 180.0)
        self.assertEqual(row["arm_positions_deg"][0], 180.0)
        self.assertEqual(row["command_action"][14:], original[14:])
        self.assertEqual(report["clipping_summary"]["clipped_joint_values"], 1)
        _, _, _, _, scheduler = self.execute_failure("schedule")
        command, changes = scheduler.call_args.kwargs["action_transform"](self.bundle.frames[0].action)
        self.assertEqual(list(command), row["command_action"])
        self.assertEqual(changes, row["action_adjustments"])
        self.assertEqual(self.bundle.frames[0].action, original)

    def fake_transport(self):
        module = ModuleType("f1_replay.ros_backend")
        module.dependency_check = MagicMock(return_value={"ok": True, "errors": []})
        backend = MagicMock()
        backend.snapshot.return_value = {"test_only": True}
        backend.command_peers.return_value = {}
        backend.read_controller_parameters.return_value = {"parameters": {}}
        backend.last_publication = None
        module.RosBackend = MagicMock(return_value=backend)
        return module, backend

    def test_feedback_opens_read_only_backend_without_bundle_output_or_execute(self):
        module, backend = self.fake_transport()
        with self.inputs(), patch.dict(sys.modules, {"f1_replay.ros_backend": module}), \
                patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output, \
                patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 2]):
            self.assertEqual(self.invoke(["feedback", "--feedback-seconds", "1"]), 0)
            cli.load_bundle.assert_not_called()
            output.assert_not_called()
            execute.assert_not_called()
        backend.open.assert_called_once_with()
        backend.poll.assert_called_once()
        backend.close.assert_called_once_with()
        backend.enable_commands.assert_not_called()
        backend.publish.assert_not_called()
        module.dependency_check.assert_not_called()
        self.assertIn('"control_publishers_created": false', self.stdout.getvalue())

    def test_check_env_uses_dependency_result_without_node_or_execute(self):
        for ok in (True, False):
            module, backend = self.fake_transport()
            module.dependency_check.return_value = {"ok": ok, "errors": [] if ok else ["missing test dependency"]}
            with self.subTest(ok=ok), self.inputs(), patch.dict(sys.modules, {"f1_replay.ros_backend": module}), \
                    patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output:
                code = self.invoke(["check-env"])
                if ok:
                    self.assertEqual(code, 0)
                else:
                    self.assertNotEqual(code, 0)
                cli.read_json.assert_not_called()
                cli.load_bundle.assert_not_called()
                execute.assert_not_called()
                output.assert_not_called()
            module.dependency_check.assert_called_once_with()
            module.RosBackend.assert_not_called()
            backend.open.assert_not_called()

    def test_preflight_collects_live_blockers_with_null_profile_without_outputs(self):
        self.profile["status"] = "pending"
        self.profile["feedback_max_age_s"] = None
        self.profile["joint_limits_deg"] = None
        module, backend = self.fake_transport()
        backend.snapshot.return_value = {"joints": {}, "grippers": {}, "base": {}}
        backend.command_peers.return_value = {
            "/motion_ctl/joint_ctl": {"publishers": [{"name": "other_controller"}],
                                       "subscribers": [{"name": "rosbag2", "type": "sensor_msgs/msg/JointState"}]}}
        backend.read_controller_parameters.return_value = {
            "parameters": {"arm_control_mode": 3, "arm_impedance_type": 1},
            "missing_names": ["arm_l_j1_min"], "complete": False}
        original_profile = json.dumps(self.profile, sort_keys=True)
        with self.inputs(), patch.dict(sys.modules, {"f1_replay.ros_backend": module}), \
                patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output, \
                patch.object(cli, "Recorder") as recorder, \
                patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 2]):
            self.assertEqual(self.invoke(["preflight", "--bundle", str(self.bundle.path),
                                          "--feedback-seconds", "1"]), 1)
        result = json.loads(self.stdout.getvalue())
        self.assertTrue(result["bundle"]["valid"])
        self.assertFalse(result["execution_ready"])
        self.assertTrue(result["profile_open_items"])
        self.assertEqual(sum("未发现控制器订阅" in item for item in result["issues"]), 4)
        self.assertTrue(any("other_controller" in item for item in result["issues"]))
        self.assertTrue(any("底盘反馈" in item for item in result["issues"]))
        self.assertTrue(any("motor_ctl未声明必需参数：arm_l_j1_min" in item for item in result["issues"]))
        self.assertEqual(original_profile, json.dumps(self.profile, sort_keys=True))
        backend.read_controller_parameters.assert_called_once()
        backend.close.assert_called_once()
        backend.enable_commands.assert_not_called()
        backend.publish.assert_not_called()
        output.assert_not_called()
        execute.assert_not_called()
        recorder.assert_not_called()

    def test_preflight_bad_bundle_still_reports_parameter_error_and_closes(self):
        module, backend = self.fake_transport()
        failure = RuntimeError("synthetic parameter response failure")
        failure.diagnostics = {"requested_count": 30, "returned_count": 0}
        backend.read_controller_parameters.side_effect = failure
        with self.inputs(), patch.object(cli, "load_bundle", side_effect=ValueError("bad sha256")), \
                patch.dict(sys.modules, {"f1_replay.ros_backend": module}), \
                patch.object(cli.time, "monotonic", side_effect=[0, 0, 0, 2]):
            self.assertEqual(self.invoke(["preflight", "--bundle", str(self.bundle.path),
                                          "--feedback-seconds", "1"]), 1)
        result = json.loads(self.stdout.getvalue())
        self.assertFalse(result["bundle"]["valid"])
        self.assertIn("bad sha256", result["bundle"]["error"])
        self.assertEqual(result["live"]["parameter_read_diagnostics"]["returned_count"], 0)
        self.assertIn("synthetic parameter response failure", result["live"]["parameter_read_error"])
        backend.command_peers.assert_called_once()
        backend.close.assert_called_once()
        backend.enable_commands.assert_not_called()

    def test_feedback_diagnostics_uses_correct_21d_state_and_19d_action_layout(self):
        state = tuple(range(21))
        action = tuple(range(100, 119))
        bundle = SimpleNamespace(frames=[SimpleNamespace(state=state, action=action)])
        snapshot = {"joints": {n: {"position": 1000, "received_ns": 100}
                               for n in (*cli.ARM_NAMES, *cli.BODY_NAMES)},
                    "grippers": {s: {"position": 1000, "received_ns": 100} for s in ("left", "right")},
                    "base": {}}
        result = cli.feedback_diagnostics(snapshot, self.profile, bundle, 100)
        self.assertEqual(result["items"][cli.BODY_NAMES[0]]["position_minus_first_state"], 984)
        self.assertEqual(result["items"][cli.BODY_NAMES[-1]]["position_minus_first_state"], 980)
        self.assertEqual(result["items"]["left_gripper"]["position_minus_first_state"], 986)
        self.assertEqual(result["items"]["right_gripper"]["position_minus_first_action"], 885)

    def test_candidate_or_pending_execute_refuses_before_ros_and_output_directory(self):
        for gate in ("candidate", "pending_profile"):
            self.bundle.manifest["status"] = "candidate" if gate == "candidate" else "replay_ready"
            self.profile["status"] = "pending" if gate == "pending_profile" else "verified"
            with self.subTest(gate=gate), self.inputs(), \
                    patch("builtins.__import__", side_effect=self.refuse_ros_import), \
                    patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output, \
                    patch.object(cli.socket, "gethostname", return_value="ms-thor"), \
                    patch.object(cli.sys.stdin, "isatty", return_value=True):
                self.assertNotEqual(self.invoke(["execute", "--bundle", str(self.bundle.path)]), 0)
                self.assertIn("candidate" if gate == "candidate" else "status", self.stderr.getvalue())
                output.assert_not_called()
                execute.assert_not_called()
        self.assertEqual(self.ros_imports, [])

    def test_wrong_host_or_noninteractive_execute_refuses_before_ros_and_output_directory(self):
        for hostname, tty in (("training-server", True), ("ms-thor", False)):
            with self.subTest(hostname=hostname, tty=tty), self.inputs(), \
                    patch("builtins.__import__", side_effect=self.refuse_ros_import), \
                    patch.object(cli, "execute") as execute, patch.object(cli, "new_output") as output, \
                    patch.object(cli, "runtime_lock", side_effect=AssertionError("Unexpected runtime lock")), \
                    patch.object(cli.socket, "gethostname", return_value=hostname), \
                    patch.object(cli.sys.stdin, "isatty", return_value=tty):
                self.assertNotEqual(self.invoke(["execute", "--bundle", str(self.bundle.path)]), 0)
                output.assert_not_called()
                execute.assert_not_called()
        self.assertEqual(self.ros_imports, [])

    def test_f1_explicit_and_default_output_reject_symlink_escape_from_bruno(self):
        bruno, outside = self.root / "Bruno", self.root / "outside"
        outside.mkdir()
        linked = bruno / "F1/foam_sort_whole_body/runtime/replay"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(outside, target_is_directory=True)
        for output, execute in ((str(linked), False), (None, False), (None, True), (str(outside), True)):
            args = SimpleNamespace(output=output)
            with self.subTest(output=output, execute=execute), patch.object(cli, "BRUNO", bruno), \
                    patch.object(cli.socket, "gethostname", return_value="ms-thor"):
                with self.assertRaises(ReplayError):
                    cli.new_output(args, execute=execute)
        self.assertEqual(list(outside.iterdir()), [])

    def test_f1_output_works_under_bruno_and_allocates_new_runs(self):
        bruno = self.root / "Bruno"
        args = SimpleNamespace(output=str(bruno / "runtime/replay"))
        with patch.object(cli, "BRUNO", bruno), patch.object(cli.socket, "gethostname", return_value="ms-thor"):
            first = cli.new_output(args)
            second = cli.new_output(args)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())
        self.assertTrue(first.resolve().is_relative_to(bruno))

    def test_recorder_ready_checks_storage_log_and_graph_without_opening_sqlite(self):
        output = self.root / "recording"
        (output / "rosbag").mkdir(parents=True)
        topics = ["/test/command", "/test/camera", "/test/feedback"]
        recorder = cli.Recorder(output, topics)
        backend = MagicMock()
        backend.topic_peers.return_value = {
            topic: {"subscribers": [{"name": "rosbag2_recorder", "namespace": "/"}]}
            for topic in topics
        }
        self.assertFalse(recorder.ready(backend))
        database = output / "rosbag/synthetic.db3"
        database.write_bytes(b"owned by the live recorder; do not open")
        console = output / "rosbag_console.log"
        console.write_text("Recording...\n"+"\n".join(f"Subscribed to topic '{t}'" for t in topics[:2]))
        self.assertFalse(recorder.ready(backend))
        console.write_text("Recording...\n"+"\n".join(f"Subscribed to topic '{t}'" for t in topics))
        backend.topic_peers.return_value[topics[2]]["subscribers"] = [{"name": "ordinary_viewer"}]
        self.assertFalse(recorder.ready(backend))
        backend.topic_peers.return_value[topics[2]]["subscribers"] = [{"name": "rosbag2_recorder"}]
        with patch.object(sqlite3,"connect",side_effect=AssertionError("live SQLite must not be opened")):
            self.assertTrue(recorder.ready(backend))
        database.write_bytes(b"")
        self.assertFalse(recorder.ready(backend))

    def test_wait_for_enter_accepts_enter_and_cancels_on_stop_or_eof(self):
        for content, stopped, error in (("\n", [False, False], None),
                                        ("", [False], "终端"),
                                        ("\n", [True], "取消"),
                                        ("\n", [False, True, True], "取消")):
            stream = io.StringIO(content)
            with self.subTest(content=content, stopped=stopped), \
                    patch.object(cli.sys, "stdin", stream), \
                    patch.object(cli.select, "select", return_value=([stream], [], [])) as select_mock, \
                    redirect_stdout(io.StringIO()):
                stop = MagicMock(side_effect=stopped)
                if error:
                    with self.assertRaisesRegex(ReplayInterrupted, error):
                        cli.wait_for_enter(stop)
                else:
                    cli.wait_for_enter(stop)
                if stopped == [True]:
                    select_mock.assert_not_called()

    def execute_failure(self, failure, recorder_close_error=False, backend_close_error=False):
        """Exercise cleanup with actual journal/report files and fake transport/time."""
        module, backend = self.fake_transport()
        self.profile["record_topics"] = ["/test/head", "/test/left", "/test/right"]
        backend.topic_peers.return_value = {
            topic: {"publishers": [{"name": "synthetic_camera"}]}
            for topic in self.profile["record_topics"]
        }
        output = self.root / f"execute_{failure}_{recorder_close_error}_{backend_close_error}"
        output.mkdir()
        profile_path = self.root / "profile.json"
        profile_path.write_text(json.dumps(self.profile))
        recorder = MagicMock()
        events = []

        def enable():
            events.append("enable")
            if failure == "enable":
                backend.last_publication = {"partial_setup": True}
                raise RuntimeError("partial enable failure")

        def schedule(*args, **kwargs):
            events.append("schedule")
            if failure == "schedule":
                raise RuntimeError("schedule failure")

        def start_recording(*args, **kwargs):
            events.append("recording_start")
            return ["synthetic_recording_no_process"]

        def close_recorder():
            events.append("recording_close")
            if recorder_close_error:
                raise RuntimeError("recorder close failure")
            return {"started": True, "metadata_present": True}

        def close_backend():
            events.append("backend_close")
            if backend_close_error:
                raise RuntimeError("backend close failure")

        def stop(*args, **kwargs):
            events.append("stop")
            return {"zero_velocity_feedback_confirmed": True, "fresh_arm_hold_published": True}

        backend.enable_commands.side_effect = enable
        backend.close.side_effect = close_backend
        recorder.start.side_effect = start_recording
        recorder.close.side_effect = close_recorder
        old_signals = {signal.SIGINT: object(), signal.SIGTERM: object()}
        signal_calls = []

        def set_signal(number, handler):
            signal_calls.append((number, handler))
            return old_signals[number]

        with patch.dict(sys.modules, {"f1_replay.ros_backend": module}), \
                patch.object(cli, "check_execution_host"), \
                patch.object(cli, "check_feedback"), patch.object(cli, "check_command_peers", return_value={}), \
                patch.object(cli, "check_controller_parameters"), patch.object(cli, "wait_for_enter"), \
                patch.object(cli, "Recorder", return_value=recorder) as recorder_factory, \
                patch.object(cli, "run_schedule", side_effect=schedule) as scheduler, \
                patch.object(cli, "stop_motion", side_effect=stop) as stop_motion, \
                patch.object(cli.signal, "signal", side_effect=set_signal), \
                patch.object(cli.time, "monotonic", side_effect=itertools.count(0, 2)), \
                patch.object(cli.time, "monotonic_ns", return_value=10_000_000_000), \
                patch.object(cli.time, "sleep", side_effect=AssertionError("Test must not sleep")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = cli.execute(self.bundle, self.profile, output, profile_path)
        self.assertEqual(json.loads((output / "report.json").read_text()), result)
        journal = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        self.assertEqual(journal[-1]["event"], "run_end")
        self.assertEqual(signal_calls[-2:], [(signal.SIGINT, old_signals[signal.SIGINT]),
                                            (signal.SIGTERM, old_signals[signal.SIGTERM])])
        self.assertEqual(len(signal_calls), 4)
        stop_motion.assert_called_once()
        backend.close.assert_called_once_with()
        self.assertLess(events.index("stop"), events.index("backend_close"))
        return result, events, recorder, recorder_factory, scheduler

    def test_partial_enable_failure_still_stops_and_writes_report_without_starting_recording(self):
        result, events, recorder, recorder_factory, scheduler = self.execute_failure("enable")
        self.assertIn("partial enable failure", result["error"])
        self.assertFalse(result["source_timeline_complete"])
        self.assertEqual(events, ["enable", "stop", "backend_close"])
        recorder_factory.assert_not_called()
        recorder.start.assert_not_called()
        scheduler.assert_not_called()

    def test_schedule_failure_still_stops_closes_recording_and_writes_report(self):
        result, events, recorder, _, scheduler = self.execute_failure("schedule")
        self.assertIn("schedule failure", result["error"])
        self.assertFalse(result["source_timeline_complete"])
        scheduler.assert_called_once()
        recorder.close.assert_called_once_with()
        self.assertEqual(events, ["enable", "recording_start", "schedule", "stop", "recording_close", "backend_close"])

    def test_recording_and_backend_close_failures_preserve_report_and_restore_signals(self):
        for recording_error, backend_error in ((True, False), (False, True), (True, True)):
            with self.subTest(recording_error=recording_error, backend_error=backend_error):
                result, events, recorder, _, _ = self.execute_failure("schedule", recording_error, backend_error)
                self.assertEqual("recording_close_error" in result, recording_error)
                self.assertEqual("backend_close_error" in result, backend_error)
                self.assertIn("schedule failure", result["error"])
                recorder.close.assert_called_once_with()
                self.assertLess(events.index("stop"), events.index("recording_close"))


if __name__ == "__main__":
    unittest.main()

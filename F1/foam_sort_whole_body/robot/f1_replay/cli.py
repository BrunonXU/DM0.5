"""Inspect/preview by default; only `execute` may create command publishers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import select
import socket
import subprocess
import sys
import time
import uuid

from .bundle import ARM_NAMES, BODY_NAMES, decode_json, load_bundle
from .runtime import (ReplayError, ReplayInterrupted, check_command_peers, check_command_activity,
                      check_controller_parameters, check_feedback, finite_number, profile_issues,
                      run_schedule, stop_motion, validate_actions, prepare_action, action_projection_summary)
from .ros_backend import COMMAND_TYPES

TASK = Path(__file__).resolve().parents[2]
BRUNO = Path("/home/user/Bruno")


def read_json(path):
    return decode_json(Path(path).read_bytes())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selected_bundle(args):
    if args.bundle:
        return Path(args.bundle).expanduser().resolve()
    selection_path = Path(args.selection).resolve()
    selection = read_json(selection_path)
    value = selection.get("bundle")
    if not value:
        raise ReplayError("尚未选择清洗数据；之后只需替换configs/replay_selection.json中的bundle路径")
    path = Path(value).expanduser()
    return (selection_path.parent / path).resolve() if not path.is_absolute() else path.resolve()


def execution_bundle(args):
    bundle = load_bundle(selected_bundle(args))
    if getattr(args, "retiming", None):
        from .retiming import apply_retiming
        bundle = apply_retiming(bundle, args.retiming)
    return bundle


def new_output(args, execute=False):
    if args.output:
        parent = Path(args.output).expanduser().resolve()
    elif execute or socket.gethostname() == "ms-thor":
        parent = BRUNO / "F1/foam_sort_whole_body/runtime/replay"
    else:
        parent = TASK / "results/replay_preview"
    parent = parent.resolve()
    if (execute or socket.gethostname() == "ms-thor") and not parent.is_relative_to(BRUNO):
        raise ReplayError("F1运行输出必须位于/home/user/Bruno内")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    out = parent / run_id
    out.mkdir(parents=True, exist_ok=False)
    return out


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = self.path.open("x", encoding="utf-8")

    def __call__(self, event, fields):
        self.handle.write(json.dumps({"event": event, "monotonic_ns": time.monotonic_ns(),
                                      "utc": datetime.now(timezone.utc).isoformat(), **fields},
                                     ensure_ascii=False, allow_nan=False) + "\n")
        self.handle.flush()

    def close(self):
        self.handle.close()


@contextmanager
def runtime_lock(directory):
    # A custom output directory must not bypass the per-task process lock.
    parent = (BRUNO / "F1/foam_sort_whole_body/runtime").resolve()
    if not parent.is_relative_to(BRUNO):
        raise ReplayError("F1锁文件目录必须位于/home/user/Bruno内")
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / "replay.lock"
    if path.is_symlink():
        raise ReplayError("replay锁文件不能是软链接")
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReplayError("本任务已有replay进程；不删除锁、不终止其他进程") from exc
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def select_recording_topics(profile, backend):
    """Keep mandatory streams; optionally record discovered diagnostic publishers."""
    required = list(dict.fromkeys([*profile["topics"].values(), *profile["record_topics"]]))
    optional = profile.get("diagnostic_record_topics", [])
    if (not isinstance(optional, list) or any(not isinstance(t, str)
            or not t.startswith('/') or any(c.isspace() for c in t) for t in optional)):
        raise ReplayError('diagnostic_record_topics必须为绝对topic名称列表')
    optional = [t for t in dict.fromkeys(optional) if t not in required]
    peers = backend.topic_peers(optional) if optional else {}
    selected = [t for t in optional if peers.get(t, {}).get('publishers')]
    missing = [t for t in optional if t not in selected]
    return required + selected, {'selected': selected, 'not_discovered': missing,
                                  'peers': peers, 'scope': 'recording_only_no_control_changes'}


class Recorder:
    def __init__(self, output, topics):
        self.output = Path(output)
        self.topics = topics
        self.process = None
        self.console = None

    def start(self, tick, backend, timeout=10):
        self.console = (self.output / "rosbag_console.log").open("xb")
        command = ["ros2", "bag", "record", "--storage", "sqlite3", "--output",
                   str(self.output / "rosbag"), *self.topics]
        self.process = subprocess.Popen(command, stdout=self.console, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tick()
            self.check()
            if self.ready(backend):
                return command
            time.sleep(0.05)
        raise ReplayError("rosbag录制未准备好；未开始发送示教动作")

    def ready(self, backend):
        # Do not open SQLite while rosbag writes it. Even a read-only reader
        # can contend with the recorder's schema/topic creation transactions.
        databases = list((self.output / "rosbag").glob("*.db3"))
        if not databases or not any(p.stat().st_size > 0 for p in databases):
            return False
        console = self.output / "rosbag_console.log"
        if not console.is_file():
            return False
        text = console.read_text(encoding='utf-8', errors='replace')
        if 'Recording...' not in text or not all(
                f"Subscribed to topic '{topic}'" in text for topic in self.topics):
            return False
        peers = backend.topic_peers(self.topics)
        return all(any("rosbag" in peer.get("name", "")
                       for peer in peers.get(topic, {}).get("subscribers", []))
                   for topic in self.topics)

    def check(self):
        if self.process is not None and self.process.poll() is not None:
            raise ReplayError("rosbag录制已退出，查看rosbag_console.log")

    def close(self):
        result = {"started": self.process is not None}
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    os.killpg(self.process.pid, signal.SIGINT)
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        # Only this child recorder, never a robot/controller process.
                        os.killpg(self.process.pid, signal.SIGTERM)
                        try:
                            self.process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            result["still_running_pid"] = self.process.pid
                result["exit_code"] = self.process.poll()
                result["metadata_present"] = (self.output / "rosbag/metadata.yaml").exists()
        finally:
            if self.console is not None:
                self.console.close()
        return result


def summarize(bundle, profile):
    return {"bundle": str(bundle.path), "manifest_sha256": bundle.sha256,
            "status": bundle.manifest["status"], "frames": len(bundle.frames),
            "duration_s": bundle.duration_ns / 1e9,
            "first_state": list(bundle.frames[0].state),
            "first_action": list(bundle.frames[0].action),
            "last_action": list(bundle.frames[-1].action),
            "clipping_summary": action_projection_summary(bundle, profile),
            "profile_open_items": profile_issues(profile),
            "retiming": bundle.manifest.get("retiming"),
            "no_ros_or_motion": True}


def preview(bundle, profile, output):
    """Generate commands directly from the same frames that execute consumes."""
    with (output / "commands.jsonl").open("x", encoding="utf-8") as handle:
        for index, frame in enumerate(bundle.frames):
            command_action, changes = prepare_action(frame.action, profile)
            handle.write(json.dumps({"index": index, "time_ns": frame.time_ns,
                "source_time_ns": frame.source_time_ns, "source_state": list(frame.state),
                "source_action": list(frame.action), "command_action": list(command_action),
                "action_adjustments": changes,
                "arm_names": list(ARM_NAMES), "arm_positions_deg": list(command_action[:14]),
                "gripper_positions": list(command_action[14:16]),
                "gripper_max_effort": profile.get("gripper_max_effort"),
                "base_vx_vy_wz": list(command_action[16:19]),
                "ros_stamp": "generated_at_actual_publish", "hardware_published": False},
                ensure_ascii=False, allow_nan=False) + "\n")
        handle.write(json.dumps({"event": "planned_end", "time_ns": bundle.duration_ns,
                                "base_vx_vy_wz": [0, 0, 0], "hardware_published": False}) + "\n")
    report = summarize(bundle, profile)
    report.update({"commands": "commands.jsonl", "scope": "numeric_command_preview_not_training_reader_or_video_review"})
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def feedback_only(profile, seconds):
    from .ros_backend import RosBackend
    backend = RosBackend(profile)
    try:
        backend.open()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            backend.poll(min(0.1, max(0, end - time.monotonic())))
        result = {"control_publishers_created": False, "hardware_published": False,
                  "snapshot_monotonic_ns": time.monotonic_ns()}
        for key, read in (("snapshot", backend.snapshot), ("command_peers", backend.command_peers)):
            try:
                result[key] = read()
            except Exception as exc:
                result[key + "_error"] = f"{type(exc).__name__}: {exc}"
        record_topics = profile.get("record_topics")
        if isinstance(record_topics, list) and record_topics:
            try:
                result["record_topic_peers"] = backend.topic_peers(record_topics)
            except Exception as exc:
                result["record_topic_peers_error"] = f"{type(exc).__name__}: {exc}"
        try:
            result["controller_parameters"] = backend.read_controller_parameters(timeout_sec=5)
        except Exception as exc:
            result["parameter_read_error"] = f"{type(exc).__name__}: {exc}"
            if hasattr(exc, "diagnostics"):
                result["parameter_read_diagnostics"] = exc.diagnostics
        if profile.get("base_controller_expected_parameters"):
            try:
                result["base_controller_parameters"] = backend.verify_base_parameters()
            except Exception as exc:
                result["base_parameter_read_error"] = str(exc)
        if profile.get('command_ownership_policy') == 'observed_idle_v1':
            result['monitor_node_name'] = backend.node_name
            result['command_activity'] = backend.command_activity()
            try:
                check_command_peers(backend, profile)
            except Exception as exc:
                result['command_ownership_error'] = str(exc)
        return result
    finally:
        backend.close()


def feedback_diagnostics(snapshot, profile, bundle, now_ns):
    """Describe missing/stale data and raw start deltas even with a null profile."""
    items, issues = {}, []
    max_age = profile.get("feedback_max_age_s")
    age_known = finite_number(max_age) and max_age > 0
    joints = snapshot.get("joints", {})
    wanted = [(name, joints.get(name), i) for i, name in enumerate((*ARM_NAMES, *BODY_NAMES))]
    wanted += [(side + "_gripper", snapshot.get("grippers", {}).get(side), 19 + i)
               for i, side in enumerate(("left", "right"))]
    for name, value, index in wanted:
        value = value if isinstance(value, dict) else {}
        position, received = value.get("position"), value.get("received_ns")
        valid = finite_number(position) and type(received) is int and received <= now_ns
        item = {"present": valid, "position": position,
                "age_s": (now_ns - received) / 1e9 if type(received) is int else None}
        if not valid:
            issues.append(f"缺少有效反馈：{name}")
        elif age_known and item["age_s"] > max_age:
            issues.append(f"反馈过旧：{name} age={item['age_s']:.3f}s")
        if valid and bundle is not None:
            # Body lives at state[16:21]; grippers live at state/action[14:16].
            state_index = index if index < 14 else index + 2 if index < 19 else index - 5
            item["position_minus_first_state"] = position - bundle.frames[0].state[state_index]
            if state_index < 16:
                item["position_minus_first_action"] = position - bundle.frames[0].action[state_index]
        items[name] = item
    base = snapshot.get("base", {})
    received = base.get("received_ns")
    base_valid = (type(received) is int and received <= now_ns
                  and all(finite_number(base.get(key)) for key in ("vx", "vy", "wz")))
    base_age = (now_ns - received) / 1e9 if type(received) is int else None
    if not base_valid:
        issues.append("缺少有效底盘反馈")
    elif age_known and base_age > max_age:
        issues.append(f"底盘反馈过旧：age={base_age:.3f}s")
    if base_valid:
        for key in ("is_soft_auto", "is_parking_mode", "is_avoid"):
            if base.get(key) is not False:
                issues.append(f"底盘模式不满足执行条件：{key}={base.get(key)!r}")
        expected = profile.get("expected_hard_auto")
        if type(expected) is bool and base.get("is_hard_auto") is not expected:
            issues.append("底盘硬控制模式与核定配置不同")
    return {"items": items, "base_age_s": base_age, "issues": issues,
            "freshness_threshold_known": age_known,
            "start_deltas_are_measurements_not_acceptance": True}


def preflight(args, profile):
    """Aggregate read-only blockers; never enable commands or create a recorder."""
    result = {"schema": "f1_replay_preflight_v1", "profile_open_items": profile_issues(profile),
              "control_publishers_created": False, "hardware_published": False,
              "recording_started": False, "execution_ready": False,
              "host": socket.gethostname(), "issues": []}
    topics = profile.get("record_topics")
    if (not isinstance(topics, list) or len(topics) < 3
            or any(not isinstance(t, str) or not t.startswith("/") for t in topics)):
        result["issues"].append("record_topics需填写三路相机等必录topic")
    bundle = None
    try:
        bundle = execution_bundle(args)
        result["retiming"] = bundle.manifest.get("retiming")
        result["bundle"] = {"valid": True, "path": str(bundle.path), "sha256": bundle.sha256,
                            "status": bundle.manifest["status"], "frames": len(bundle.frames),
                            "duration_s": bundle.duration_ns / 1e9}
        if bundle.manifest.get("status") != "replay_ready":
            result["issues"].append("数据包仍为candidate，尚未完成实机数据批准")
        if not result["profile_open_items"]:
            try:
                validate_actions(bundle, profile)
                result["bundle"]["execution_validation"] = "passed"
            except Exception as exc:
                result["issues"].append(f"动作检查：{type(exc).__name__}: {exc}")
        else:
            result["bundle"]["execution_validation"] = "blocked_by_profile_open_items"
    except Exception as exc:
        result["bundle"] = {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
        result["issues"].append("数据包读取或结构检查失败")
    try:
        live = feedback_only(profile, args.feedback_seconds)
        result["live"] = live
        for key, value in live.items():
            if key.endswith("_error"):
                result["issues"].append(f"{key}: {value}")
        if "snapshot" in live:
            result["feedback"] = feedback_diagnostics(
                live["snapshot"], profile, bundle, live["snapshot_monotonic_ns"])
            result["issues"].extend(result["feedback"]["issues"])
            if bundle is not None and not result["profile_open_items"]:
                try:
                    check_feedback(live["snapshot"], bundle.frames[0], profile,
                                   live["snapshot_monotonic_ns"], initial=True)
                except Exception as exc:
                    result["issues"].append(f"起点检查：{type(exc).__name__}: {exc}")
        peers = live.get("command_peers", {})
        for key, message_type in COMMAND_TYPES.items():
            topic = profile.get("topics", {}).get(key)
            info = peers.get(topic, {})
            consumers = [p for p in info.get("subscribers", [])
                         if "rosbag" not in p.get("name", "") and p.get("type") == message_type
                         and p.get('name') not in (live.get('monitor_node_name'),
                                                  (live.get('monitor_node_name') or '')+'_monitor')]
            if not consumers:
                result["issues"].append(f"未发现控制器订阅：{topic or key}")
            if info.get("publishers") and profile.get('command_ownership_policy') != 'observed_idle_v1':
                result["issues"].append(f"存在其他命令发布者：{topic} {info['publishers']}")
        for topic in topics if isinstance(topics, list) else []:
            if not live.get("record_topic_peers", {}).get(topic, {}).get("publishers"):
                result["issues"].append(f"必录传感器topic没有发布者：{topic}")
        parameter_report = live.get("controller_parameters", {})
        if parameter_report.get("missing_names"):
            result["issues"].append("motor_ctl未声明必需参数：" + ", ".join(parameter_report["missing_names"]))
        parameters = parameter_report.get("parameters")
        if parameters is not None and not result["profile_open_items"]:
            try:
                check_controller_parameters(parameters, profile)
            except Exception as exc:
                result["issues"].append(f"控制参数检查：{type(exc).__name__}: {exc}")
    except Exception as exc:
        result["issues"].append(f"只读ROS检查：{type(exc).__name__}: {exc}")
    if result["host"] != profile.get("expected_host"):
        result["issues"].append("当前主机不是profile指定的F1执行主机")
    result["execution_ready"] = not result["profile_open_items"] and not result["issues"]
    result["note"] = "只读瞬时检查；不写verified、不批准candidate、不代表现场任务成功；execute会重新检查并要求现场Enter。"
    return result


def check_execution_host(profile):
    if socket.gethostname() != profile.get("expected_host"):
        raise ReplayError("真机execute仅在配置指定的F1主机现场运行")
    if not sys.stdin.isatty():
        raise ReplayError("execute需要现场交互终端和Enter确认")


def wait_for_enter(stopped):
    print("按 Enter 开始；Ctrl-C 取消：", end="", flush=True)
    while not stopped():
        readable, _, _ = select.select([sys.stdin], [], [], 0.1)
        if readable:
            if not sys.stdin.readline():
                raise ReplayInterrupted("现场终端已关闭")
            if not stopped():
                return
    raise ReplayInterrupted("操作者取消准备")


def execute(bundle, profile, output, profile_path):
    from .ros_backend import RosBackend
    check_execution_host(profile)
    validate_actions(bundle, profile)
    backend = RosBackend(profile)
    log = Journal(output / "events.jsonl")
    recorder = None
    possible_outputs = False
    stop_requested = False
    old_handlers = {}
    result = {"source_timeline_complete": False, "onsite_task_success": "not_assessed",
              "execution_timeline_kind": "retimed_replay_diagnostic" if bundle.manifest.get("retiming") else "original_source",
              "original_timing_preserved": not bool(bundle.manifest.get("retiming")),
              "distance_acceptance": "not_assessed"}
    last_peer_check = 0

    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    def tick():
        nonlocal last_peer_check
        if stop_requested:
            raise ReplayInterrupted("操作者停止回放")
        backend.poll(0.0)
        check_command_activity(backend, profile)
        check_feedback(backend.snapshot(), bundle.frames[0], profile, time.monotonic_ns())
        if recorder:
            recorder.check()
        current = time.monotonic_ns()
        if possible_outputs and current - last_peer_check >= 1_000_000_000:
            log("command_peers", {"peers": check_command_peers(backend, profile, enabled=True)})
            last_peer_check = current

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, request_stop)
        log("run_start", {"bundle": str(bundle.path), "manifest_sha256": bundle.sha256,
                          "profile_sha256": sha256(profile_path), "profile": profile,
                          "source_manifest": bundle.manifest})
        backend.open()
        deadline = time.monotonic() + 10
        last_error = None
        while time.monotonic() < deadline:
            if stop_requested:
                raise ReplayInterrupted("操作者停止准备")
            backend.poll(0.1)
            try:
                check_feedback(backend.snapshot(), bundle.frames[0], profile, time.monotonic_ns(), initial=True)
                check_command_peers(backend, profile)
                last_error = None
                break
            except ReplayError as exc:
                last_error = exc
        if last_error:
            raise last_error
        parameters = backend.read_controller_parameters(timeout_sec=5)
        check_controller_parameters(parameters["parameters"], profile)
        base_parameters = backend.verify_base_parameters() if profile.get("base_controller_expected_parameters") else None
        log("base_controller_parameters", {"result": base_parameters})
        log("preflight", {"feedback": backend.snapshot(), "controller_parameters": parameters,
                          "command_peers": backend.command_peers()})
        print(f"将回放 {len(bundle.frames)} 帧 / {bundle.duration_ns/1e9:.3f} 秒，双臂＋双夹爪＋底盘。")
        if bundle.manifest.get("retiming"):
            print("本次是同步重定时诊断回放；完成时间轴不代表距离验收或训练数据通过。")
        print("确认机器人和物料已处于示教起点，回放期间不触发遥操作/复位/升降，现场急停就绪。")
        wait_for_enter(lambda: stop_requested)
        for remaining in (3, 2, 1):
            print(f"{remaining}…", flush=True)
            until = time.monotonic() + 1
            while time.monotonic() < until:
                tick()
                check_feedback(backend.snapshot(), bundle.frames[0], profile, time.monotonic_ns(), initial=True)
                time.sleep(0.01)
        check_command_peers(backend, profile)
        record_topics, diagnostics = select_recording_topics(profile, backend)
        log('recording_diagnostics', diagnostics)
        sensor_peers = backend.topic_peers(profile["record_topics"])
        for topic in profile["record_topics"]:
            if not sensor_peers.get(topic, {}).get("publishers"):
                raise ReplayError(f"必录传感器topic没有发布者：{topic}")
        if profile.get("base_controller_expected_parameters"):
            log("base_controller_parameters", {"result": backend.verify_base_parameters()})
        possible_outputs = True
        backend.enable_commands()
        recorder = Recorder(output, record_topics)
        log("recording_start", {"command": recorder.start(tick, backend)})
        tick()
        check_feedback(backend.snapshot(), bundle.frames[0], profile, time.monotonic_ns(), initial=True)
        check_command_peers(backend, profile, enabled=True)

        def publish(action):
            if stop_requested:
                raise ReplayInterrupted("操作者停止回放")
            snapshot = backend.snapshot()
            sent = backend.publish(action)
            log("feedback", {"snapshot": snapshot,
                             "arm_target_minus_feedback_deg": [a - snapshot["joints"][n]["position"]
                                                              for n, a in zip(ARM_NAMES, action[:14])]})
            return sent

        run_schedule(bundle, publish, tick, log, profile["max_lateness_s"],
                     stopped=lambda: stop_requested, max_command_hz=profile["max_command_hz"],
                     action_transform=lambda action: prepare_action(action, profile))
        result["source_timeline_complete"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        log("run_error", {"error": result["error"],
                          "last_publication": getattr(backend, "last_publication", None)})
    finally:
        try:
            if possible_outputs:
                result["stop"] = stop_motion(backend, profile, log)
        except Exception as exc:
            result["stop_error"] = str(exc)
        try:
            if recorder:
                result["recording"] = recorder.close()
        except Exception as exc:
            result["recording_close_error"] = str(exc)
        try:
            backend.close()
        except Exception as exc:
            result["backend_close_error"] = str(exc)
        for sig, old in old_handlers.items():
            signal.signal(sig, old)
        try:
            log("run_end", result)
        finally:
            log.close()
            (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if possible_outputs and not result.get("stop", {}).get("zero_velocity_feedback_confirmed"):
        print("未确认底盘实际停车，请现场检查并按需急停。", file=sys.stderr)
    return result


def parser():
    result = argparse.ArgumentParser(description="F1示教replay：默认inspect，无ROS或动作")
    result.add_argument("mode", nargs="?", default="inspect",
                        choices=("inspect", "dry-run", "check-env", "feedback", "preflight", "execute"))
    result.add_argument("--bundle", help="便携数据包目录；省略则使用selection")
    result.add_argument("--retiming", help="显式回放诊断重定时计划；原训练源不变")
    result.add_argument("--selection", default=str(TASK / "configs/replay_selection.json"))
    result.add_argument("--profile", default=str(TASK / "configs/replay_f1.json"))
    result.add_argument("--output", help="输出父目录，每次创建独立运行目录")
    result.add_argument("--feedback-seconds", type=float, default=3.0, help="只读订阅采样时长")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.mode == "check-env":
            from .ros_backend import dependency_check
            result = dependency_check()
        else:
            profile = read_json(args.profile)
            if args.mode in ("feedback", "preflight"):
                if not 0 < args.feedback_seconds <= 60:
                    raise ReplayError("只读采样时长应在0到60秒之间")
                result = (feedback_only(profile, args.feedback_seconds) if args.mode == "feedback"
                          else preflight(args, profile))
            else:
                bundle = execution_bundle(args)
                if args.mode == "inspect":
                    result = summarize(bundle, profile)
                elif args.mode == "dry-run":
                    output = new_output(args)
                    result = preview(bundle, profile, output)
                    result["output"] = str(output)
                else:
                    # Validate before creating any ROS node or output directory.
                    validate_actions(bundle, profile)
                    check_execution_host(profile)
                    topics = profile.get("record_topics")
                    if (not isinstance(topics, list) or len(topics) < 3
                            or any(not isinstance(t, str) or not t.startswith("/") for t in topics)):
                        raise ReplayError("record_topics需填写三路相机等必录topic")
                    output = new_output(args, execute=True)
                    with runtime_lock(output):
                        result = execute(bundle, profile, output, args.profile)
                    result["output"] = str(output)
                    if (not result["source_timeline_complete"]
                            or not result.get("stop", {}).get("zero_velocity_feedback_confirmed")
                            or not result.get("stop", {}).get("fresh_arm_hold_published")
                            or not result.get("recording", {}).get("metadata_present")
                            or result.get("recording", {}).get("still_running_pid")
                            or result.get("backend_close_error")):
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.mode == "check-env" and not result.get("ok"):
            return 1
        if args.mode == "preflight" and not result["execution_ready"]:
            return 1
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"F1 replay: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

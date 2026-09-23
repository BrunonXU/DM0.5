"""G2客户端的应用装配和生命周期管理。

用途：把CLI、策略后端、相机、机器人、servo、telemetry和控制循环组装起来。
输入：解析完成的命令行参数；真实模式读取GDK，mock模式使用确定性假数据。
输出：进程退出码、日志和可选JSONL telemetry；powered模式产生本地servo流。
方法：按mode创建最小资源，安装信号/软件急停，并按有界顺序清理资源。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from argparse import Namespace
from pathlib import Path
from typing import Sequence

import numpy as np

from .cameras import G2CameraRig
from .cli import parse_args
from .cleanup import close_resources
from .contracts import ACTION_DIM
from .control_loop import ControlLoop, RuntimeOptions
from .dm05_backend import DM05HTTPBackend
from .keyboard import KeyboardCommandMonitor, RuntimeCommandState
from .joint_feedback import JointFeedbackWriter, load_validated_feedback_hz
from .mock import MockCameraRig
from .observation import ObservationSource, StateOnlyObservationSource
from .process_pipeline import PolicyWorkerConfig, ProcessInferencePipeline
from .robot_control import G2RobotController, MockRobotController
from .servo import ServoStreamer
from .telemetry import DispatchTelemetry, Telemetry
from .trajectory_smoothing import (
    SHARED_PREVIEW_ACCELERATION_PENALTY,
    SHARED_PREVIEW_DECAY,
    SHARED_PREVIEW_HORIZON_STEPS,
    SHARED_PREVIEW_JERK_PENALTY,
)

logger = logging.getLogger(__name__)


def _acquire_pid_file(path: Path) -> None:
    """Atomically prevent two G2 runtime processes from sharing the controller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except FileExistsError:
            try:
                old_pid = int(path.read_text(encoding="utf-8").strip())
                os.kill(old_pid, 0)
            except (ValueError, ProcessLookupError, FileNotFoundError):
                path.unlink(missing_ok=True)
                continue
            except PermissionError as exc:
                raise RuntimeError(
                    f"cannot inspect existing G2 runtime PID {old_pid}"
                ) from exc
            raise RuntimeError(
                f"another G2 runtime is already active with PID {old_pid}"
            )
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            return
    raise RuntimeError(f"could not acquire G2 runtime PID file {path}")


def _release_pid_file(path: Path) -> None:
    try:
        if int(path.read_text(encoding="utf-8").strip()) == os.getpid():
            path.unlink(missing_ok=True)
    except (FileNotFoundError, ValueError):
        return


def _configure_logging(level: str, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _configure_gdk_native_logging() -> Path:
    """Keep GDK/glog native files in one project directory, never beside code."""
    default_dir = Path(__file__).resolve().parents[3] / "logs" / "gdk_native"
    log_dir = Path(os.environ.get("GLOG_log_dir", default_dir))
    log_dir.mkdir(parents=True, exist_ok=True)
    os.environ["GLOG_log_dir"] = str(log_dir)
    return log_dir


def _load_mock_state(path: Path | None) -> np.ndarray:
    if path is None:
        return np.zeros(ACTION_DIM, dtype=np.float32)
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("state") if isinstance(payload, dict) else payload
    state = np.asarray(values, dtype=np.float32)
    if state.shape != (ACTION_DIM,) or not np.all(np.isfinite(state)):
        raise ValueError(f"mock state must be a finite {ACTION_DIM}-value JSON array")
    return state


def run(args: Namespace) -> int:
    _configure_logging(args.log_level, args.log_file)
    powered = args.mode not in {"shadow", "rtc-shadow"}
    options = RuntimeOptions(
        mode=args.mode,
        execute_prefix_steps=args.execute_prefix_steps,
        chunk_prefetch_lookahead=args.chunk_prefetch_lookahead,
        async_min_fresh_steps=args.async_min_fresh_steps,
        rtc_predicted_delay_steps=args.rtc_predicted_delay_steps,
        rtc_execution_horizon=args.rtc_execution_horizon,
        start_jump_limit_radians=args.max_start_jump_radians,
        hand_start_jump_limit_radians=args.max_hand_start_jump_radians,
        boundary_blend_seconds=args.boundary_blend_seconds,
        boundary_blend_profile=(
            args.boundary_blend_profile
            if args.split_control_profile
            else "linear"
        ),
        arm_interpolation_profile=args.arm_interpolation_profile,
        arm_internal_jerk_weight=args.arm_internal_jerk_weight,
        arm_micro_motion_profile=args.arm_micro_motion_profile,
        arm_micro_motion_threshold_rad=args.arm_micro_motion_threshold_rad,
        observation_retry_seconds=args.observation_retry_seconds,
        max_requests=args.max_requests,
        max_clip_delta_radians=args.max_clip_delta_radians,
        max_arm_velocity_rad_s=args.max_arm_velocity_rad_s,
        max_hand_velocity_rad_s=args.max_hand_velocity_rad_s,
        max_arm_acceleration_rad_s2=args.max_arm_acceleration_rad_s2,
        max_hand_acceleration_rad_s2=args.max_hand_acceleration_rad_s2,
        max_jerk_rad_s3=args.max_jerk_rad_s3,
        enforce_action_safety_gates=(
            args.mode == "powered-rtc"
            or not args.split_control_profile
        ),
        executed_action_dim=(
            14 if args.servo_output_mode == "arms-only" else ACTION_DIM
        ),
    )
    command_state = RuntimeCommandState()
    backend = (
        DM05HTTPBackend(
            endpoint=args.policy_endpoint,
            timeout_seconds=args.request_timeout_seconds,
        )
        if (
            args.policy_transport == "http-multipart-json"
            and not args.rtc_process_isolation
        )
        else None
    )
    telemetry = Telemetry(args.telemetry_jsonl)
    timing_warning_callback = (
        lambda event, duration, threshold: telemetry.emit(
            event, duration_seconds=duration, threshold_seconds=threshold,
            request_layout=args.servo_output_mode, action="continue",
        )
    ) if args.servo_timing_warning_only else None
    effective_hand_hz = (
        100.0 if args.servo_output_mode in {"dual-side-17d", "unified-34"} else args.hand_stream_hz
    )
    dispatch_telemetry = (
        DispatchTelemetry(telemetry, hand_nominal_hz=effective_hand_hz)
        if powered and not args.mock
        else None
    )
    cameras = None
    robot = None
    servo = None
    keyboard = None
    inference_pipeline = None
    joint_feedback_writer = None
    joint_feedback_hz = 0.0
    gdk_module = None
    gdk_initialized = False
    previous_handlers: dict[int, object] = {}
    pid_acquired = False

    def stop(reason: str) -> None:
        if not command_state.stop_event.is_set():
            logger.critical("G2 graceful software stop requested: %s", reason)
        command_state.stop()

    try:
        _acquire_pid_file(args.pid_file)
        pid_acquired = True
        if args.mock:
            robot = MockRobotController(
                _load_mock_state(args.mock_state_json),
                powered=powered,
            )
            if not args.rtc_process_isolation:
                cameras = MockCameraRig()
        else:
            native_log_dir = _configure_gdk_native_logging()
            logger.info("GDK native log directory: %s", native_log_dir)
            import agibot_gdk as gdk_module

            init_result = gdk_module.gdk_init()
            if init_result != gdk_module.GDKRes.kSuccess:
                raise RuntimeError(f"gdk_init failed: {init_result}")
            gdk_initialized = True
            joint_feedback_hz = (
                0.0
                if args.disable_joint_feedback or not powered
                else load_validated_feedback_hz(args.joint_feedback_probe_json)
            )
            if powered and joint_feedback_hz and args.telemetry_jsonl is not None:
                feedback_path = (
                    args.telemetry_jsonl.parent.parent
                    / "feedback"
                    / args.telemetry_jsonl.name
                )
                joint_feedback_writer = JointFeedbackWriter(
                    feedback_path,
                    run_id=args.telemetry_jsonl.stem,
                )
            if not args.rtc_process_isolation:
                cameras = G2CameraRig(
                    timeout_ms=args.camera_timeout_ms,
                    stabilization_seconds=args.gdk_stabilization_seconds,
                )
            robot = G2RobotController(
                powered=powered,
                control_period=args.servo_control_period,
                servo_channel=args.servo_channel,
                servo_output_mode=args.servo_output_mode,
                stabilization_seconds=args.gdk_stabilization_seconds,
                max_send_seconds=args.max_servo_send_seconds,
                timing_warning_callback=timing_warning_callback,
                enforce_low_latency_step_limit=(
                    not args.split_control_profile
                ),
                last_hand_command_path=args.last_hand_command_json,
                hand_command_run_id=(
                    f"{time.strftime('%Y%m%d_%H%M%S')}-{os.getpid()}"
                ),
                hand_startup_delay_seconds=args.hand_startup_delay_seconds,
                hand_stream_hz=args.hand_stream_hz,
                hand_smoothing_profile=args.hand_smoothing_profile,
                hand_boundary_blend_seconds=(
                    args.hand_boundary_blend_seconds
                ),
                hand_max_velocity_rad_s=args.hand_max_velocity_rad_s,
                hand_max_acceleration_rad_s2=(
                    args.hand_max_acceleration_rad_s2
                ),
                hand_max_delta_radians=args.hand_max_delta_radians,
                hand_boundary_max_delta_radians=(
                    args.hand_boundary_max_delta_radians
                ),
                hand_reversal_error_radians=(
                    args.hand_reversal_error_radians
                ),
                dispatch_telemetry=dispatch_telemetry,
                joint_feedback_writer=joint_feedback_writer,
                joint_feedback_hz=joint_feedback_hz,
            )

        if args.rtc_process_isolation:
            assert robot is not None
            inference_pipeline = ProcessInferencePipeline(
                PolicyWorkerConfig(
                    endpoint=args.policy_endpoint,
                    request_timeout_seconds=args.request_timeout_seconds,
                    camera_timeout_ms=args.camera_timeout_ms,
                    gdk_stabilization_seconds=(
                        0.0 if args.mock else args.gdk_stabilization_seconds
                    ),
                    transport=args.policy_transport,
                    mock=args.mock,
                    native_log_dir=(
                        None if args.mock else str(native_log_dir)
                    ),
                    log_level=args.log_level,
                ),
                state_source=robot,
                cancel_event=command_state.stop_event,
            )
            observations = StateOnlyObservationSource(robot)
        else:
            assert cameras is not None
            observations = ObservationSource(cameras, robot)
        if powered:
            servo = ServoStreamer(
                robot.send_action,
                None,
                max_tick_lateness_seconds=(
                    args.max_servo_tick_lateness_seconds
                ),
                timing_warning_callback=timing_warning_callback,
                smoothstep_blend=args.split_control_profile,
                boundary_blend_profile=(
                    args.boundary_blend_profile
                    if args.split_control_profile
                    else "linear"
                ),
                arm_interpolation_profile=args.arm_interpolation_profile,
                arm_micro_motion_profile=args.arm_micro_motion_profile,
                arm_micro_motion_threshold_rad=(
                    args.arm_micro_motion_threshold_rad
                ),
            )

        loop = ControlLoop(
            options=options,
            observations=observations,
            backend=backend,  # type: ignore[arg-type]
            inference_pipeline=inference_pipeline,
            telemetry=telemetry,
            dispatch_telemetry=dispatch_telemetry,
            robot=robot if powered else None,
            servo=servo,
            stop_event=command_state.stop_event,
            pause_event=command_state.pause_event,
        )

        def signal_handler(signum: int, _frame: object) -> None:
            stop(f"signal {signum}")

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, signal_handler)
        def keyboard_command(command: str) -> None:
            if command == "p":
                command_state.pause()
                if servo is not None:
                    servo.hold()
                telemetry.emit(
                    "operator_pause_requested",
                    reason="keyboard p",
                )
            elif command == "r":
                command_state.resume()
                telemetry.emit(
                    "operator_resume_requested",
                    reason="keyboard r; old chunk remains discarded",
                )
            else:
                stop("keyboard q")

        keyboard_enabled = (
            not args.no_interactive_keys
            and (powered or args.interactive_keys)
        )
        if keyboard_enabled:
            keyboard = KeyboardCommandMonitor(keyboard_command)
            keyboard.start()
        telemetry.emit(
            "runtime_start",
            mode=args.mode,
            control_pid=os.getpid(),
            servo_channel=args.servo_channel,
            request_layout=args.servo_output_mode,
            endpoint=args.policy_endpoint,
            policy_transport=args.policy_transport,
            boundary_blend_seconds=args.boundary_blend_seconds,
            rtc_replan_boundary_blend_seconds=(
                0.0
                if (
                    args.mode == "powered-rtc"
                    and args.arm_interpolation_profile != "linear"
                )
                else args.boundary_blend_seconds
            ),
            boundary_blend_profile=(
                args.boundary_blend_profile
                if args.split_control_profile
                else "linear"
            ),
            arm_internal_jerk_weight=args.arm_internal_jerk_weight,
            arm_interpolation_profile=args.arm_interpolation_profile,
            arm_micro_motion_profile=args.arm_micro_motion_profile,
            arm_micro_motion_threshold_rad=(
                args.arm_micro_motion_threshold_rad
            ),
            rtc_predicted_delay_steps=args.rtc_predicted_delay_steps,
            rtc_execution_horizon=args.rtc_execution_horizon,
            rtc_process_isolation=args.rtc_process_isolation,
            robot_owner=(
                "control_process" if args.rtc_process_isolation else "single_process"
            ),
            policy_robot_reader=False if args.rtc_process_isolation else None,
            state_transport=(
                "timestamped_34d_shared_ring"
                if args.rtc_process_isolation
                else None
            ),
            policy_worker_pid=(
                None
                if inference_pipeline is None
                else inference_pipeline.worker_pid
            ),
            servo_timing_warning_only=args.servo_timing_warning_only,
            max_requests=args.max_requests,
            hand_stream_hz=effective_hand_hz,
            joint_feedback_hz=joint_feedback_hz,
            hand_smoothing_profile=args.hand_smoothing_profile,
            hand_derivative_constraints_enforced=(
                False
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_position_constraints_enforced=(
                True
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_preview_horizon_steps=(
                SHARED_PREVIEW_HORIZON_STEPS
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_acceleration_penalty=(
                SHARED_PREVIEW_ACCELERATION_PENALTY
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_jerk_penalty=(
                SHARED_PREVIEW_JERK_PENALTY
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_preview_decay=(
                SHARED_PREVIEW_DECAY
                if args.hand_smoothing_profile == "shared_preview_v1"
                else None
            ),
            hand_boundary_blend_seconds=args.hand_boundary_blend_seconds,
            hand_max_velocity_rad_s=args.hand_max_velocity_rad_s,
            hand_max_acceleration_rad_s2=(
                args.hand_max_acceleration_rad_s2
            ),
            hand_max_delta_radians=args.hand_max_delta_radians,
            hand_boundary_max_delta_radians=(
                args.hand_boundary_max_delta_radians
            ),
            hand_reversal_error_radians=args.hand_reversal_error_radians,
            logical_target_dimension=34,
            split_arm_servo_dimension=(
                14 if args.servo_output_mode == "split" else None
            ),
            split_hand_command_dimension=(
                20 if args.servo_output_mode == "split" else None
            ),
            per_side_servo_dimension=(
                17 if args.servo_output_mode == "dual-side-17d" else None
            ),
            per_tick_servo_request_count=(
                2 if args.servo_output_mode == "dual-side-17d" else 1
            ),
            servo_control_period_seconds=args.servo_control_period,
            summary=(
                f"模式={args.mode} Servo通道={args.servo_channel} "
                f"输出={args.servo_output_mode} "
                f"边界融合={args.boundary_blend_seconds:.3f}s "
                f"双臂内部jerk权重={args.arm_internal_jerk_weight:g} "
                f"双臂插值={args.arm_interpolation_profile} "
                f"双臂微颤={args.arm_micro_motion_profile}/"
                f"{args.arm_micro_motion_threshold_rad:g}rad "
                f"RTC(d={args.rtc_predicted_delay_steps},s={args.rtc_execution_horizon}) "
                f"进程隔离={args.rtc_process_isolation} "
                f"手部平滑={args.hand_smoothing_profile} "
                f"服务={args.policy_endpoint}"
            ),
        )

        logger.info(
            "【运行提示】DM0.5模型频率=30Hz，G2 Servo频率=100Hz，"
            "模式=%s，通道=%s，输出=%s",
            args.mode,
            args.servo_channel,
            args.servo_output_mode,
        )
        if powered:
            logger.warning(
                "【真机提示】p=暂停保持，r=重新采集恢复，q/Ctrl+C=安全退出"
            )
            logger.warning(
                "【启动安全】首个模型动作通过所有安全门之前，"
                "Servo发送次数严格为0"
            )
            if args.split_control_profile:
                if args.mode == "powered-rtc":
                    logger.critical(
                        "【RTC Split控制配置】双臂LOW-LATENCY无碰撞保护；"
                        "RTC首跳、裁剪幅度和动力学超限执行硬拒绝并保持"
                    )
                else:
                    logger.critical(
                        "【Split控制配置】双臂LOW-LATENCY无碰撞保护；首跳、"
                        "裁剪幅度和动力学超限只告警不拒绝。"
                        "仍保留合同、finite和物理关节限位"
                    )
            if args.servo_output_mode == "arms-only":
                logger.critical(
                    "【接口隔离】仅向14D双臂发送Servo；本进程绝不发送灵巧手动作"
                )
            elif args.servo_output_mode == "split":
                logger.critical(
                    "【正式Split接口】14D双臂走100Hz Servo；O10双手走独立"
                    f"{args.hand_stream_hz:g}Hz move_ee_pos"
                )
            elif args.servo_output_mode == "dual-side-17d":
                logger.critical(
                    "【双侧17D Servo候选】每个100Hz tick依次发送左侧17D与右侧17D；"
                    "O10名称来自get_end_state原字符串；绝不调用move_ee_pos"
                )
        else:
            logger.info("【Shadow提示】本模式只采集和推理，绝不发送Servo动作")
        loop.run()
        return 0
    except Exception:
        logger.exception("【运行失败】G2运行时异常结束，请查看上方原因和JSONL")
        return 1
    finally:
        if servo is not None:
            try:
                servo.stop()
                stats = servo.stats()
                logger.warning(
                    "【Servo汇总】发送次数=%d 保持次数=%d "
                    "已接受首目标=%s",
                    int(stats["send_count"]),
                    int(stats["hold_send_count"]),
                    servo.has_target(),
                )
            except Exception:
                logger.exception("Servo streamer stop/summary failed")
        if robot is not None and powered:
            try:
                control_stats = robot.control_stats()
                logger.warning(
                    "【硬件输出汇总】模式=%s Servo请求=%d 完整=%s "
                    "末次names/positions=%d/%d 双手频率=%gHz 双手发送次数=%d "
                    "重复跳过=%d 末次双手调用=%.6fs",
                    control_stats.get("request_layout", "unknown"),
                    int(control_stats.get("servo_request_count", 0)),
                    bool(control_stats.get("servo_requests_all_complete", False)),
                    int(control_stats.get("last_servo_joint_count", 0)),
                    int(control_stats.get("last_servo_position_count", 0)),
                    float(control_stats.get("hand_stream_hz", 0.0)),
                    int(control_stats.get("hand_send_count", 0)),
                    int(control_stats.get("hand_unchanged_skip_count", 0)),
                    float(control_stats.get("last_hand_send_seconds", 0.0)),
                )
            except Exception:
                logger.exception("Hardware output summary failed")
        if inference_pipeline is not None:
            try:
                state_stats = inference_pipeline.control_state_stats()
                logger.warning(
                    "【Control状态ring】读取=%d 失败=%d 最大读取=%.6fs "
                    "末序号=%d 容量=%d",
                    int(state_stats["read_count"]),
                    int(state_stats["read_failure_count"]),
                    float(state_stats["read_max_seconds"]),
                    int(state_stats["latest_ring_sequence"]),
                    int(state_stats["ring_capacity"]),
                )
            except Exception:
                logger.exception("Control state ring summary failed")
            if powered:
                try:
                    robot.emergency_stop()
                except Exception:
                    pass
        close_resources(
            (
                ("keyboard", keyboard),
                ("servo streamer", servo),
                ("cameras", cameras),
                ("process-isolated policy", inference_pipeline),
                ("robot", robot),
                ("policy backend", backend),
            )
        )
        if joint_feedback_writer is not None:
            joint_feedback_writer.close()
        if gdk_initialized and gdk_module is not None:
            try:
                gdk_module.gdk_release()
            except Exception:
                logger.exception("gdk_release failed")
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        if pid_acquired:
            _release_pid_file(args.pid_file)


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))

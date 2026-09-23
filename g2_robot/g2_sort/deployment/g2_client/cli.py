"""DM0.5 G2运行时的命令行合同。

用途：定义shadow、rtc-shadow、powered-rtc、powered-sync、powered-async及通用参数。
输入：命令行字符串列表，或默认读取当前进程的argv。
输出：完成范围校验的``argparse.Namespace``。
方法：冻结DM0.5的34D、50步、30Hz和100Hz，不暴露prompt、horizon或checkpoint。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from g2_sort.config.dm05_contract import POWERED_SAFETY_DEFAULTS
from .trajectory_smoothing import (
    HAND_V11_MAX_ACCELERATION_RAD_S2,
    HAND_V11_MAX_DELTA_RAD,
    HAND_V11_MAX_VELOCITY_RAD_S,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "G2 runtime with a fixed DM0.5 three-RGB/34D/50-step HTTP backend"
        )
    )
    parser.add_argument(
        "--mode",
        choices=(
            "shadow",
            "rtc-shadow",
            "powered-rtc",
            "powered-sync",
            "powered-async",
        ),
        default="shadow",
        help="shadow never sends servo commands; async must be selected explicitly",
    )
    parser.add_argument(
        "--safe-test",
        action="store_true",
        help="compatibility alias for --mode shadow; conflicts with powered modes",
    )
    parser.add_argument(
        "--policy-backend",
        choices=("dm05",),
        default="dm05",
    )
    parser.add_argument(
        "--policy-endpoint",
        default="http://10.130.153.114:7892/process_frame",
    )
    parser.add_argument(
        "--policy-transport",
        choices=("http-multipart-json", "websocket-binary"),
        default="http-multipart-json",
        help="frozen HTTP baseline or persistent binary WebSocket candidate",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--execute-prefix-steps",
        type=int,
        default=6,
        help="powered-sync 30 Hz intervals to execute (6 is about 0.2 seconds)",
    )
    parser.add_argument(
        "--chunk-prefetch-lookahead",
        type=int,
        default=20,
        help="powered-async starts one request at this many remaining 30 Hz intervals",
    )
    parser.add_argument(
        "--async-min-fresh-steps",
        type=int,
        default=12,
        help="reject async chunks with fewer fresh 30 Hz intervals",
    )
    parser.add_argument(
        "--rtc-predicted-delay-steps",
        type=int,
        default=20,
        help="RTC safe end-to-end delay estimate in 30 Hz steps",
    )
    parser.add_argument(
        "--rtc-execution-horizon",
        type=int,
        default=25,
        help="RTC logical request horizon s; requires d <= s <= 50-d",
    )
    parser.add_argument(
        "--rtc-process-isolation",
        action="store_true",
        help=(
            "run powered RTC or sync camera/decode/JPEG/HTTP/JSON in a "
            "spawned GDK worker so the 100 Hz control process never handles images"
        ),
    )
    parser.add_argument(
        "--max-start-jump-radians",
        type=float,
        default=0.05,
        help="reject execution if any joint differs this much from measured state",
    )
    parser.add_argument(
        "--max-hand-start-jump-radians",
        type=float,
        default=0.35,
        help=(
            "powered hard-gate limit for the executed 20D hand target; "
            "the arm limit remains --max-start-jump-radians"
        ),
    )
    parser.add_argument(
        "--boundary-blend-seconds",
        type=float,
        default=0.06,
        help="convex transition from current safe target into a new chunk",
    )
    parser.add_argument(
        "--boundary-blend-profile",
        choices=("cubic_smoothstep", "quintic_smootherstep"),
        default="cubic_smoothstep",
        help=(
            "chunk-boundary blend curve; cubic_smoothstep is the frozen "
            "baseline and quintic_smootherstep is the zero-endpoint-acceleration "
            "A/B candidate"
        ),
    )
    parser.add_argument(
        "--arm-internal-jerk-weight",
        type=float,
        default=0.0,
        help=(
            "experimental fixed-endpoint smoothing for the first 14 arm axes; "
            "0 disables it and the first powered candidate is 10"
        ),
    )
    parser.add_argument(
        "--arm-interpolation-profile",
        choices=("linear", "cubic_hermite", "cubic_spline"),
        default="linear",
        help=(
            "30 Hz to 100 Hz arm interpolation; cubic profiles preserve "
            "continuous velocity and are RTC smoothing candidates"
        ),
    )
    parser.add_argument(
        "--arm-micro-motion-profile",
        choices=("off", "stationary_minjerk_v1"),
        default="off",
        help=(
            "post-C2 near-stationary arm projection; off preserves the "
            "frozen baseline"
        ),
    )
    parser.add_argument(
        "--arm-micro-motion-threshold-rad",
        type=float,
        default=0.0035,
        help="maximum arm motion range eligible for near-stationary projection",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="0 runs until interrupted; otherwise bounds total policy requests",
    )
    parser.add_argument(
        "--observation-retry-seconds",
        type=float,
        default=0.1,
    )
    parser.add_argument(
        "--camera-timeout-ms",
        type=float,
        default=200.0,
    )
    parser.add_argument(
        "--gdk-stabilization-seconds",
        type=float,
        default=3.0,
    )
    parser.add_argument(
        "--servo-control-period",
        type=float,
        default=0.01,
        help="G2 position-servo period; DM0.5 runtime requires 0.01 (100 Hz)",
    )
    parser.add_argument(
        "--servo-channel",
        choices=("normal", "low-latency"),
        default="normal",
        help=(
            "normal keeps GDK collision protection; low-latency has no "
            "collision protection and must be selected explicitly"
        ),
    )
    parser.add_argument(
        "--servo-output-mode",
        choices=("unified-34", "arms-only", "split", "dual-side-17d"),
        default="split",
        help=(
            "split is the current default: 14D arms use joint_servo_control "
            "and O10 hands use rate-configured move_ee_pos; dual-side-17d is the "
            "two per-side continuous Servo candidate; unified-34 and arms-only "
            "remain explicit diagnostics"
        ),
    )
    parser.add_argument(
        "--enable-low-latency-servo",
        action="store_true",
        help="deprecated alias for --servo-channel low-latency",
    )
    parser.add_argument(
        "--split-control-profile",
        action="store_true",
        help=(
            "validated powered profile: split arm/hand dispatch, low-latency "
            "arms, 60 ms smoothstep entry, and warning-only action safety gates"
        ),
    )
    parser.add_argument(
        "--interactive-keys",
        action="store_true",
        help="force p/r/q single-key controls even outside powered defaults",
    )
    parser.add_argument(
        "--no-interactive-keys",
        action="store_true",
        help="disable p/r/q controls (Ctrl-C and SIGTERM remain active)",
    )
    parser.add_argument(
        "--max-clip-delta-radians",
        type=float,
        default=POWERED_SAFETY_DEFAULTS["max_clip_delta_radians"],
        help="reject a powered chunk if any limit clip exceeds this amount",
    )
    parser.add_argument(
        "--max-arm-velocity-rad-s",
        type=float,
        default=POWERED_SAFETY_DEFAULTS["max_arm_velocity_rad_s"],
    )
    parser.add_argument(
        "--max-hand-velocity-rad-s",
        type=float,
        default=POWERED_SAFETY_DEFAULTS["max_hand_velocity_rad_s"],
    )
    parser.add_argument(
        "--max-arm-acceleration-rad-s2",
        type=float,
        default=POWERED_SAFETY_DEFAULTS[
            "max_arm_acceleration_rad_s2"
        ],
    )
    parser.add_argument(
        "--max-hand-acceleration-rad-s2",
        type=float,
        default=POWERED_SAFETY_DEFAULTS[
            "max_hand_acceleration_rad_s2"
        ],
    )
    parser.add_argument(
        "--max-jerk-rad-s3",
        type=float,
        default=POWERED_SAFETY_DEFAULTS["max_jerk_rad_s3"],
    )
    parser.add_argument("--servo-timing-warning-only", action="store_true")
    parser.add_argument(
        "--max-servo-send-seconds",
        type=float,
        default=0.03,
    )
    parser.add_argument(
        "--max-servo-tick-lateness-seconds",
        type=float,
        default=0.03,
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=Path("/tmp/g2_dm05_runtime.pid"),
    )
    parser.add_argument(
        "--last-hand-command-json",
        type=Path,
        default=Path("logs/runtime/last_hand_command.json"),
        help="latest successfully acknowledged Split O10 command",
    )
    parser.add_argument(
        "--joint-feedback-probe-json",
        type=Path,
        default=Path("logs/diagnostics/joint_feedback_probe_latest.json"),
        help="read-only probe result; missing/failed result keeps command-only mode",
    )
    parser.add_argument(
        "--disable-joint-feedback",
        action="store_true",
        help="force command-only telemetry even when a probe passed",
    )
    parser.add_argument(
        "--hand-stream-hz",
        type=float,
        choices=(5.0, 10.0, 20.0, 30.0, 50.0),
        default=5.0,
        help=(
            "Split O10 command sampling rate; formal powered-sync and powered-rtc "
            "entry scripts explicitly select the validated 10 Hz setting"
        ),
    )
    parser.add_argument(
        "--hand-startup-delay-seconds",
        type=float,
        default=0.08,
        help="delay the first O10 send until the arm boundary blend has started",
    )
    parser.add_argument(
        "--hand-smoothing-profile",
        choices=("off", "v1", "v1_1", "rtc_cap_v2", "shared_preview_v1"),
        default="off",
        help=(
            "rate-aware causal O10 smoothing; v1/v1_1 are sync-only and "
            "rtc_cap_v2 uses separate RTC boundary/internal delta caps; "
            "shared_preview_v1 is the one Sync/RTC-shared q/v/a preview planner"
        ),
    )
    parser.add_argument(
        "--hand-boundary-blend-seconds",
        type=float,
        default=0.4,
    )
    parser.add_argument(
        "--hand-max-velocity-rad-s",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--hand-max-acceleration-rad-s2",
        type=float,
        default=10.0,
    )
    parser.add_argument(
        "--hand-max-delta-radians",
        type=float,
        default=0.4,
    )
    parser.add_argument(
        "--hand-boundary-max-delta-radians",
        type=float,
        default=0.15,
        help="rtc_cap_v2 first 5 Hz command delta cap",
    )
    parser.add_argument(
        "--hand-reversal-error-radians",
        type=float,
        default=0.15,
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use explicit deterministic mock cameras/state instead of agibot_gdk",
    )
    parser.add_argument(
        "--mock-state-json",
        type=Path,
        help="JSON file containing a 34-value list or {'state': [...]}",
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--telemetry-jsonl", type=Path)
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_args)
    if args.safe_test:
        if args.mode != "shadow":
            parser.error("--safe-test conflicts with powered modes")
        args.mode = "shadow"
    if args.enable_low_latency_servo:
        if (
            args.servo_channel == "normal"
            and any(
                value == "--servo-channel"
                or value.startswith("--servo-channel=")
                for value in raw_args
            )
        ):
            parser.error(
                "--enable-low-latency-servo conflicts with "
                "--servo-channel normal"
            )
        args.servo_channel = "low-latency"
    if args.split_control_profile:
        if args.mode in {"shadow", "rtc-shadow"}:
            parser.error(
                "--split-control-profile requires a powered mode"
            )
        channel_explicit = any(
            value == "--servo-channel"
            or value.startswith("--servo-channel=")
            for value in raw_args
        )
        output_mode_explicit = any(
            value == "--servo-output-mode"
            or value.startswith("--servo-output-mode=")
            for value in raw_args
        )
        if (
            channel_explicit
            and args.servo_channel != "low-latency"
            and args.servo_output_mode not in {"dual-side-17d", "unified-34"}
        ):
            parser.error(
                "--split-control-profile conflicts with "
                "--servo-channel normal"
            )
        if args.servo_output_mode == "unified-34" and not channel_explicit:
            parser.error("unified-34 control profile requires explicit --servo-channel normal")
        if args.servo_output_mode in {"dual-side-17d", "unified-34"} and output_mode_explicit:
            if args.servo_channel != "normal":
                parser.error(
                    "continuous hand Servo profile requires normal Servo"
                )
        else:
            args.servo_channel = "low-latency"
        if not output_mode_explicit:
            args.servo_output_mode = "split"
    if args.servo_timing_warning_only and (args.servo_output_mode != "unified-34" or args.servo_channel != "normal" or args.mode != "powered-sync"):
        parser.error("--servo-timing-warning-only requires powered-sync unified-34 normal")
    if args.interactive_keys and args.no_interactive_keys:
        parser.error("--interactive-keys conflicts with --no-interactive-keys")
    if args.hand_smoothing_profile in {"v1", "v1_1"} and (
        args.mode != "powered-sync" or args.servo_output_mode != "split"
    ):
        parser.error(
            "--hand-smoothing-profile v1/v1_1 requires powered-sync split output"
        )
    if args.hand_smoothing_profile == "v1_1":
        args.hand_max_velocity_rad_s = HAND_V11_MAX_VELOCITY_RAD_S
        args.hand_max_acceleration_rad_s2 = HAND_V11_MAX_ACCELERATION_RAD_S2
        args.hand_max_delta_radians = HAND_V11_MAX_DELTA_RAD
    if args.hand_smoothing_profile == "rtc_cap_v2" and (
        args.mode != "powered-rtc" or args.servo_output_mode != "split"
    ):
        parser.error(
            "--hand-smoothing-profile rtc_cap_v2 requires powered-rtc split output"
        )
    if args.hand_smoothing_profile == "shared_preview_v1" and (
        args.mode not in {"powered-sync", "powered-rtc"}
        or args.servo_output_mode != "split"
        or abs(args.hand_stream_hz - 10.0) > 1e-9
    ):
        parser.error(
            "--hand-smoothing-profile shared_preview_v1 requires "
            "10 Hz powered-sync/powered-rtc split output"
        )
    if args.rtc_process_isolation and args.mode not in {
        "shadow",
        "powered-rtc",
        "powered-sync",
        "rtc-shadow",
    }:
        parser.error(
            "--rtc-process-isolation requires powered-rtc, powered-sync, or rtc-shadow"
        )
    if args.policy_transport == "websocket-binary":
        if not args.rtc_process_isolation:
            parser.error("websocket-binary currently requires --rtc-process-isolation")
        if not args.policy_endpoint.startswith(("ws://", "wss://")):
            parser.error("websocket-binary endpoint must start with ws:// or wss://")
    elif not args.policy_endpoint.startswith(("http://", "https://")):
        parser.error("HTTP policy endpoint must start with http:// or https://")
    if args.request_timeout_seconds <= 0:
        parser.error("--request-timeout-seconds must be positive")
    if abs(args.servo_control_period - 0.01) > 1e-9:
        parser.error("DM0.5 G2 runtime freezes --servo-control-period at 0.01 (100 Hz)")
    if args.gdk_stabilization_seconds < 0:
        parser.error("--gdk-stabilization-seconds cannot be negative")
    if not (
        0 < args.hand_boundary_max_delta_radians
        <= args.hand_max_delta_radians
    ):
        parser.error(
            "hand delta caps require 0 < boundary <= internal max delta"
        )
    if not 1 <= args.execute_prefix_steps < 50:
        parser.error("--execute-prefix-steps must be in [1, 49]")
    if not 1 <= args.chunk_prefetch_lookahead < 50:
        parser.error("--chunk-prefetch-lookahead must be in [1, 49]")
    if not 1 <= args.async_min_fresh_steps <= 50:
        parser.error("--async-min-fresh-steps must be in [1, 50]")
    if not (
        0
        <= args.rtc_predicted_delay_steps
        <= args.rtc_execution_horizon
        <= 50 - args.rtc_predicted_delay_steps
    ):
        parser.error("RTC requires d <= s <= 50-d")
    if args.max_start_jump_radians <= 0:
        parser.error("--max-start-jump-radians must be positive")
    if args.max_hand_start_jump_radians <= 0:
        parser.error("--max-hand-start-jump-radians must be positive")
    if args.boundary_blend_seconds < 0:
        parser.error("--boundary-blend-seconds cannot be negative")
    if not 0.0 <= args.arm_internal_jerk_weight <= 100.0:
        parser.error("--arm-internal-jerk-weight must be in [0, 100]")
    if args.observation_retry_seconds < 0:
        parser.error("--observation-retry-seconds cannot be negative")
    if args.max_requests < 0:
        parser.error("--max-requests cannot be negative")
    if args.camera_timeout_ms <= 0:
        parser.error("--camera-timeout-ms must be positive")
    if not 0.0 <= args.hand_startup_delay_seconds <= 1.0:
        parser.error("--hand-startup-delay-seconds must be in [0, 1]")
    if args.hand_boundary_blend_seconds <= 0:
        parser.error("--hand-boundary-blend-seconds must be positive")
    if args.hand_reversal_error_radians < 0:
        parser.error("--hand-reversal-error-radians cannot be negative")
    for name in (
        "max_clip_delta_radians",
        "max_arm_velocity_rad_s",
        "max_hand_velocity_rad_s",
        "max_arm_acceleration_rad_s2",
        "max_hand_acceleration_rad_s2",
        "max_jerk_rad_s3",
        "max_servo_send_seconds",
        "max_servo_tick_lateness_seconds",
        "hand_max_velocity_rad_s",
        "hand_max_acceleration_rad_s2",
        "hand_max_delta_radians",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args

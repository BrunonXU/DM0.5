"""DM0.5 G2运行时的命令行合同。

用途：定义shadow、powered-sync、powered-async及通用GDK/日志参数。
输入：命令行字符串列表，或默认读取当前进程的argv。
输出：完成范围校验的``argparse.Namespace``。
方法：冻结DM0.5的34D、50步、30Hz和100Hz，不暴露prompt、horizon或checkpoint。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "G2 runtime with a fixed DM0.5 three-RGB/34D/50-step HTTP backend"
        )
    )
    parser.add_argument(
        "--mode",
        choices=("shadow", "powered-sync", "powered-async"),
        default="shadow",
        help="shadow never sends servo commands; async must be selected explicitly",
    )
    parser.add_argument(
        "--safe-test",
        action="store_true",
        help="legacy alias for --mode shadow; conflicts with powered modes",
    )
    parser.add_argument(
        "--policy-backend",
        choices=("dm05",),
        default="dm05",
    )
    parser.add_argument(
        "--policy-endpoint",
        default="http://127.0.0.1:7891/process_frame",
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
        "--max-start-jump-radians",
        type=float,
        default=0.35,
        help="reject execution if any joint differs this much from measured state",
    )
    parser.add_argument(
        "--boundary-blend-seconds",
        type=float,
        default=0.06,
        help="convex transition from current safe target into a new chunk",
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
        "--enable-low-latency-servo",
        action="store_true",
        help="opt into the GDK low-latency channel; normal channel is safer default",
    )
    parser.add_argument(
        "--interactive-keys",
        action="store_true",
        help="allow q+Enter to latch the software emergency stop",
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
    args = parser.parse_args(argv)
    if args.safe_test:
        if args.mode != "shadow":
            parser.error("--safe-test conflicts with powered modes")
        args.mode = "shadow"
    if args.request_timeout_seconds <= 0:
        parser.error("--request-timeout-seconds must be positive")
    if abs(args.servo_control_period - 0.01) > 1e-9:
        parser.error("DM0.5 G2 runtime freezes --servo-control-period at 0.01 (100 Hz)")
    if args.gdk_stabilization_seconds < 0:
        parser.error("--gdk-stabilization-seconds cannot be negative")
    if not 1 <= args.execute_prefix_steps < 50:
        parser.error("--execute-prefix-steps must be in [1, 49]")
    if not 1 <= args.chunk_prefetch_lookahead < 50:
        parser.error("--chunk-prefetch-lookahead must be in [1, 49]")
    if not 1 <= args.async_min_fresh_steps <= 50:
        parser.error("--async-min-fresh-steps must be in [1, 50]")
    if args.max_start_jump_radians <= 0:
        parser.error("--max-start-jump-radians must be positive")
    if args.boundary_blend_seconds < 0:
        parser.error("--boundary-blend-seconds cannot be negative")
    if args.observation_retry_seconds < 0:
        parser.error("--observation-retry-seconds cannot be negative")
    if args.max_requests < 0:
        parser.error("--max-requests cannot be negative")
    if args.camera_timeout_ms <= 0:
        parser.error("--camera-timeout-ms must be positive")
    return args

"""G2客户端的应用装配和生命周期管理。

用途：把CLI、策略后端、相机、机器人、servo、telemetry和控制循环组装起来。
输入：解析完成的命令行参数；真实模式读取GDK，mock模式使用确定性假数据。
输出：进程退出码、日志和可选JSONL telemetry；powered模式产生本地servo流。
方法：按mode创建最小资源，安装信号/软件急停，并按有界顺序清理资源。
"""

from __future__ import annotations

import json
import logging
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
from .keyboard import EmergencyStop, KeyboardQuitMonitor
from .mock import MockCameraRig
from .observation import ObservationSource
from .robot_control import G2RobotController, MockRobotController
from .servo import ServoStreamer
from .telemetry import Telemetry

logger = logging.getLogger(__name__)


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
    powered = args.mode != "shadow"
    options = RuntimeOptions(
        mode=args.mode,
        execute_prefix_steps=args.execute_prefix_steps,
        chunk_prefetch_lookahead=args.chunk_prefetch_lookahead,
        async_min_fresh_steps=args.async_min_fresh_steps,
        start_jump_limit_radians=args.max_start_jump_radians,
        boundary_blend_seconds=args.boundary_blend_seconds,
        observation_retry_seconds=args.observation_retry_seconds,
        max_requests=args.max_requests,
    )
    emergency_stop = EmergencyStop()
    backend = DM05HTTPBackend(
        endpoint=args.policy_endpoint,
        timeout_seconds=args.request_timeout_seconds,
    )
    cameras = None
    robot = None
    servo = None
    keyboard = None
    previous_handlers: dict[int, object] = {}

    def stop(reason: str) -> None:
        emergency_stop.activate(reason)
        if robot is not None:
            robot.emergency_stop()

    try:
        if args.mock:
            cameras = MockCameraRig()
            robot = MockRobotController(
                _load_mock_state(args.mock_state_json),
                powered=powered,
            )
        else:
            cameras = G2CameraRig(
                timeout_ms=args.camera_timeout_ms,
                stabilization_seconds=args.gdk_stabilization_seconds,
            )
            robot = G2RobotController(
                powered=powered,
                control_period=args.servo_control_period,
                enable_low_latency=args.enable_low_latency_servo,
                stabilization_seconds=args.gdk_stabilization_seconds,
            )

        observations = ObservationSource(cameras, robot)
        if powered:
            initial_state = robot.read_state()
            servo = ServoStreamer(robot.send_action, initial_state)

        telemetry = Telemetry(args.telemetry_jsonl)
        loop = ControlLoop(
            options=options,
            observations=observations,
            backend=backend,
            telemetry=telemetry,
            robot=robot if powered else None,
            servo=servo,
            stop_event=emergency_stop.event,
        )

        def signal_handler(signum: int, _frame: object) -> None:
            stop(f"signal {signum}")

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, signal_handler)
        if args.interactive_keys:
            keyboard = KeyboardQuitMonitor(lambda: stop("keyboard q"))
            keyboard.start()

        logger.info(
            "Starting G2 runtime mode=%s backend=dm05 endpoint=%s "
            "model_hz=30 servo_hz=100",
            args.mode,
            args.policy_endpoint,
        )
        loop.run()
        return 0
    except Exception:
        logger.exception("G2 runtime terminated with an error")
        return 1
    finally:
        if servo is not None:
            try:
                servo.hold()
                time.sleep(0.03)
            except Exception:
                pass
        close_resources(
            (
                ("keyboard", keyboard),
                ("servo streamer", servo),
                ("cameras", cameras),
                ("robot", robot),
                ("policy backend", backend),
            )
        )
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))

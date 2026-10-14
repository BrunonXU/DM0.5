#!/usr/bin/env python3
"""Send safety-gated F710 intent from Windows to the G2 Shadow receiver."""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path
import sys
import threading
import time
import uuid

import numpy as np

# Support direct execution from an unpacked/updated repository on Windows.
# Optional packages such as pygame are still validated by their own imports,
# but the local src-layout package does not require an editable install.
PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from g2dagger.f710_mapping import F710Mapper
from g2dagger.gamepad_windows import GamepadNotFoundError, WindowsF710Reader
from g2dagger.paths import artifact_root
from g2dagger.async_transport import AsyncLatestIntentTransport
from g2dagger.protocol import IntentPacket
from g2dagger.sender_guidance import operator_guidance, transport_guidance
from datetime import datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Windows F710 -> direct SSH tunnel -> G2 Shadow")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7904)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--hz", type=float, default=50.0)
    parser.add_argument("--ack-timeout-ms", type=float, default=500.0)
    parser.add_argument(
        "--log",
        type=Path,
        default=artifact_root(Path(__file__).resolve().parents[1]) / "logs/windows_sender_console.log",
    )
    parser.add_argument("--verbose", action="store_true", help="同时显示原始输入/ACK 字段")
    return parser.parse_args()


class _Tee:
    """Thread-safe console tee so a Windows tab closure cannot erase evidence."""

    def __init__(self, console: object, log_handle: object) -> None:
        self.console = console
        self.log_handle = log_handle
        self.lock = threading.Lock()

    def write(self, value: str) -> int:
        with self.lock:
            self.console.write(value)
            self.log_handle.write(value)
            self.console.flush()
            self.log_handle.flush()
        return len(value)

    def flush(self) -> None:
        with self.lock:
            self.console.flush()
            self.log_handle.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.console, "isatty", lambda: False)())


def install_console_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, handle)
    sys.stderr = _Tee(sys.stderr, handle)
    print(f"Persistent sender log: {path}", flush=True)


class WindowsTimerResolution:
    """Request 1 ms Windows scheduling granularity for the 50 Hz input loop."""

    def __init__(self) -> None:
        self._winmm = None
        self.enabled = False

    def __enter__(self) -> "WindowsTimerResolution":
        if sys.platform == "win32":
            self._winmm = ctypes.WinDLL("winmm")
            result = int(self._winmm.timeBeginPeriod(1))
            if result != 0:
                raise RuntimeError(f"timeBeginPeriod(1) failed: {result}")
            self.enabled = True
            print("Windows timer resolution: 1 ms", flush=True)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.enabled and self._winmm is not None:
            self._winmm.timeEndPeriod(1)
            self.enabled = False


def wait_until(deadline: float) -> float:
    """Sleep efficiently, then busy-wait only the final millisecond."""
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0.0:
            return now
        if remaining > 0.0015:
            time.sleep(remaining - 0.001)


def operator_status_key(intent: object, ack: object) -> tuple[object, ...]:
    """Exclude continuously changing axes so console I/O cannot stall sampling."""
    return (
        intent.active_side,
        intent.mode,
        intent.deadman,
        None if ack is None else ack.receiver_state,
        None if ack is None else ack.accepted,
        None if ack is None else ack.reason,
        intent.warning,
        getattr(intent, "speed_scale", 1.0),
        getattr(intent, "home_reset", False),
        getattr(intent, "hil_toggle_id", 0),
        getattr(intent, "hil_button_pressed", False),
    )


def make_packet(session_id: str, sequence: int, intent: object) -> IntentPacket:
    return IntentPacket(
        session_id=session_id,
        sequence=sequence,
        sent_at_unix_ns=time.time_ns(),
        deadman=intent.deadman,
        active_side=intent.active_side,
        mode=intent.mode,
        axes=intent.axes,
        grip_velocity=intent.grip_velocity,
        thumb_clearance=intent.thumb_clearance,
        warning=intent.warning,
        angular_axes=intent.angular_axes,
        grip_target=intent.grip_target,
        speed_scale=intent.speed_scale,
        home_reset=intent.home_reset,
        hil_toggle_id=intent.hil_toggle_id,
        hil_button_pressed=intent.hil_button_pressed,
    )


def wait_for_gamepad(reader: WindowsF710Reader) -> None:
    """Wait for Windows/SDL hot-plug instead of terminating the sender."""
    next_message = 0.0
    while True:
        try:
            reader.open()
            print(f"pygame device: {reader.name}", flush=True)
            return
        except GamepadNotFoundError:
            now = time.monotonic()
            if now >= next_message:
                print(
                    "HOLD / waiting for F710: plug in the USB receiver, switch "
                    "to X, wake the controller, and verify joy.cpl. Retrying...",
                    flush=True,
                )
                next_message = now + 5.0
            time.sleep(0.5)


def announce(message: str) -> None:
    print(f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] {message}", flush=True)


def main() -> int:
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    install_console_log(args.log)
    announce("DAgger sender 启动；流程提示 v2")
    if not 1.0 <= args.hz <= 100.0:
        raise ValueError("--hz must be in [1, 100]")
    print("INTENT CLIENT: robot output depends on the G2 receiver currently listening on this port.")
    print("Confirm the G2 terminal explicitly says SHADOW or POWERED before continuing.")
    print(f"Destination {args.host}:{args.port}; async latest-state transport.")
    announce("流程：tunnel → G2 Powered 监听 → sender 收到模式回执 → 首次 START 开始策略 → START 人工介入 → START 退出人工。")
    announce("人工：十字键选侧并释放 → RB 控制臂；RT 切换手开合；A 切换 1/2 倍速度；Y 请求 Home。")
    announce("录制由 G2 recording-mode 决定，START 不是录制开关。ACK 是接收端状态，不是 SDK 返回或实测运动。")
    mapper = F710Mapper()
    last_status: tuple[object, ...] | None = None
    last_guidance = None
    last_toggle = None
    period = 1.0 / args.hz
    sample_intervals_ms: list[float] = []
    try:
        with WindowsTimerResolution(), AsyncLatestIntentTransport(
            args.host, args.port, ack_timeout_s=args.ack_timeout_ms / 1000.0,
            status=lambda message: announce(transport_guidance(message)),
        ) as transport:
            reader = WindowsF710Reader(args.index)
            wait_for_gamepad(reader)
            generation = -1
            session_id = str(uuid.uuid4())
            sequence = 0
            next_tick = time.monotonic()
            last_sample_at: float | None = None
            next_report = next_tick + 5.0
            while True:
                now = time.monotonic()
                if now < next_tick:
                    now = wait_until(next_tick)
                # Maintain a stable cadence, but never replay missed samples.
                next_tick += period
                if next_tick <= now:
                    next_tick = now + period
                if last_sample_at is not None:
                    sample_intervals_ms.append((now - last_sample_at) * 1000.0)
                    if len(sample_intervals_ms) > 500:
                        sample_intervals_ms.pop(0)
                last_sample_at = now

                transport_state = transport.snapshot()
                if not transport_state.connected:
                    mapper.reset_for_disconnect()
                    generation = -1
                    continue
                if transport_state.generation != generation:
                    mapper.reset_for_disconnect()
                    generation = transport_state.generation
                    session_id = str(uuid.uuid4())
                    sequence = 0
                    last_guidance = last_status = None
                    last_toggle = None
                    announce(f"新发送会话 session={session_id} generation={generation}；旧回执已清空。")

                sample = reader.snapshot()
                if not sample.connected:
                    # Publish an explicit disabled state once, then let the G2
                    # watchdog independently hold while SDL is re-enumerated.
                    mapper.reset_for_disconnect()
                    hold_intent = mapper.map(sample)
                    if transport.publish(
                        generation, make_packet(session_id, sequence, hold_intent)
                    ):
                        sequence += 1
                    announce("[手柄断开] SDL 未检测到 F710；检查 USB 接收器、电源和 X 档，等待重新识别。")
                    reader.close()
                    wait_for_gamepad(reader)
                    mapper.reset_for_disconnect()
                    last_status = None
                    continue

                intent = mapper.map(sample)
                packet = make_packet(session_id, sequence, intent)
                if transport.publish(generation, packet):
                    sequence += 1
                ack = transport_state.last_ack
                status = operator_status_key(intent, ack)
                guidance = operator_guidance(intent, ack)
                if guidance != last_guidance:
                    announce(guidance)
                    last_guidance = guidance
                if last_toggle is not None and intent.hil_toggle_id != last_toggle:
                    announce(f"[START 输入] toggle={intent.hil_toggle_id}；已在本地检测，等待 G2 回执确认模式。若仍是 HIL，检查 RB 是否松开及手部/Home 是否完成。")
                last_toggle = intent.hil_toggle_id
                if status != last_status:
                    announce(f"输入：side={intent.active_side} RB请求={intent.deadman} 速度={intent.speed_scale}x Home请求={intent.home_reset} warning={intent.warning}；"
                             f"回执：state={None if ack is None else ack.receiver_state} reason={None if ack is None else ack.reason} ack_seq={None if ack is None else ack.sequence}")
                if args.verbose and status != last_status:
                    print(
                        "side=%s mode=%s enabled=%s receiver=%s accepted=%s "
                        "reason=%s warning=%s speed=%sx home_reset=%s hil_toggle_id=%s "
                        "start_pressed=%s linear=%s angular=%s "
                        "grip_target=%s c=%s"
                        % (
                            *status,
                            tuple(round(v, 2) for v in intent.axes),
                            tuple(round(v, 2) for v in intent.angular_axes),
                            intent.grip_target,
                            intent.thumb_clearance,
                        ),
                        flush=True,
                    )
                last_status = status

                if now >= next_report:
                    state = transport.snapshot()
                    sample = np.asarray(sample_intervals_ms, dtype=np.float64)
                    rtt = np.asarray(state.ack_rtt_ms, dtype=np.float64)
                    sample_text = (
                        "n/a" if not len(sample) else
                        f"p50={np.percentile(sample, 50):.1f} "
                        f"p99={np.percentile(sample, 99):.1f} max={np.max(sample):.1f}"
                    )
                    rtt_text = (
                        "n/a" if not len(rtt) else
                        f"p50={np.percentile(rtt, 50):.1f} "
                        f"p95={np.percentile(rtt, 95):.1f} "
                        f"p99={np.percentile(rtt, 99):.1f} max={np.max(rtt):.1f}"
                    )
                    announce(
                        f"TIMING sample_ms[{sample_text}] ack_rtt_ms[{rtt_text}] "
                        f"sent={state.sent_packets} acked={state.acked_packets} "
                        f"latest_overwrites={state.dropped_latest_packets} "
                        f"reconnects={state.reconnects} generation={generation} session={session_id} "
                        f"last_ack_seq={None if state.last_ack is None else state.last_ack.sequence}",
                    )
                    next_report = now + 5.0
    except KeyboardInterrupt:
        print("Stopped. Network close causes G2 receiver HOLD.")
    finally:
        if "reader" in locals():
            reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

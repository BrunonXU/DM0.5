"""软件急停、终端退出和隔离保留的轨迹回放辅助。

用途：为运行时提供线程安全的latched stop，以及可选q退出监听。
输入：操作员终端输入、急停原因；诊断类还可接收历史state和robot adapter。
输出：共享stop Event、退出回调或诊断回放命令。
方法：生产CLI只启用q+Enter急停；旧p/r/f行为保留但不会隐式接入VLA运行。
"""

from __future__ import annotations

import logging
import fcntl
import os
import select
import sys
import termios
import threading
import time
import tty
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class EmergencyStop:
    """Thread-safe, latched command gate shared by signals and keyboard."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def activate(self, reason: str = "operator request") -> None:
        if not self.event.is_set():
            logger.critical("SOFTWARE EMERGENCY STOP: %s", reason)
        self.event.set()

    def check(self) -> bool:
        return self.event.is_set()


class KeyboardQuitMonitor:
    """Read ``q``/Ctrl-C without changing terminal mode or blocking control."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None or not sys.stdin.isatty():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="g2-keyboard-estop",
            daemon=True,
        )
        self._thread.start()
        logger.info("Interactive emergency quit enabled: press q then Enter")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            ready, _, _ = select.select([sys.stdin], [], [], 0.0)
            if not ready:
                continue
            value = sys.stdin.readline().strip().lower()
            if value in {"q", "\x03"}:
                self._callback()
                return


class TrajectoryRecorder:
    """Legacy bounded state history used by optional pause/rewind diagnostics."""

    def __init__(self, maxlen: int = 1_000) -> None:
        self._history: deque[np.ndarray] = deque(maxlen=maxlen)
        self._replay_index = 0
        self._paused = False

    def record(self, state: np.ndarray) -> None:
        if not self._paused:
            self._history.append(np.asarray(state).copy())
            self._replay_index = len(self._history)

    def pause(self) -> None:
        self._paused = True
        self._replay_index = len(self._history)

    def resume(self) -> None:
        self._paused = False

    def rewind_step(self, robot: Any, steps: int = 10) -> bool:
        if not self._history:
            return False
        self._replay_index = max(0, self._replay_index - steps)
        robot.send_action(list(self._history)[self._replay_index])
        time.sleep(0.04)
        return True

    def forward_step(self, robot: Any, steps: int = 10) -> bool:
        if not self._history:
            return False
        self._replay_index = min(
            len(self._history) - 1, self._replay_index + steps
        )
        robot.send_action(list(self._history)[self._replay_index])
        time.sleep(0.04)
        return True

    def is_paused(self) -> bool:
        return self._paused


class LegacyKeyboardController:
    """Preserved p/r/f/q controller for explicit diagnostic composition.

    The production CLI intentionally wires only the latched q emergency path;
    rewind/forward motion must not be activated implicitly in a VLA run.
    """

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self.fd)
        self._paused = False
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        tty.setcbreak(self.fd)

    def close(self) -> None:
        try:
            termios.tcsetattr(
                self.fd, termios.TCSADRAIN, self._old_settings
            )
            flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
            fcntl.fcntl(self.fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        except Exception:
            pass

    def check_input(
        self,
        recorder: TrajectoryRecorder | None,
        robot: Any,
    ) -> tuple[bool, bool]:
        try:
            character = sys.stdin.read(1)
        except (OSError, BlockingIOError):
            return False, False
        if not character:
            return False, False
        character = character.lower()
        if character in {"q", "\x03"}:
            return True, False
        if character in {"p", " "}:
            self._paused = not self._paused
            if recorder is not None:
                recorder.pause() if self._paused else recorder.resume()
            return False, True
        if self._paused and recorder is not None:
            if character == "r":
                recorder.rewind_step(robot)
            elif character == "f":
                recorder.forward_step(robot)
        return False, False

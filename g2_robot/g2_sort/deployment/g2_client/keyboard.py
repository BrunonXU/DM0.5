"""Non-blocking single-key pause, resume, and graceful-stop controls.

Purpose
-------
Provide one production keyboard contract for the G2 runtime. ``p`` pauses and
holds, ``r`` resumes only through a fresh observation, and ``q`` requests a
graceful exit. Hardware emergency stop remains the primary safety mechanism.

Input / output
--------------
Reads a TTY in cbreak mode and calls a command callback with ``p``, ``r``, or
``q``. The original terminal settings are restored during cleanup.

Method
------
One daemon reader polls the TTY without Enter; callbacks only update thread-safe
runtime events and never send robot actions directly.
"""

from __future__ import annotations

import logging
import select
import sys
import termios
import threading
import tty
from collections.abc import Callable

logger = logging.getLogger(__name__)


class RuntimeCommandState:
    """Thread-safe pause/stop state shared by signals, keyboard, and loops."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

    def pause(self) -> None:
        if not self.stop_event.is_set():
            self.pause_event.set()

    def resume(self) -> None:
        if not self.stop_event.is_set():
            self.pause_event.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()


class KeyboardCommandMonitor:
    """Read p/r/q immediately from a TTY without blocking the control loop."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_settings: list[object] | None = None

    def start(self) -> bool:
        if self._thread is not None or not sys.stdin.isatty():
            return False
        self._fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._thread = threading.Thread(
            target=self._run,
            name="g2-keyboard-control",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Interactive controls enabled: p=pause/hold r=fresh resume q=quit"
        )
        return True

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.3)
            self._thread = None
        if self._fd is not None and self._old_settings is not None:
            try:
                termios.tcsetattr(
                    self._fd,
                    termios.TCSADRAIN,
                    self._old_settings,
                )
            except Exception:
                logger.exception("Failed to restore terminal settings")
        self._fd = None
        self._old_settings = None

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            ready, _, _ = select.select([sys.stdin], [], [], 0.0)
            if not ready:
                continue
            value = sys.stdin.read(1).lower()
            if value in {"p", "r", "q"}:
                self._callback(value)
                if value == "q":
                    return

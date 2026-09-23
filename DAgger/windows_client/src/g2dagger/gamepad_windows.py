"""Windows F710 snapshots through pygame/SDL2; contains no network or robot code."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from .f710_mapping import GamepadSnapshot


class GamepadNotFoundError(RuntimeError):
    """SDL currently exposes no joystick at the requested index."""


@dataclass(frozen=True)
class WindowsF710Layout:
    left_x: int = 0
    left_y: int = 1
    right_x: int = 2
    right_y: int = 3
    left_trigger: int = 4
    right_trigger: int = 5
    lb: int = 4
    rb: int = 5
    # Expected XInput START index. Must be confirmed by the next Windows
    # read-only test before it is allowed to switch a Powered HIL supervisor.
    start: int = 7
    hat: int = 0
    triggers_rest_at_minus_one: bool = True


def normalize_trigger(value: float, rests_at_minus_one: bool = True) -> float:
    normalized = (value + 1.0) * 0.5 if rests_at_minus_one else value
    return max(0.0, min(1.0, normalized))


class WindowsF710Reader:
    def __init__(self, joystick_index: int = 0, layout: WindowsF710Layout | None = None) -> None:
        self.joystick_index = joystick_index
        self.layout = layout or WindowsF710Layout()
        self._pygame: Any = None
        self._joystick: Any = None

    @property
    def name(self) -> str:
        return "" if self._joystick is None else str(self._joystick.get_name())

    def open(self) -> None:
        # Match a normal game loop on Windows: keep controller reports alive
        # while another terminal tab has focus, and let SDL own its raw-input
        # message thread.  SDL requires both hints before pygame/SDL init.
        os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
        os.environ.setdefault("SDL_JOYSTICK_THREAD", "1")
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError("pygame is required; run: py -m pip install -e .[windows]") from exc
        pygame.init()
        pygame.joystick.init()
        if pygame.joystick.get_count() <= self.joystick_index:
            pygame.quit()
            raise GamepadNotFoundError(
                "F710 not found by pygame; verify USB receiver, X mode, power, "
                "and Windows Game Controllers (joy.cpl)"
            )
        joystick = pygame.joystick.Joystick(self.joystick_index)
        joystick.init()
        self._pygame = pygame
        self._joystick = joystick

    def close(self) -> None:
        if self._joystick is not None:
            self._joystick.quit()
        if self._pygame is not None:
            self._pygame.quit()
        self._joystick = None
        self._pygame = None

    def snapshot(self) -> GamepadSnapshot:
        if self._joystick is None or self._pygame is None:
            return GamepadSnapshot(connected=False)
        # event.pump() alone updates state but leaves every event queued.  A
        # game drains its queue every frame; otherwise Windows can eventually
        # classify the process as unresponsive.  Axis/button state is still
        # read directly below, so old events are intentionally discarded.
        self._pygame.event.get()
        if not self._joystick.get_init():
            return GamepadSnapshot(connected=False)
        js, layout = self._joystick, self.layout
        try:
            hat_x, hat_y = js.get_hat(layout.hat)
            return GamepadSnapshot(
                left_x=js.get_axis(layout.left_x), left_y=js.get_axis(layout.left_y),
                right_x=js.get_axis(layout.right_x), right_y=js.get_axis(layout.right_y),
                left_trigger=normalize_trigger(js.get_axis(layout.left_trigger), layout.triggers_rest_at_minus_one),
                right_trigger=normalize_trigger(js.get_axis(layout.right_trigger), layout.triggers_rest_at_minus_one),
                dpad_x=int(hat_x), dpad_y=int(hat_y),
                lb=bool(js.get_button(layout.lb)), rb=bool(js.get_button(layout.rb)),
                a=bool(js.get_button(0)), b=bool(js.get_button(1)),
                x=bool(js.get_button(2)), y=bool(js.get_button(3)),
                start=bool(js.get_button(layout.start)),
            )
        except Exception:
            return GamepadSnapshot(connected=False)

    def __enter__(self) -> "WindowsF710Reader":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

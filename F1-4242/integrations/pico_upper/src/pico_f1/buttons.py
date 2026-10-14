"""Deterministic PICO button gestures for reset and episode recording."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class XYButtonDecision:
    reset_requested: bool = False
    start_recording: bool = False
    stop_recording: bool = False


@dataclass(frozen=True)
class ReviewDecision:
    save: bool = False
    discard: bool = False


class _DoubleClick:
    def __init__(self, window_s: float, debounce_s: float) -> None:
        if not math.isfinite(window_s) or window_s <= 0.0:
            raise ValueError("double-click window must be positive and finite")
        if not math.isfinite(debounce_s) or not 0.0 <= debounce_s < window_s:
            raise ValueError("debounce must be finite and smaller than the window")
        self.window_s = float(window_s)
        self.debounce_s = float(debounce_s)
        self._pressed = False
        self._first_rise: float | None = None
        self._last_rise = -math.inf

    def reset(self) -> None:
        self._pressed = False
        self._first_rise = None
        self._last_rise = -math.inf

    def update(self, pressed: bool, now: float) -> bool:
        if not math.isfinite(now):
            raise ValueError("button timestamp must be finite")
        pressed = bool(pressed)
        fired = False
        if self._first_rise is not None and now - self._first_rise > self.window_s:
            self._first_rise = None
        if pressed and not self._pressed and now - self._last_rise >= self.debounce_s:
            self._last_rise = now
            if self._first_rise is not None and now - self._first_rise <= self.window_s:
                fired = True
                self._first_rise = None
            else:
                self._first_rise = now
        self._pressed = pressed
        return fired


class XYRecordingArbiter:
    """Give an X+Y chord priority over the individual double-click gestures.

    The reset event fires on the first sampled frame where both buttons are down.
    Both component clicks are discarded until X and Y have both been released, so
    a reset can neither start nor stop an episode.
    """

    def __init__(self, double_click_s: float, debounce_s: float) -> None:
        # XR already reports a digital state. Distinct press/release edges need
        # no minimum spacing; a 40 ms debounce used to erase fast second clicks.
        self._x = _DoubleClick(double_click_s, 0.0)
        self._y = _DoubleClick(double_click_s, 0.0)
        self._chord_lockout = False
        # XR button streams can briefly report both buttons released while a
        # chord is physically held. Require a stable release before rearming,
        # otherwise one physical X+Y press can queue a second reset.
        self._release_stable_s = max(0.10, float(debounce_s))
        self._release_started: float | None = None

    def update(self, x_pressed: bool, y_pressed: bool, now: float) -> XYButtonDecision:
        if not math.isfinite(now):
            raise ValueError("button timestamp must be finite")
        x_pressed = bool(x_pressed)
        y_pressed = bool(y_pressed)
        if self._chord_lockout:
            if not x_pressed and not y_pressed:
                if self._release_started is None:
                    self._release_started = now
                elif now - self._release_started >= self._release_stable_s:
                    self._chord_lockout = False
                    self._release_started = None
                    self._x.reset()
                    self._y.reset()
            else:
                self._release_started = None
            return XYButtonDecision()
        if x_pressed and y_pressed:
            self._chord_lockout = True
            self._release_started = None
            self._x.reset()
            self._y.reset()
            return XYButtonDecision(reset_requested=True)
        return XYButtonDecision(
            start_recording=self._x.update(x_pressed, now),
            stop_recording=self._y.update(y_pressed, now),
        )


class ReviewButtonArbiter:
    """Resolve B single-click save versus B double-click discard.

    A single click is deliberately emitted only when the double-click window has
    expired. This is the only causal way to guarantee that a later second click
    cannot save an episode before it is discarded.
    """

    def __init__(self, double_click_s: float, debounce_s: float) -> None:
        self.window_s = float(double_click_s)
        self.debounce_s = float(debounce_s)
        if not math.isfinite(self.window_s) or self.window_s <= 0.0:
            raise ValueError("double-click window must be positive and finite")
        if not math.isfinite(self.debounce_s) or not 0.0 <= self.debounce_s < self.window_s:
            raise ValueError("debounce must be finite and smaller than the window")
        self.reset()

    def reset(self) -> None:
        self._pressed = False
        self._first_rise: float | None = None
        self._last_rise = -math.inf

    def update(self, pressed: bool, now: float) -> ReviewDecision:
        if not math.isfinite(now):
            raise ValueError("button timestamp must be finite")
        pressed = bool(pressed)
        if pressed and not self._pressed and now - self._last_rise >= self.debounce_s:
            self._last_rise = now
            if self._first_rise is not None and now - self._first_rise <= self.window_s:
                self._first_rise = None
                self._pressed = pressed
                return ReviewDecision(discard=True)
            self._first_rise = now
        self._pressed = pressed
        if self._first_rise is not None and now - self._first_rise > self.window_s:
            self._first_rise = None
            return ReviewDecision(save=True)
        return ReviewDecision()

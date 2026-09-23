"""Stateful and interlocked mapping from F710 snapshots to operator intent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class MotionMode(str, Enum):
    TRANSLATION = "translation"
    ROTATION = "rotation"
    COMBINED = "combined"


@dataclass(frozen=True)
class GamepadSnapshot:
    connected: bool = True
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    dpad_x: int = 0
    dpad_y: int = 0
    lb: bool = False
    rb: bool = False
    a: bool = False
    b: bool = False
    x: bool = False
    y: bool = False
    start: bool = False

    def __post_init__(self) -> None:
        analogs = (
            self.left_x, self.left_y, self.right_x, self.right_y,
            self.left_trigger, self.right_trigger,
        )
        if not all(math.isfinite(v) and -1.0 <= v <= 1.0 for v in analogs):
            raise ValueError("all analog values must be finite and in [-1, 1]")
        if self.dpad_x not in {-1, 0, 1} or self.dpad_y not in {-1, 0, 1}:
            raise ValueError("D-pad values must be -1, 0, or 1")


@dataclass(frozen=True)
class MappedIntent:
    deadman: bool
    active_side: str | None
    mode: str
    axes: tuple[float, float, float]
    grip_velocity: float
    thumb_clearance: bool
    warning: str | None = None
    angular_axes: tuple[float, float, float] = (0.0, 0.0, 0.0)
    grip_target: float | None = None
    speed_scale: float = 1.0
    home_reset: bool = False
    hil_toggle_id: int = 0
    hil_button_pressed: bool = False


class F710Mapper:
    """Requires neutral controls and a fresh RB edge before enabling output."""

    def __init__(
        self,
        stick_deadband: float = 0.12,
        neutral_threshold: float = 0.18,
        grip_press_threshold: float = 0.65,
        grip_release_threshold: float = 0.35,
    ) -> None:
        if not 0.0 <= stick_deadband < neutral_threshold < 1.0:
            raise ValueError("require 0 <= deadband < neutral_threshold < 1")
        if not 0.0 <= grip_release_threshold < grip_press_threshold <= 1.0:
            raise ValueError("require 0 <= grip release < grip press <= 1")
        self.stick_deadband = stick_deadband
        self.neutral_threshold = neutral_threshold
        self.grip_press_threshold = grip_press_threshold
        self.grip_release_threshold = grip_release_threshold
        self.active_side: str | None = None
        self.mode = MotionMode.COMBINED
        self._neutral_seen = False
        self._previous_rb = False
        self._previous_a = False
        self._previous_dpad = (0, 0)
        self._previous_start = False
        self._start_release_seen = False
        self._hil_toggle_id = 0
        self._armed_for_hold = False
        self._right_trigger_pressed = False
        self._speed_scale = 1.0
        # Unknown is deliberate: arming teleop must never open or close a hand.
        # A side gets a binary target only after its first explicit RT click.
        self._grip_target_by_side: dict[str, float | None] = {
            "left": None,
            "right": None,
        }

    def reset_for_disconnect(self) -> None:
        self.active_side = None
        self._neutral_seen = False
        self._previous_rb = False
        self._previous_a = False
        self._previous_dpad = (0, 0)
        self._previous_start = False
        self._start_release_seen = False
        self._armed_for_hold = False
        self._right_trigger_pressed = False
        self._speed_scale = 1.0
        # A reconnect (including the one after Y global Home) must not replay
        # an old binary hand target into the freshly restored Home pose.
        self._grip_target_by_side = {"left": None, "right": None}

    def _apply_deadband(self, value: float) -> float:
        magnitude = abs(value)
        if magnitude <= self.stick_deadband:
            return 0.0
        scaled = (magnitude - self.stick_deadband) / (1.0 - self.stick_deadband)
        return math.copysign(min(1.0, scaled), value)

    def _is_neutral(self, sample: GamepadSnapshot) -> bool:
        return (
            max(abs(sample.left_x), abs(sample.left_y), abs(sample.right_x), abs(sample.right_y))
            <= self.neutral_threshold
            and sample.left_trigger <= self.neutral_threshold
            and sample.right_trigger <= self.neutral_threshold
            and not sample.rb
            and not sample.lb
            and not sample.a
            and not sample.b
            and not sample.x
            and not sample.y
            and not sample.start
        )

    def map(self, sample: GamepadSnapshot) -> MappedIntent:
        if not sample.connected:
            self.reset_for_disconnect()
            return MappedIntent(
                False,
                None,
                self.mode.value,
                (0.0, 0.0, 0.0),
                0.0,
                False,
                "controller_disconnected",
                hil_toggle_id=self._hil_toggle_id,
                hil_button_pressed=False,
            )

        if self._is_neutral(sample):
            self._neutral_seen = True

        if not sample.start:
            self._start_release_seen = True
        rising_start = sample.start and not self._previous_start
        self._previous_start = sample.start
        if rising_start and self._start_release_seen:
            self._hil_toggle_id += 1

        rising_a = sample.a and not self._previous_a
        self._previous_a = sample.a
        if rising_a and self._neutral_seen:
            self._speed_scale = 2.0 if self._speed_scale == 1.0 else 1.0

        grip_pressed_edge = False
        if sample.right_trigger >= self.grip_press_threshold:
            grip_pressed_edge = not self._right_trigger_pressed
            self._right_trigger_pressed = True
        elif sample.right_trigger <= self.grip_release_threshold:
            self._right_trigger_pressed = False

        warning: str | None = None
        dpad = (sample.dpad_x, sample.dpad_y)
        dpad_changed = dpad != self._previous_dpad
        selection_changed_this_sample = False
        if dpad_changed and dpad != (0, 0):
            if sample.rb:
                warning = "select_side_before_deadman"
            elif sample.dpad_x and sample.dpad_y:
                warning = "diagonal_dpad_ignored"
            elif sample.dpad_x == -1:
                self.active_side = "left"
                selection_changed_this_sample = True
            elif sample.dpad_x == 1:
                self.active_side = "right"
                selection_changed_this_sample = True
            elif sample.dpad_y:
                warning = "vertical_dpad_unassigned_in_combined_mode"
        # Always consume the D-pad edge. A selection attempted during RB must
        # be released and pressed again; it must not apply later by surprise.
        self._previous_dpad = dpad

        rising_rb = sample.rb and not self._previous_rb
        self._previous_rb = sample.rb
        if rising_rb:
            self._armed_for_hold = (
                self._neutral_seen
                and self.active_side is not None
                and not selection_changed_this_sample
                and dpad == (0, 0)
            )
            if selection_changed_this_sample or dpad != (0, 0):
                warning = "release_dpad_then_press_deadman"
            elif not self._neutral_seen:
                warning = "deadman_before_neutral_ignored"
            elif self.active_side is None:
                warning = "deadman_without_active_side"
        if not sample.rb:
            self._armed_for_hold = False

        if sample.y:
            if sample.rb:
                warning = "release_deadman_before_home_reset"
            elif self._neutral_seen:
                self.active_side = None
                self._armed_for_hold = False
                self._speed_scale = 1.0
                self._grip_target_by_side = {"left": None, "right": None}
                return MappedIntent(
                    False,
                    None,
                    self.mode.value,
                    (0.0, 0.0, 0.0),
                    0.0,
                    False,
                    warning,
                    speed_scale=self._speed_scale,
                    home_reset=True,
                    hil_toggle_id=self._hil_toggle_id,
                    hil_button_pressed=sample.start,
                )

        # RT is an independent discrete hand control. It only needs a selected
        # side; RB continues to gate every arm Cartesian output.
        if grip_pressed_edge:
            if self.active_side is None:
                warning = "hand_intent_without_active_side"
            else:
                previous_target = self._grip_target_by_side[self.active_side]
                self._grip_target_by_side[self.active_side] = (
                    1.0 if previous_target is None or previous_target < 0.5 else 0.0
                )

        enabled = sample.rb and self._armed_for_hold
        if not enabled:
            return MappedIntent(
                False, self.active_side, self.mode.value,
                (0.0, 0.0, 0.0), 0.0, False, warning,
                grip_target=(
                    None
                    if self.active_side is None
                    else self._grip_target_by_side[self.active_side]
                ),
                speed_scale=self._speed_scale,
                hil_toggle_id=self._hil_toggle_id,
                hil_button_pressed=sample.start,
            )

        lx = self._apply_deadband(sample.left_x)
        ly = self._apply_deadband(sample.left_y)
        rx = self._apply_deadband(sample.right_x)
        ry = self._apply_deadband(sample.right_y)
        # Continuous correction layout: both pose components can be commanded
        # together without a mode switch. +X forward, +Y left, +Z up.
        linear_axes = (-ly, -lx, float(sample.lb) - sample.left_trigger)
        # Field-validated natural mapping (2026-09-01): the previous signs felt
        # like inverted flight controls on both axes. X/B remain roll -/+.
        angular_axes = (float(sample.b) - float(sample.x), ry, -rx)
        return MappedIntent(
            True,
            self.active_side,
            self.mode.value,
            linear_axes,
            0.0,
            False,
            warning,
            angular_axes,
            self._grip_target_by_side[self.active_side],
            self._speed_scale,
            False,
            self._hil_toggle_id,
            sample.start,
        )

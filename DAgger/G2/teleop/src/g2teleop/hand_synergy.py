"""Dataset-fitted two-control mapping for each ten-joint O10 hand."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np


class HandSynergyError(ValueError):
    pass


class DualHandSynergy:
    def __init__(self, config_path: Path) -> None:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.status = str(payload.get("status", ""))
        self.powered_allowed = bool(payload.get("powered_allowed", False))
        self.open: dict[str, np.ndarray] = {}
        self.closed: dict[str, np.ndarray] = {}
        self.clearance: dict[str, np.ndarray] = {}
        for side in ("left", "right"):
            section = payload[side]
            self.open[side] = self._ten(section["open_g0"], f"{side}.open_g0")
            self.closed[side] = self._ten(section["closed_g1"], f"{side}.closed_g1")
            thumb = np.asarray(section["thumb_clearance_c1"], dtype=np.float64)
            if thumb.shape != (3,) or not np.isfinite(thumb).all():
                raise HandSynergyError(f"{side}.thumb_clearance_c1 must be finite 3D")
            self.clearance[side] = thumb

    @staticmethod
    def _ten(values: object, label: str) -> np.ndarray:
        result = np.asarray(values, dtype=np.float64)
        if result.shape != (10,) or not np.isfinite(result).all():
            raise HandSynergyError(f"{label} must be finite 10D")
        return result

    def estimate_grip(self, side: str, measured: np.ndarray) -> float:
        value = self._ten(measured, "measured hand")
        indices = np.asarray([4, 5, 7, 9], dtype=np.intp)
        span = self.closed[side][indices] - self.open[side][indices]
        if np.min(np.abs(span)) < 1e-5:
            raise HandSynergyError(f"{side} grip template has a degenerate PIP span")
        return float(np.clip(np.median((value[indices] - self.open[side][indices]) / span), 0, 1))

    def target(self, side: str, grip: float, clearance: float) -> np.ndarray:
        if side not in {"left", "right"}:
            raise HandSynergyError(f"invalid side {side!r}")
        g = float(np.clip(grip, 0.0, 1.0))
        c = float(np.clip(clearance, 0.0, 1.0))
        result = (1.0 - g) * self.open[side] + g * self.closed[side]
        result[:3] = (1.0 - c) * result[:3] + c * self.clearance[side]
        return result


class DualHandCommandState:
    """Integrate g/c and slew the 20D command toward the fitted manifold."""

    def __init__(self, synergy: DualHandSynergy, measured20: np.ndarray) -> None:
        measured = np.asarray(measured20, dtype=np.float64)
        if measured.shape != (20,) or not np.isfinite(measured).all():
            raise HandSynergyError("initial hand feedback must be finite 20D")
        if np.max(np.abs(measured)) < 0.05:
            raise HandSynergyError("all-zero/near-zero O10 feedback is unsafe to echo")
        self.synergy = synergy
        self.command = measured.copy()
        self.grip = {
            "left": synergy.estimate_grip("left", measured[:10]),
            "right": synergy.estimate_grip("right", measured[10:]),
        }
        self.clearance = {"left": 0.0, "right": 0.0}

    def step(
        self,
        *,
        side: str | None,
        enabled: bool,
        grip_velocity: float,
        thumb_clearance: bool,
        dt: float,
        grip_target: float | None = None,
        grip_speed_per_s: float = 0.30,
        clearance_speed_per_s: float = 2.0,
        max_joint_speed_rad_s: float = 0.30,
    ) -> np.ndarray:
        if not enabled or side not in {"left", "right"}:
            return self.command.copy()
        if grip_target is None:
            grip_goal = self.grip[side] + float(grip_velocity) * grip_speed_per_s * dt
        else:
            grip_goal = float(np.clip(grip_target, 0.0, 1.0))
            grip_goal = self.grip[side] + float(np.clip(
                grip_goal - self.grip[side],
                -grip_speed_per_s * dt,
                grip_speed_per_s * dt,
            ))
        self.grip[side] = float(np.clip(grip_goal, 0.0, 1.0))
        c_goal = 1.0 if thumb_clearance else 0.0
        c_delta = np.clip(
            c_goal - self.clearance[side],
            -clearance_speed_per_s * dt,
            clearance_speed_per_s * dt,
        )
        self.clearance[side] += float(c_delta)
        desired = self.synergy.target(side, self.grip[side], self.clearance[side])
        hand_slice = slice(0, 10) if side == "left" else slice(10, 20)
        current = self.command[hand_slice]
        grip_commanded = grip_target is not None or abs(float(grip_velocity)) > 0.0
        if not grip_commanded:
            # Before the first explicit RT/grip command, arming F710 must not
            # project the measured fingers onto the fitted synergy manifold.
            # A may still move the thumb, and releasing A may return that thumb,
            # while the other seven joints preserve their commanded values.
            if thumb_clearance or self.clearance[side] > 0.0:
                desired[3:] = current[3:]
            else:
                desired = current.copy()
        max_delta = max_joint_speed_rad_s * dt
        self.command[hand_slice] = current + np.clip(desired - current, -max_delta, max_delta)
        return self.command.copy()


@dataclass(frozen=True)
class HandScheduleDecision:
    """One F710 hand scheduler decision on the caller's control clock."""

    command: np.ndarray
    should_send: bool
    reason: str
    intent_id: int
    intent_changed: bool
    sampled: bool


class ChangedOnlyHandScheduler:
    """Sample a gradual hand intent at low rate and never resend an unchanged target.

    ``move_ee_pos`` is a point-target interface, not a continuous Servo stream.
    The caller may tick this object at 100 Hz, but the hand state advances only
    at ``nominal_hz``.  A changed semantic intent becomes pending immediately,
    but never bypasses the low-rate call spacing. Disabled periods freeze the
    last generated and last successfully submitted command.
    """

    def __init__(
        self,
        state: DualHandCommandState,
        *,
        nominal_hz: float = 5.0,
    ) -> None:
        if not math.isfinite(nominal_hz) or nominal_hz <= 0.0:
            raise ValueError("nominal_hz must be finite and positive")
        self.state = state
        self.nominal_hz = float(nominal_hz)
        self.period_s = 1.0 / self.nominal_hz
        self.last_sent = state.command.copy()
        self.next_sample_at: float | None = None
        self.last_sample_at: float | None = None
        self.intent_id = 0
        self._intent_signature: tuple[object, ...] | None = None

    def step(
        self,
        *,
        now: float,
        side: str | None,
        enabled: bool,
        grip_velocity: float,
        thumb_clearance: bool,
        grip_target: float | None = None,
    ) -> HandScheduleDecision:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if not enabled or side not in {"left", "right"}:
            self.next_sample_at = None
            return self._decision(False, "disabled_hold", False, False)

        has_hand_intent = (
            grip_target is not None
            or abs(float(grip_velocity)) > 0.0
            or bool(thumb_clearance)
            or self.state.clearance[side] > 0.0
        )
        if not has_hand_intent:
            self.next_sample_at = None
            return self._decision(False, "no_hand_intent_hold", False, False)

        signature = (
            side,
            float(grip_velocity),
            None if grip_target is None else float(grip_target),
            bool(thumb_clearance),
        )
        intent_changed = signature != self._intent_signature
        if intent_changed:
            self._intent_signature = signature
            self.intent_id += 1
            self.next_sample_at = self._first_legal_sample_at(float(now))
        elif self.next_sample_at is None:
            self.next_sample_at = self._first_legal_sample_at(float(now))

        assert self.next_sample_at is not None
        if now < self.next_sample_at:
            return self._decision(
                False,
                "waiting_for_5hz_slot",
                intent_changed,
                False,
            )

        command = self.state.step(
            side=side,
            enabled=True,
            grip_velocity=grip_velocity,
            grip_target=grip_target,
            thumb_clearance=thumb_clearance,
            dt=self.period_s,
        )
        # Schedule from the actual sample time and never issue catch-up bursts.
        self.last_sample_at = float(now)
        self.next_sample_at = float(now) + self.period_s
        if np.array_equal(command, self.last_sent):
            return HandScheduleDecision(
                command=command,
                should_send=False,
                reason="sampled_but_unchanged",
                intent_id=self.intent_id,
                intent_changed=intent_changed,
                sampled=True,
            )
        self.last_sent = command.copy()
        return HandScheduleDecision(
            command=command,
            should_send=True,
            reason="changed_5hz_sample",
            intent_id=self.intent_id,
            intent_changed=intent_changed,
            sampled=True,
        )

    def _decision(
        self,
        should_send: bool,
        reason: str,
        intent_changed: bool,
        sampled: bool,
    ) -> HandScheduleDecision:
        return HandScheduleDecision(
            command=self.state.command.copy(),
            should_send=should_send,
            reason=reason,
            intent_id=self.intent_id,
            intent_changed=intent_changed,
            sampled=sampled,
        )

    def _first_legal_sample_at(self, now: float) -> float:
        if self.last_sample_at is None:
            return now
        return max(now, self.last_sample_at + self.period_s)

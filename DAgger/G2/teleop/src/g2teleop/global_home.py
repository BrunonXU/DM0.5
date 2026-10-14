"""Pure same-owner trajectory state for the approved G2 global Home."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class GlobalHomeStep:
    arms: np.ndarray
    hands: np.ndarray
    send_hands: bool
    phase: str
    done: bool


class GlobalHomePlan:
    """Move arms first at 100 Hz, then submit bounded hands at 5 Hz."""

    def __init__(
        self,
        start_arms: np.ndarray,
        target_arms: np.ndarray,
        start_hands: np.ndarray,
        target_hands: np.ndarray,
        *,
        started_at: float,
        arm_duration_s: float = 5.0,
        hand_hz: float = 5.0,
        hand_max_step_rad: float = 0.03,
        hand_max_intervals: int = 8,
    ) -> None:
        self.start_arms = self._vector(start_arms, 14, "start arms")
        self.target_arms = self._vector(target_arms, 14, "target arms")
        self.start_hands = self._vector(start_hands, 20, "start hands")
        self.target_hands = self._vector(target_hands, 20, "target hands")
        if not math.isfinite(started_at):
            raise ValueError("Home start time must be finite")
        if arm_duration_s <= 0.0 or hand_hz <= 0.0 or hand_max_step_rad <= 0.0:
            raise ValueError("Home timing and hand step must be positive")
        if hand_max_intervals < 1:
            raise ValueError("Home hand interval cap must be positive")
        self.started_at = float(started_at)
        self.arm_duration_s = float(arm_duration_s)
        self.hand_period_s = 1.0 / float(hand_hz)
        maximum = float(np.max(np.abs(self.target_hands - self.start_hands)))
        intervals = max(1, int(math.ceil(maximum / hand_max_step_rad)))
        intervals = min(intervals, int(hand_max_intervals))
        alpha = np.linspace(0.0, 1.0, intervals + 1)[:, None]
        self.hand_trajectory = self.start_hands[None, :] + alpha * (
            self.target_hands - self.start_hands
        )[None, :]
        self.hand_index = -1
        self.next_hand_at = self.started_at + self.arm_duration_s

    @staticmethod
    def _vector(values: np.ndarray, size: int, label: str) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        if array.shape != (size,) or not np.all(np.isfinite(array)):
            raise ValueError(f"{label} must be finite {size}D")
        return array.copy()

    def step(self, now: float) -> GlobalHomeStep:
        if not math.isfinite(now) or now < self.started_at:
            raise ValueError("Home step time must be finite and monotonic")
        elapsed = now - self.started_at
        if elapsed < self.arm_duration_s:
            alpha = elapsed / self.arm_duration_s
            arms = self.start_arms + alpha * (self.target_arms - self.start_arms)
            return GlobalHomeStep(
                arms, self.start_hands.copy(), False, "arms_to_home", False
            )

        send_hands = False
        if self.hand_index + 1 < len(self.hand_trajectory) and now >= self.next_hand_at:
            self.hand_index += 1
            self.next_hand_at = now + self.hand_period_s
            send_hands = True
        hands = (
            self.start_hands
            if self.hand_index < 0
            else self.hand_trajectory[self.hand_index]
        )
        done = self.hand_index == len(self.hand_trajectory) - 1
        phase = (
            "hand_anchor"
            if self.hand_index == 0
            else "hand_to_home" if not done else "complete"
        )
        return GlobalHomeStep(
            self.target_arms.copy(), hands.copy(), send_hands, phase, done
        )

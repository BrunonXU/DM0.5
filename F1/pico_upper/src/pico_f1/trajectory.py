"""Online Ruckig jerk-limited joint trajectory generation.

All values use degrees and seconds because the F1 ROS joint command contract is
degree-based.  A proposal is committed only after CollisionGuard accepts it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TrajectoryProposal:
    position: tuple[float, ...]
    velocity: tuple[float, ...]
    acceleration: tuple[float, ...]
    filtered_target: tuple[float, ...]


class JerkLimitedTrajectory:
    """Causal low-pass target filter plus velocity/acceleration/jerk limits."""

    def __init__(
        self,
        size: int,
        dt: float,
        max_velocity: float,
        max_acceleration: float,
        max_jerk: float,
        target_cutoff_hz: float,
        deadband: float,
    ) -> None:
        values = (dt, max_velocity, max_acceleration, max_jerk, target_cutoff_hz)
        if size <= 0 or any(not math.isfinite(v) or v <= 0.0 for v in values):
            raise ValueError("trajectory size and limits must be positive and finite")
        if not math.isfinite(deadband) or deadband < 0.0:
            raise ValueError("deadband must be finite and non-negative")
        self.size = size
        self.dt = dt
        self.max_velocity = max_velocity
        self.max_acceleration = max_acceleration
        self.max_jerk = max_jerk
        self.deadband = deadband
        self.alpha = 1.0 - math.exp(-2.0 * math.pi * target_cutoff_hz * dt)
        self.target_cutoff_hz = target_cutoff_hz
        try:
            import ruckig
        except ImportError as exc:
            raise RuntimeError("ruckig is required for online trajectory planning") from exc
        self._ruckig_type = ruckig.Ruckig
        self._input_type = ruckig.InputParameter
        self._output_type = ruckig.OutputParameter
        self._independent_sync = ruckig.Synchronization.No
        self.recovery_events = 0
        self.position = [0.0] * size
        self.velocity = [0.0] * size
        self.acceleration = [0.0] * size
        self.filtered_target = [0.0] * size
        self.initialized = False

    def _vector(self, values: Iterable[float], name: str) -> list[float]:
        vector = [float(value) for value in values]
        if len(vector) != self.size or any(not math.isfinite(v) for v in vector):
            raise ValueError(f"{name} must contain {self.size} finite values")
        return vector


    def reset(self, position: Iterable[float]) -> None:
        position_vector = self._vector(position, "position")
        self.position = position_vector.copy()
        self.velocity = [0.0] * self.size
        self.acceleration = [0.0] * self.size
        self.filtered_target = position_vector.copy()
        self.initialized = True

    def propose(self, target: Iterable[float], dt: float | None = None) -> TrajectoryProposal:
        target_vector = self._vector(target, "target")
        step_dt = self.dt if dt is None else float(dt)
        if not math.isfinite(step_dt) or not 0.0 < step_dt <= .1:
            raise ValueError('trajectory step must be finite and within (0, 0.1] seconds')
        alpha = 1.0 - math.exp(-2.0 * math.pi * self.target_cutoff_hz * step_dt)
        if not self.initialized:
            self.reset(target_vector)

        next_target: list[float] = []
        for q, v, filtered, raw_target in zip(
            self.position,
            self.velocity,
            self.filtered_target,
            target_vector,
        ):
            filtered += alpha * (raw_target - filtered)
            error = filtered - q
            if abs(error) <= self.deadband and abs(v) <= self.deadband / self.dt:
                filtered = q
            next_target.append(filtered)


        trajectory_input = self._input_type(self.size)
        trajectory_output = self._output_type(self.size)
        trajectory_input.current_position = self.position
        trajectory_input.current_velocity = self.velocity
        trajectory_input.current_acceleration = self.acceleration
        trajectory_input.target_position = next_target
        trajectory_input.target_velocity = [0.0] * self.size
        trajectory_input.target_acceleration = [0.0] * self.size
        trajectory_input.max_velocity = [self.max_velocity] * self.size
        trajectory_input.max_acceleration = [self.max_acceleration] * self.size
        trajectory_input.max_jerk = [self.max_jerk] * self.size
        # Constructing a planner per proposal guarantees that no cached
        # trajectory is advanced before CollisionGuard commits the proposal.
        # The 14-DoF planning cost is measured in the offline benchmark and
        # again during Shadow qualification.
        try:
            result = self._ruckig_type(self.size, step_dt).update(
                trajectory_input, trajectory_output
            )
            if int(result) < 0:
                raise RuntimeError(f'Ruckig result={result}')
        except Exception:
            # Retry a synchronization failure without inventing a stationary
            # robot. If independent planning also fails, let the safety caller
            # hold; never conceal an abrupt zero-velocity reset as smooth.
            self.recovery_events += 1
            if self.recovery_events == 1 or self.recovery_events % 100 == 0:
                print(f'TRAJECTORY_SYNC_RETRY count={self.recovery_events} preserve_velocity_acceleration=true')
            trajectory_input.synchronization = self._independent_sync
            trajectory_output = self._output_type(self.size)
            result = self._ruckig_type(self.size, step_dt).update(
                trajectory_input, trajectory_output
            )
        if int(result) < 0:
            raise RuntimeError(f"Ruckig failed to generate trajectory: {result}")

        return TrajectoryProposal(
            tuple(trajectory_output.new_position),
            tuple(trajectory_output.new_velocity),
            tuple(trajectory_output.new_acceleration),
            tuple(next_target),
        )

    def commit(
        self,
        proposal: TrajectoryProposal,
        accepted: Sequence[bool] | None = None,
    ) -> None:
        mask = [True] * self.size if accepted is None else list(accepted)
        if len(mask) != self.size:
            raise ValueError("accepted mask has the wrong size")
        for index, is_accepted in enumerate(mask):
            if is_accepted:
                self.position[index] = proposal.position[index]
                self.velocity[index] = proposal.velocity[index]
                self.acceleration[index] = proposal.acceleration[index]
                self.filtered_target[index] = proposal.filtered_target[index]
            else:
                self.velocity[index] = 0.0
                self.acceleration[index] = 0.0

    def hold(self) -> None:
        self.velocity = [0.0] * self.size
        self.acceleration = [0.0] * self.size
        self.filtered_target = self.position.copy()

    def hold_axes(self, enabled: Sequence[bool]) -> None:
        """Drop pending motion on unclutched axes without disturbing the others."""
        if len(enabled) != self.size:
            raise ValueError("enabled mask has the wrong size")
        for i, active in enumerate(enabled):
            if not active:
                self.velocity[i] = 0.0
                self.acceleration[i] = 0.0
                self.filtered_target[i] = self.position[i]

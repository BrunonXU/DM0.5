"""Ruckig-backed joint-velocity governor with no robot or GDK dependency."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

import numpy as np


class OTGError(RuntimeError):
    """Raised before robot output when the OTG input or result is unsafe."""


@dataclass(frozen=True)
class OTGSample:
    position: np.ndarray
    velocity: np.ndarray
    acceleration: np.ndarray
    jerk: np.ndarray
    target_velocity: np.ndarray
    result: str
    calculation_duration_us: float


def ruckig_version() -> str:
    """Import Ruckig eagerly for powered-startup dependency validation."""
    try:
        import ruckig
    except ImportError as exc:
        raise OTGError(
            "Ruckig OTG requested but Python package 'ruckig' is unavailable"
        ) from exc
    return str(getattr(ruckig, "__version__", "unknown"))


def _vector(values: Sequence[float], dof: int, label: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (dof,) or not np.isfinite(array).all():
        raise OTGError(f"{label} must be finite shape ({dof},)")
    return array


class RuckigVelocityOTG:
    """Stateful velocity-interface OTG that emits one safe position per tick.

    Ruckig owns the authoritative ``q/dq/ddq`` command state between calls.
    Measured position is used only at an explicit takeover/reset; feeding a
    slower feedback stream into every 100 Hz update would create state jumps.
    """

    _SYNCHRONIZATION_NAMES = {
        "none": "No",
        "phase": "Phase",
        "time": "Time",
        "time_if_necessary": "TimeIfNecessary",
    }

    def __init__(
        self,
        dof: int,
        control_period_s: float,
        *,
        max_velocity_rad_s: float | Sequence[float],
        max_acceleration_rad_s2: float | Sequence[float],
        max_jerk_rad_s3: float | Sequence[float],
        lower_position_rad: Sequence[float],
        upper_position_rad: Sequence[float],
        synchronization: str = "phase",
        ruckig_module: Any | None = None,
    ) -> None:
        if dof <= 0:
            raise OTGError("dof must be positive")
        if not math.isfinite(control_period_s) or control_period_s <= 0.0:
            raise OTGError("control period must be finite and positive")
        if synchronization not in self._SYNCHRONIZATION_NAMES:
            raise OTGError(f"unsupported Ruckig synchronization: {synchronization}")
        if ruckig_module is None:
            try:
                import ruckig as ruckig_module
            except ImportError as exc:
                raise OTGError("Ruckig Python package is unavailable") from exc

        self.dof = dof
        self.control_period_s = float(control_period_s)
        self.max_velocity = self._limit_vector(max_velocity_rad_s, "max_velocity")
        self.max_acceleration = self._limit_vector(
            max_acceleration_rad_s2, "max_acceleration"
        )
        self.max_jerk = self._limit_vector(max_jerk_rad_s3, "max_jerk")
        self.lower_position = _vector(lower_position_rad, dof, "lower_position")
        self.upper_position = _vector(upper_position_rad, dof, "upper_position")
        if np.any(self.lower_position >= self.upper_position):
            raise OTGError("every lower position limit must be below its upper limit")

        self.synchronization = synchronization
        self._ruckig_module = ruckig_module
        self._otg = ruckig_module.Ruckig(dof, self.control_period_s)
        self._input = ruckig_module.InputParameter(dof)
        self._output = ruckig_module.OutputParameter(dof)
        self._input.control_interface = ruckig_module.ControlInterface.Velocity
        sync_enum = self._SYNCHRONIZATION_NAMES[synchronization]
        self._input.synchronization = getattr(
            ruckig_module.Synchronization, sync_enum
        )
        self._input.max_velocity = self.max_velocity.tolist()
        self._input.max_acceleration = self.max_acceleration.tolist()
        self._input.max_jerk = self.max_jerk.tolist()
        self._input.target_acceleration = np.zeros(dof).tolist()
        self.initialized = False
        self.last_sample: OTGSample | None = None

    def _limit_vector(
        self, values: float | Sequence[float], label: str
    ) -> np.ndarray:
        if np.isscalar(values):
            array = np.full(self.dof, float(values), dtype=np.float64)
        else:
            array = _vector(values, self.dof, label)
        if not np.isfinite(array).all() or np.any(array <= 0.0):
            raise OTGError(f"{label} must be finite and strictly positive")
        return array

    def reset(
        self,
        position: Sequence[float],
        velocity: Sequence[float] | None = None,
        acceleration: Sequence[float] | None = None,
    ) -> None:
        q = _vector(position, self.dof, "reset position")
        dq = (
            np.zeros(self.dof)
            if velocity is None
            else _vector(velocity, self.dof, "reset velocity")
        )
        ddq = (
            np.zeros(self.dof)
            if acceleration is None
            else _vector(acceleration, self.dof, "reset acceleration")
        )
        self._validate_state(q, dq, ddq, np.zeros(self.dof), "reset")
        self._input.current_position = q.tolist()
        self._input.current_velocity = dq.tolist()
        self._input.current_acceleration = ddq.tolist()
        self._input.target_velocity = dq.tolist()
        self._input.target_acceleration = np.zeros(self.dof).tolist()
        self.initialized = True
        self.last_sample = OTGSample(
            position=q.copy(),
            velocity=dq.copy(),
            acceleration=ddq.copy(),
            jerk=np.zeros(self.dof),
            target_velocity=dq.copy(),
            result="reset",
            calculation_duration_us=0.0,
        )

    def emergency_hold(self, position: Sequence[float]) -> None:
        """Reset to a stationary state for a safety HOLD; never coast."""
        self.reset(position, np.zeros(self.dof), np.zeros(self.dof))
        q = _vector(position, self.dof, "safety hold position")
        self.last_sample = OTGSample(
            position=q.copy(),
            velocity=np.zeros(self.dof),
            acceleration=np.zeros(self.dof),
            jerk=np.zeros(self.dof),
            target_velocity=np.zeros(self.dof),
            result="safety_hold",
            calculation_duration_us=0.0,
        )

    def _validate_state(
        self,
        q: np.ndarray,
        dq: np.ndarray,
        ddq: np.ndarray,
        jerk: np.ndarray,
        label: str,
    ) -> None:
        tolerance = 1e-8
        if np.any(q < self.lower_position - tolerance) or np.any(
            q > self.upper_position + tolerance
        ):
            raise OTGError(f"{label} position violates the Pink model limits")
        if np.any(np.abs(dq) > self.max_velocity + 1e-6):
            raise OTGError(f"{label} velocity violates the project limit")
        if np.any(np.abs(ddq) > self.max_acceleration + 1e-6):
            raise OTGError(f"{label} acceleration violates the OTG limit")
        if np.any(np.abs(jerk) > self.max_jerk + 1e-5):
            raise OTGError(f"{label} jerk violates the OTG limit")

    def step(self, target_velocity_rad_s: Sequence[float]) -> OTGSample:
        if not self.initialized:
            raise OTGError("OTG must be reset from measured q before step")
        target = _vector(target_velocity_rad_s, self.dof, "target velocity")
        if np.any(np.abs(target) > self.max_velocity + 1e-6):
            raise OTGError("target velocity violates the project limit")
        target = np.clip(target, -self.max_velocity, self.max_velocity)
        self._input.target_velocity = target.tolist()
        self._input.target_acceleration = np.zeros(self.dof).tolist()

        try:
            result = self._otg.update(self._input, self._output)
        except Exception as exc:
            raise OTGError(f"Ruckig update failed: {type(exc).__name__}: {exc}") from exc
        allowed = {
            self._ruckig_module.Result.Working,
            self._ruckig_module.Result.Finished,
        }
        if result not in allowed:
            raise OTGError(f"Ruckig returned {result}")

        q = _vector(self._output.new_position, self.dof, "Ruckig position")
        dq = _vector(self._output.new_velocity, self.dof, "Ruckig velocity")
        ddq = _vector(self._output.new_acceleration, self.dof, "Ruckig acceleration")
        jerk = _vector(self._output.new_jerk, self.dof, "Ruckig jerk")
        self._validate_state(q, dq, ddq, jerk, "Ruckig output")
        calculation_duration = float(self._output.calculation_duration)
        if not math.isfinite(calculation_duration) or calculation_duration < 0.0:
            raise OTGError("Ruckig calculation duration is invalid")

        sample = OTGSample(
            position=q.copy(),
            velocity=dq.copy(),
            acceleration=ddq.copy(),
            jerk=jerk.copy(),
            target_velocity=target.copy(),
            result="working" if result == self._ruckig_module.Result.Working else "finished",
            calculation_duration_us=calculation_duration,
        )
        # Commit only after every safety check passes.  If validation fails,
        # the previous authoritative state remains untouched.
        self._output.pass_to_input(self._input)
        self.last_sample = sample
        return sample

    def diagnostics(self) -> dict[str, object]:
        sample = self.last_sample
        velocity_utilization = None
        acceleration_utilization = None
        jerk_utilization = None
        reversing = None
        decelerating = None
        lower_margin = None
        upper_margin = None
        if sample is not None:
            velocity_utilization = np.abs(sample.velocity) / self.max_velocity
            acceleration_utilization = np.abs(sample.acceleration) / self.max_acceleration
            jerk_utilization = np.abs(sample.jerk) / self.max_jerk
            reversing = (sample.target_velocity * sample.velocity < 0.0).tolist()
            decelerating = (sample.acceleration * sample.velocity < 0.0).tolist()
            lower_margin = (sample.position - self.lower_position).tolist()
            upper_margin = (self.upper_position - sample.position).tolist()
        return {
            "otg_enabled": True,
            "otg_backend": "ruckig",
            "otg_synchronization": self.synchronization,
            "otg_status": None if sample is None else sample.result,
            "otg_target_dq_rad_s": (
                None if sample is None else sample.target_velocity.tolist()
            ),
            "otg_q_cmd_rad": None if sample is None else sample.position.tolist(),
            "otg_dq_cmd_rad_s": None if sample is None else sample.velocity.tolist(),
            "otg_ddq_cmd_rad_s2": (
                None if sample is None else sample.acceleration.tolist()
            ),
            "otg_jerk_cmd_rad_s3": None if sample is None else sample.jerk.tolist(),
            "otg_calculation_duration_us": (
                None if sample is None else sample.calculation_duration_us
            ),
            "otg_velocity_error_rad_s": (
                None
                if sample is None
                else (sample.target_velocity - sample.velocity).tolist()
            ),
            "otg_velocity_limit_utilization": (
                None if velocity_utilization is None else velocity_utilization.tolist()
            ),
            "otg_acceleration_limit_utilization": (
                None
                if acceleration_utilization is None
                else acceleration_utilization.tolist()
            ),
            "otg_jerk_limit_utilization": (
                None if jerk_utilization is None else jerk_utilization.tolist()
            ),
            "otg_reversing_dofs": reversing,
            "otg_decelerating_dofs": decelerating,
            "otg_joint_lower_margin_rad": lower_margin,
            "otg_joint_upper_margin_rad": upper_margin,
        }

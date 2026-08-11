"""所有G2策略后端共享的稳定数据合同。

用途：冻结Observation、ActionChunk、异常类型以及34D/50步/30Hz/100Hz常量。
输入：三路RGB HWC uint8、34D finite state、固定prompt和单调时间/request ID。
输出：验证并连续存储的观测和严格50×34 finite absolute-action chunk。
方法：dataclass在构造时执行shape、dtype、有限性、时间顺序和prompt校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import numpy as np

from g2_sort.config.dm05_contract import ACTION_DIM, PROMPT

CHUNK_STEPS: Final[int] = 50
MODEL_HZ: Final[float] = 30.0
SERVO_HZ: Final[float] = 100.0
IMAGE_NAMES: Final[tuple[str, str, str]] = (
    "head",
    "left_wrist",
    "right_wrist",
)


def _validated_timing(
    values: dict[str, float],
    context: str,
) -> dict[str, float]:
    if not isinstance(values, dict):
        raise ContractError(f"{context} timing must be a dictionary")
    timing: dict[str, float] = {}
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = float(raw_value)
        if not key or not np.isfinite(value) or value < 0:
            raise ContractError(
                f"{context} timing contains invalid entry {raw_key!r}"
            )
        timing[key] = value
    return timing


class G2RuntimeError(RuntimeError):
    """Base error for the decoupled G2 runtime."""


class ContractError(G2RuntimeError):
    """An observation or action violated the frozen DM0.5 contract."""


class ObservationError(G2RuntimeError):
    """A camera or measured-state observation was unusable."""


class PolicyError(G2RuntimeError):
    """The policy request failed or returned an invalid response."""


class RobotControlError(G2RuntimeError):
    """The local G2 control path failed."""


def _validated_image(image: np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(image)
    if array.dtype != np.uint8:
        raise ContractError(f"{name} image must be uint8, got {array.dtype}")
    if array.ndim != 3 or array.shape[2] != 3 or min(array.shape[:2]) <= 0:
        raise ContractError(
            f"{name} image must be non-empty RGB HWC, got shape={array.shape}"
        )
    return np.ascontiguousarray(array)


@dataclass(frozen=True)
class Observation:
    """One timestamped DM0.5 input captured on the G2 domain controller."""

    request_id: int
    captured_at: float
    images: tuple[np.ndarray, np.ndarray, np.ndarray]
    state: np.ndarray
    prompt: str = PROMPT
    timing_seconds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.request_id < 0:
            raise ContractError("request_id must be non-negative")
        if not np.isfinite(self.captured_at):
            raise ContractError("captured_at must be finite monotonic time")
        if len(self.images) != len(IMAGE_NAMES):
            raise ContractError(
                f"expected {len(IMAGE_NAMES)} images, got {len(self.images)}"
            )
        images = tuple(
            _validated_image(image, name)
            for image, name in zip(self.images, IMAGE_NAMES, strict=True)
        )
        state = np.asarray(self.state, dtype=np.float32)
        if state.shape != (ACTION_DIM,):
            raise ContractError(
                f"measured state must be shape ({ACTION_DIM},), got {state.shape}"
            )
        if not np.all(np.isfinite(state)):
            raise ContractError("measured state contains NaN or Inf")
        if self.prompt != PROMPT:
            raise ContractError("DM0.5 runtime prompt must equal g2_contract.PROMPT")
        timing = _validated_timing(self.timing_seconds, "observation")
        object.__setattr__(self, "images", images)
        object.__setattr__(self, "state", np.ascontiguousarray(state))
        object.__setattr__(self, "timing_seconds", timing)

    @property
    def head_rgb(self) -> np.ndarray:
        return self.images[0]

    @property
    def left_wrist_rgb(self) -> np.ndarray:
        return self.images[1]

    @property
    def right_wrist_rgb(self) -> np.ndarray:
        return self.images[2]


@dataclass(frozen=True)
class ActionChunk:
    """A validated absolute joint-position chunk returned by a policy backend."""

    request_id: int
    observation_at: float
    request_started_at: float
    response_received_at: float
    actions: np.ndarray
    timing_seconds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.request_id < 0:
            raise ContractError("request_id must be non-negative")
        times = (
            self.observation_at,
            self.request_started_at,
            self.response_received_at,
        )
        if not all(np.isfinite(value) for value in times):
            raise ContractError("action timestamps must all be finite")
        if self.request_started_at < self.observation_at:
            raise ContractError("request started before its observation was captured")
        if self.response_received_at < self.request_started_at:
            raise ContractError("response time precedes request start")
        actions = np.asarray(self.actions, dtype=np.float64)
        expected = (CHUNK_STEPS, ACTION_DIM)
        if actions.shape != expected:
            raise ContractError(
                f"DM0.5 response must be exactly {expected}, got {actions.shape}"
            )
        if not np.all(np.isfinite(actions)):
            raise ContractError("DM0.5 response contains NaN or Inf")
        timing = _validated_timing(self.timing_seconds, "action chunk")
        object.__setattr__(self, "actions", np.ascontiguousarray(actions))
        object.__setattr__(self, "timing_seconds", timing)

    @property
    def latency_seconds(self) -> float:
        return self.response_received_at - self.request_started_at

    def stale_prefix_steps(self, now: float) -> int:
        """Conservatively convert observation age into stale 30 Hz intervals."""
        age = max(0.0, float(now) - self.observation_at)
        # Subtract only a scale-aware floating-point tolerance.  Without it,
        # exact grid values such as 1.3 * 30 can become 39.00000000000001 and
        # incorrectly discard one extra interval.
        scaled = age * MODEL_HZ
        tolerance = np.finfo(np.float64).eps * max(1.0, abs(scaled)) * 8.0
        return min(CHUNK_STEPS, int(np.ceil(scaled - tolerance)))

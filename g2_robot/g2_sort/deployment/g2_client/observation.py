"""三路图像和严格state的Observation装配器。

用途：把相机与机器人状态源组合成策略后端唯一接受的观测对象。
输入：实现``capture``的相机源、实现``read_state``的状态源和monotonic时钟。
输出：带递增request ID、采集起始时间、固定prompt的验证后Observation。
方法：先记采集时间，再读取三图和34D state；任一失败整轮无效且不补默认值。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np

from g2_sort.config.dm05_contract import PROMPT

from .contracts import Observation, ObservationError


class CameraSource(Protocol):
    def capture(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...


class StateSource(Protocol):
    def read_state(self) -> np.ndarray: ...


class ObservationSource:
    """Capture three images and strict measured state as one logical sample."""

    def __init__(
        self,
        cameras: CameraSource,
        state_source: StateSource,
        *,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._cameras = cameras
        self._state_source = state_source
        self._clock = clock
        self._timer = timer
        self._next_request_id = 0
        self._lock = threading.Lock()

    def capture(self) -> Observation:
        captured_at = self._clock()
        capture_started = self._timer()
        try:
            images = self._cameras.capture()
            images_finished = self._timer()
            state = self._state_source.read_state()
            state_finished = self._timer()
        except ObservationError:
            raise
        except Exception as exc:
            raise ObservationError(f"observation capture failed: {exc}") from exc
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
        return Observation(
            request_id=request_id,
            captured_at=captured_at,
            images=images,
            state=state,
            prompt=PROMPT,
            timing_seconds={
                "client_camera_capture_seconds": max(
                    0.0, images_finished - capture_started
                ),
                "client_state_read_seconds": max(
                    0.0, state_finished - images_finished
                ),
                "client_observation_capture_seconds": max(
                    0.0, state_finished - capture_started
                ),
            },
        )

    def read_state(self) -> np.ndarray:
        """Re-read strict measured state for the powered execution gate."""
        return self._state_source.read_state()

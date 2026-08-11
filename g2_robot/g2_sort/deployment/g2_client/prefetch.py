"""异步模式使用的single-flight策略请求器。

用途：把HTTP推理移出100Hz控制线程，同时强制服务器最多一个请求在途。
输入：一个Observation和发起请求时仍在执行的playback ID。
输出：包含ActionChunk或错误文本的``PrefetchResult``。
方法：每次请求使用一个daemon worker；结果消费前拒绝再次submit，便于判定迟到。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .contracts import ActionChunk, Observation
from .policy import PolicyBackend


@dataclass(frozen=True)
class PrefetchResult:
    observation: Observation
    playback_id: int
    chunk: ActionChunk | None
    error: str | None


class SingleFlightPrefetcher:
    """Run at most one backend inference and retain at most one result."""

    def __init__(self, backend: PolicyBackend) -> None:
        self._backend = backend
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._inflight = False
        self._result: PrefetchResult | None = None

    def submit(self, observation: Observation, playback_id: int) -> bool:
        with self._lock:
            if self._inflight or self._result is not None:
                return False
            self._inflight = True
            self._thread = threading.Thread(
                target=self._worker,
                args=(observation, playback_id),
                name=f"dm05-prefetch-{observation.request_id}",
                daemon=True,
            )
            self._thread.start()
            return True

    def _worker(self, observation: Observation, playback_id: int) -> None:
        chunk = None
        error = None
        try:
            chunk = self._backend.infer(observation)
        except Exception as exc:
            error = str(exc)
        with self._lock:
            self._result = PrefetchResult(
                observation=observation,
                playback_id=playback_id,
                chunk=chunk,
                error=error,
            )
            self._inflight = False

    def take_ready(self) -> PrefetchResult | None:
        with self._lock:
            result = self._result
            self._result = None
            return result

    def has_inflight(self) -> bool:
        with self._lock:
            return self._inflight

    def is_idle(self) -> bool:
        with self._lock:
            return not self._inflight and self._result is None

    def close(self, timeout: float = 1.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))

"""DM0.5的multipart HTTP策略后端。

用途：把稳定Observation转换为服务器``/process_frame``协议并严格解析响应。
输入：按头/左腕/右腕排序的RGB图、34D state、固定prompt和HTTP endpoint。
输出：带request/观测/请求/响应时间的严格50×34 finite ``ActionChunk``。
方法：Pillow编码JPEG，三个同名image字段顺序上传；锁保证最多一个请求在途。
"""

from __future__ import annotations

import json
import numbers
import threading
import time
from io import BytesIO
from collections.abc import Callable
from typing import Any

import numpy as np

from .contracts import (
    ACTION_DIM,
    CHUNK_STEPS,
    ActionChunk,
    ContractError,
    IMAGE_NAMES,
    Observation,
    PolicyError,
)

ImageEncoder = Callable[[np.ndarray], bytes]


def _strict_response_array(value: Any) -> np.ndarray:
    if not isinstance(value, list) or len(value) != CHUNK_STEPS:
        raise ContractError(
            f"response must contain exactly {CHUNK_STEPS} action rows"
        )
    for row_index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != ACTION_DIM:
            raise ContractError(
                f"response row {row_index} must contain exactly {ACTION_DIM} values"
            )
        for column_index, item in enumerate(row):
            if (
                isinstance(item, bool)
                or not isinstance(item, numbers.Real)
                or not np.isfinite(float(item))
            ):
                raise ContractError(
                    "response action values must be finite JSON numbers; "
                    f"invalid value at [{row_index}][{column_index}]"
                )
    return np.asarray(value, dtype=np.float64)


def encode_rgb_jpeg(image: np.ndarray) -> bytes:
    """Encode one validated RGB image without resizing it."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise PolicyError("Pillow is required to encode G2 camera frames") from exc
    output = BytesIO()
    try:
        Image.fromarray(image, mode="RGB").save(
            output, format="JPEG", quality=95
        )
    except Exception as exc:
        raise PolicyError(f"Pillow failed to JPEG-encode an RGB frame: {exc}") from exc
    return output.getvalue()


class DM05HTTPBackend:
    """Serialize the frozen three-image/34D/prompt DM0.5 HTTP contract."""

    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 10.0,
        *,
        session: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[], float] = time.perf_counter,
        image_encoder: ImageEncoder = encode_rgb_jpeg,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("policy endpoint must start with http:// or https://")
        if timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - runtime dependency
                raise PolicyError("requests is required for the DM0.5 HTTP backend") from exc
            session = requests.Session()
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        self._session = session
        self._clock = clock
        self._timer = timer
        self._image_encoder = image_encoder
        self._inflight_lock = threading.Lock()

    def infer(self, observation: Observation) -> ActionChunk:
        if not self._inflight_lock.acquire(blocking=False):
            raise PolicyError("DM0.5 backend already has a request in flight")
        try:
            request_started_at = self._clock()
            backend_started = self._timer()
            try:
                encode_started = self._timer()
                files = [
                    (
                        "image",
                        (
                            f"{name}.jpg",
                            self._image_encoder(image),
                            "image/jpeg",
                        ),
                    )
                    for name, image in zip(
                        IMAGE_NAMES, observation.images, strict=True
                    )
                ]
                encode_finished = self._timer()
                payload_started = self._timer()
                data = {
                    "states": json.dumps(
                        observation.state.astype(float).tolist(),
                        separators=(",", ":"),
                    ),
                    "text": observation.prompt,
                }
                payload_finished = self._timer()
                http_started = self._timer()
                response = self._session.post(
                    self.endpoint,
                    files=files,
                    data=data,
                    timeout=self.timeout_seconds,
                )
                http_finished = self._timer()
                response.raise_for_status()
                json_started = self._timer()
                payload = response.json()
                json_finished = self._timer()
            except Exception as exc:
                raise PolicyError(f"DM0.5 HTTP request failed: {exc}") from exc
            response_received_at = self._clock()

            if not isinstance(payload, dict) or "response" not in payload:
                raise PolicyError("DM0.5 response JSON must contain key 'response'")
            try:
                validation_started = self._timer()
                actions = _strict_response_array(payload["response"])
                validation_finished = self._timer()
                timing = dict(observation.timing_seconds)
                timing.update(
                    {
                        "client_jpeg_encode_seconds": max(
                            0.0, encode_finished - encode_started
                        ),
                        "client_request_payload_seconds": max(
                            0.0, payload_finished - payload_started
                        ),
                        "client_http_post_seconds": max(
                            0.0, http_finished - http_started
                        ),
                        "client_response_json_seconds": max(
                            0.0, json_finished - json_started
                        ),
                        "client_response_validation_seconds": max(
                            0.0, validation_finished - validation_started
                        ),
                        "client_backend_total_seconds": max(
                            0.0, validation_finished - backend_started
                        ),
                        "client_observation_to_backend_seconds": max(
                            0.0,
                            request_started_at - observation.captured_at,
                        ),
                    }
                )
                observation_capture = timing.get(
                    "client_observation_capture_seconds", 0.0
                )
                timing["client_capture_to_backend_handoff_seconds"] = max(
                    0.0,
                    timing["client_observation_to_backend_seconds"]
                    - observation_capture,
                )
                self._add_server_timing(timing, payload, response)
                return ActionChunk(
                    request_id=observation.request_id,
                    observation_at=observation.captured_at,
                    request_started_at=request_started_at,
                    response_received_at=response_received_at,
                    actions=actions,
                    timing_seconds=timing,
                )
            except (ContractError, TypeError, ValueError) as exc:
                raise PolicyError(f"invalid DM0.5 response contract: {exc}") from exc
        finally:
            self._inflight_lock.release()

    @staticmethod
    def _add_server_timing(
        timing: dict[str, float],
        payload: dict[str, Any],
        response: Any,
    ) -> None:
        """Best-effort timing parsing must never invalidate a valid action."""
        raw_server = payload.get("timing_seconds")
        if isinstance(raw_server, dict):
            for raw_key, raw_value in raw_server.items():
                try:
                    key = str(raw_key)
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if key and np.isfinite(value) and value >= 0:
                    timing[key] = value
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                handler_seconds = float(
                    headers.get("X-DM05-Handler-Seconds", "nan")
                )
            except (TypeError, ValueError):
                handler_seconds = float("nan")
            if np.isfinite(handler_seconds) and handler_seconds >= 0:
                timing["server_handler_seconds"] = handler_seconds
        server_total = timing.get("server_handler_seconds")
        if server_total is None:
            server_total = timing.get("server_total_before_jsonify_seconds")
        if server_total is not None:
            timing["client_http_unattributed_seconds"] = max(
                0.0,
                timing["client_http_post_seconds"] - server_total,
            )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

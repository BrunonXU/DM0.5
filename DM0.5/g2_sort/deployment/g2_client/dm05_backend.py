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
        self._image_encoder = image_encoder
        self._inflight_lock = threading.Lock()

    def infer(self, observation: Observation) -> ActionChunk:
        if not self._inflight_lock.acquire(blocking=False):
            raise PolicyError("DM0.5 backend already has a request in flight")
        try:
            request_started_at = self._clock()
            try:
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
                data = {
                    "states": json.dumps(
                        observation.state.astype(float).tolist(),
                        separators=(",", ":"),
                    ),
                    "text": observation.prompt,
                }
                response = self._session.post(
                    self.endpoint,
                    files=files,
                    data=data,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise PolicyError(f"DM0.5 HTTP request failed: {exc}") from exc
            response_received_at = self._clock()

            if not isinstance(payload, dict) or "response" not in payload:
                raise PolicyError("DM0.5 response JSON must contain key 'response'")
            try:
                return ActionChunk(
                    request_id=observation.request_id,
                    observation_at=observation.captured_at,
                    request_started_at=request_started_at,
                    response_received_at=response_received_at,
                    actions=_strict_response_array(payload["response"]),
                )
            except (ContractError, TypeError, ValueError) as exc:
                raise PolicyError(f"invalid DM0.5 response contract: {exc}") from exc
        finally:
            self._inflight_lock.release()

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

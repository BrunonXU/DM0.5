"""Server-side copy of the versioned DM0.5 binary wire contract."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

import numpy as np

ACTION_DIM = 34
CHUNK_STEPS = 50
REQUEST_MAGIC = b"DM05RQ1\0"
RESPONSE_MAGIC = b"DM05RS1\0"
RTC_FLAG = 1
_REQUEST_HEADER = struct.Struct("<8sQHHHH")
_IMAGE_HEADER = struct.Struct("<BIII")
_RESPONSE_HEADER = struct.Struct("<8sQB3xI")
_MEDIA_TO_CODE = {"image/jpeg": 1, "image/png": 2}
_CODE_TO_MEDIA = {value: key for key, value in _MEDIA_TO_CODE.items()}
_ACTION_BYTES = CHUNK_STEPS * ACTION_DIM * np.dtype("<f4").itemsize


class BinaryProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class EncodedImage:
    data: bytes
    media_type: str
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        payload = bytes(self.data)
        if self.media_type not in _MEDIA_TO_CODE:
            raise BinaryProtocolError("unsupported image media type")
        if self.width < 0 or self.height < 0 or not payload:
            raise BinaryProtocolError("invalid compressed image dimensions/payload")
        if self.media_type == "image/jpeg" and not payload.startswith(b"\xff\xd8"):
            raise BinaryProtocolError("invalid JPEG SOI marker")
        if self.media_type == "image/png" and not payload.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise BinaryProtocolError("invalid PNG signature")
        object.__setattr__(self, "data", payload)


@dataclass(frozen=True)
class BinaryRequest:
    request_id: int
    state: np.ndarray
    images: tuple[EncodedImage, EncodedImage, EncodedImage]
    rtc_prefix: np.ndarray | None = None
    rtc_delay: int = 0
    rtc_execution: int = 0


@dataclass(frozen=True)
class BinaryResponse:
    request_id: int
    actions: np.ndarray
    timing_seconds: dict[str, float]


def encode_request(request: BinaryRequest) -> bytes:
    state = np.asarray(request.state, dtype="<f4")
    if state.shape != (ACTION_DIM,) or not np.isfinite(state).all():
        raise BinaryProtocolError("request state must be finite 34D float32")
    if len(request.images) != 3:
        raise BinaryProtocolError("request requires exactly three images")
    flags = 0
    prefix_steps = 0
    prefix_bytes = b""
    if request.rtc_prefix is not None:
        prefix = np.asarray(request.rtc_prefix, dtype="<f4")
        if (
            prefix.ndim != 2
            or prefix.shape[1] != ACTION_DIM
            or not 1 <= prefix.shape[0] <= CHUNK_STEPS
            or not np.isfinite(prefix).all()
        ):
            raise BinaryProtocolError("RTC prefix must be finite [1..50,34]")
        prefix_steps = int(prefix.shape[0])
        if not 0 <= request.rtc_delay <= prefix_steps:
            raise BinaryProtocolError("RTC delay does not fit prefix")
        if not request.rtc_delay <= request.rtc_execution <= CHUNK_STEPS - request.rtc_delay:
            raise BinaryProtocolError("RTC requires d <= s <= 50-d")
        if prefix_steps != CHUNK_STEPS - request.rtc_execution:
            raise BinaryProtocolError("RTC prefix length must equal 50-s")
        flags = RTC_FLAG
        prefix_bytes = np.ascontiguousarray(prefix).tobytes()
    elif request.rtc_delay or request.rtc_execution:
        raise BinaryProtocolError("non-RTC request cannot contain d/s")
    parts = [
        _REQUEST_HEADER.pack(
            REQUEST_MAGIC,
            int(request.request_id),
            flags,
            int(request.rtc_delay),
            int(request.rtc_execution),
            prefix_steps,
        ),
        np.ascontiguousarray(state).tobytes(),
        prefix_bytes,
    ]
    for image in request.images:
        parts.append(
            _IMAGE_HEADER.pack(
                _MEDIA_TO_CODE[image.media_type],
                image.width,
                image.height,
                len(image.data),
            )
        )
        parts.append(image.data)
    return b"".join(parts)


def decode_request(payload: bytes) -> BinaryRequest:
    view = memoryview(payload)
    if len(view) < _REQUEST_HEADER.size + ACTION_DIM * 4:
        raise BinaryProtocolError("request is truncated")
    magic, request_id, flags, delay, execution, prefix_steps = _REQUEST_HEADER.unpack_from(view)
    if magic != REQUEST_MAGIC or flags & ~RTC_FLAG:
        raise BinaryProtocolError("request magic or flags are invalid")
    offset = _REQUEST_HEADER.size
    state = np.frombuffer(view[offset : offset + ACTION_DIM * 4], dtype="<f4").copy()
    offset += ACTION_DIM * 4
    prefix = None
    if flags & RTC_FLAG:
        prefix_size = prefix_steps * ACTION_DIM * 4
        if prefix_steps < 1 or offset + prefix_size > len(view):
            raise BinaryProtocolError("RTC prefix is truncated")
        prefix = np.frombuffer(
            view[offset : offset + prefix_size], dtype="<f4"
        ).reshape(prefix_steps, ACTION_DIM).copy()
        offset += prefix_size
    elif prefix_steps or delay or execution:
        raise BinaryProtocolError("non-RTC header contains RTC fields")
    images = []
    for _ in range(3):
        if offset + _IMAGE_HEADER.size > len(view):
            raise BinaryProtocolError("image header is truncated")
        code, width, height, size = _IMAGE_HEADER.unpack_from(view, offset)
        offset += _IMAGE_HEADER.size
        if code not in _CODE_TO_MEDIA or size < 1 or offset + size > len(view):
            raise BinaryProtocolError("image payload is invalid")
        images.append(
            EncodedImage(
                bytes(view[offset : offset + size]),
                _CODE_TO_MEDIA[code],
                width,
                height,
            )
        )
        offset += size
    if offset != len(view):
        raise BinaryProtocolError("request contains trailing bytes")
    request = BinaryRequest(
        request_id,
        state,
        tuple(images),  # type: ignore[arg-type]
        prefix,
        delay,
        execution,
    )
    encode_request(request)
    return request


def encode_response(
    request_id: int,
    actions: np.ndarray,
    timing_seconds: dict[str, float],
) -> bytes:
    array = np.asarray(actions, dtype="<f4")
    if array.shape != (CHUNK_STEPS, ACTION_DIM) or not np.isfinite(array).all():
        raise BinaryProtocolError("response actions must be finite [50,34]")
    timing = _validated_timing(timing_seconds)
    metadata = json.dumps(
        {"timing_seconds": timing}, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return b"".join(
        (
            _RESPONSE_HEADER.pack(RESPONSE_MAGIC, int(request_id), 0, len(metadata)),
            np.ascontiguousarray(array).tobytes(),
            metadata,
        )
    )


def encode_error(request_id: int, reason: str) -> bytes:
    metadata = json.dumps(
        {"error": str(reason)}, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return _RESPONSE_HEADER.pack(RESPONSE_MAGIC, int(request_id), 1, len(metadata)) + metadata


def decode_response(payload: bytes) -> BinaryResponse:
    view = memoryview(payload)
    if len(view) < _RESPONSE_HEADER.size:
        raise BinaryProtocolError("response is truncated")
    magic, request_id, status, metadata_size = _RESPONSE_HEADER.unpack_from(view)
    if magic != RESPONSE_MAGIC or status not in (0, 1):
        raise BinaryProtocolError("response header is invalid")
    action_size = _ACTION_BYTES if status == 0 else 0
    action_end = _RESPONSE_HEADER.size + action_size
    if len(view) != action_end + metadata_size:
        raise BinaryProtocolError("response length does not match header")
    try:
        metadata: Any = json.loads(bytes(view[action_end:]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BinaryProtocolError("response metadata is invalid JSON") from exc
    if not isinstance(metadata, dict):
        raise BinaryProtocolError("response metadata must be an object")
    if status:
        raise BinaryProtocolError(f"server error: {metadata.get('error')}")
    actions = np.frombuffer(
        view[_RESPONSE_HEADER.size:action_end], dtype="<f4"
    ).reshape(CHUNK_STEPS, ACTION_DIM).copy()
    raw_timing = metadata.get("timing_seconds", {})
    if not isinstance(raw_timing, dict):
        raise BinaryProtocolError("response timing must be an object")
    return BinaryResponse(request_id, actions, _validated_timing(raw_timing))


def _validated_timing(values: dict[str, Any]) -> dict[str, float]:
    result = {}
    for key, value in values.items():
        number = float(value)
        if not str(key) or not np.isfinite(number) or number < 0:
            raise BinaryProtocolError("response contains invalid timing")
        result[str(key)] = number
    return result


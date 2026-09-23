from __future__ import annotations

import hashlib

import numpy as np
import pytest

from g2_sort.transport.dm05_binary import (
    ACTION_DIM,
    CHUNK_STEPS,
    BinaryRequest,
    EncodedImage,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)


def _images():
    return tuple(
        EncodedImage(b"\xff\xd8" + bytes([index]), "image/jpeg", 1280, 720)
        for index in (1, 2, 3)
    )


def test_binary_request_round_trip_preserves_float32_rtc_and_native_jpegs():
    prefix = np.arange(30 * ACTION_DIM, dtype=np.float32).reshape(30, ACTION_DIM)
    original = BinaryRequest(
        request_id=42,
        state=np.arange(ACTION_DIM, dtype=np.float32),
        images=_images(),
        rtc_prefix=prefix,
        rtc_delay=16,
        rtc_execution=20,
    )

    decoded = decode_request(encode_request(original))

    assert decoded.request_id == 42
    assert np.array_equal(decoded.state, original.state)
    assert np.array_equal(decoded.rtc_prefix, prefix)
    assert [image.data for image in decoded.images] == [
        image.data for image in original.images
    ]


def test_binary_action_response_is_fixed_6800_byte_float32_plus_metadata():
    actions = np.linspace(-2.0, 2.0, CHUNK_STEPS * ACTION_DIM).reshape(
        CHUNK_STEPS, ACTION_DIM
    )
    payload = encode_response(5, actions, {"server_handler_seconds": 0.2})
    decoded = decode_response(payload)

    assert decoded.actions.dtype == np.float32
    assert decoded.actions.nbytes == 6800
    assert np.max(np.abs(decoded.actions.astype(np.float64) - actions)) < 1.2e-7
    assert decoded.timing_seconds["server_handler_seconds"] == pytest.approx(0.2)
    assert len(payload) < 7000


def test_binary_request_matches_cross_repository_golden_wire_hash():
    payload = encode_request(
        BinaryRequest(
            request_id=42,
            state=np.arange(ACTION_DIM, dtype=np.float32),
            images=_images(),
        )
    )
    assert len(payload) == 208
    assert hashlib.sha256(payload).hexdigest() == (
        "e6fcee255fbbacb3ed4978fd4c7fccb677f02fe1d92e0628ce1f4ef106645e0f"
    )

import numpy as np
import pytest
from f1_harness.images import DecodedFrameCache, decode
from test_scene_pipeline import fixture


def test_exact_frame_reuses_immutable_pixels_without_reusing_new_evidence():
    pipeline, obs = fixture()
    frame = obs.frames[0]
    data = pipeline.blob(frame.blob_sha256)
    cache = DecodedFrameCache()
    first = cache.decode(frame, data)
    np.testing.assert_array_equal(first, decode(frame, data))
    assert cache.decode(frame, bytes(bytearray(data))) is first
    with pytest.raises(ValueError):
        first[0, 0] = 255
    with pytest.raises(ValueError):
        first.setflags(write=True)
    newer = frame.model_copy(update={'sequence': 2, 'received_ns': 2})
    assert cache.decode(newer, data) is not first
    changed = bytes([255]) + data[1:]
    assert cache.decode(frame, changed) is not first


def test_validation_and_depth_units_cannot_be_bypassed_by_cache():
    pipeline, obs = fixture()
    frame = obs.frames[1]
    data = pipeline.blob(frame.blob_sha256)
    cache = DecodedFrameCache()
    cache.decode(frame, data)
    with pytest.raises(ValueError, match='integer_depth_meter_scale_unverified'):
        cache.decode(frame.model_copy(update={'depth_scale_m': 1.}), data)
    with pytest.raises(ValueError, match='stride_or_length'):
        cache.decode(frame, data[:-1])


def test_lru_is_bounded_and_oversized_frames_are_not_retained():
    pipeline, obs = fixture()
    frame = obs.frames[0]
    data = pipeline.blob(frame.blob_sha256)
    cache = DecodedFrameCache(max_bytes=len(data)*4, max_entries=2)
    first = cache.decode(frame, data)
    for i in (2, 3):
        cache.decode(frame.model_copy(update={'sequence': i}), data)
    assert len(cache._items) == 2 and cache._bytes <= cache.max_bytes
    assert cache.decode(frame, data) is not first
    small = DecodedFrameCache(max_bytes=1)
    small.decode(frame, data)
    assert not small._items and small._bytes == 0

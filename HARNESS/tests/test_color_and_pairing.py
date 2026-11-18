"""Production decoder and asynchronous camera delivery regressions."""
import cv2
import numpy as np
import pytest

from f1_harness.images import decode
from f1_harness.observation import ObservationBuffer
from f1_harness.perception import FixedColorSegmenter
from test_observation import frame


@pytest.mark.parametrize('encoding', ['rgb8', 'bgr8', 'png'])
def test_production_decode_preserves_rgb_for_color_segmentation(encoding):
    # Wooden target color from the registered HSV range: the old BGR output
    # turns this brown into blue and silently rejects the first real object.
    pixels = np.zeros((30, 40, 3), np.uint8)
    pixels[8:20, 12:25] = [160, 110, 80]
    bgr = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    data = (cv2.imencode('.png', bgr)[1].tobytes() if encoding == 'png'
            else (bgr if encoding == 'bgr8' else pixels).tobytes())
    image = decode(frame('rgb', width=40, height=30, encoding=encoding,
                         depth_scale_m=None), data)
    assert np.array_equal(image, pixels)
    segmenter = FixedColorSegmenter({'wood': dict(hsv_lower=[5, 60, 55],
        hsv_upper=[18, 170, 200], minimum_area_px=100, maximum_area_px=300)})
    mask, score = segmenter.segment(image, box=[2, 2, 38, 28], object_id='wood')[0]
    assert score == 1 and mask.sum() == 12 * 13


@pytest.mark.parametrize('trusted', [False, True])
def test_latest_incomplete_rgb_does_not_hide_fresh_complete_pair(trusted):
    buffer = ObservationBuffer(source_clock_verified=trusted, max_skew_ns=20,
                               clock=lambda: 140)
    buffer.add(frame('rgb', sequence=1, received_ns=100, source_ns=100))
    buffer.add(frame('depth', sequence=1, received_ns=102, source_ns=102))
    buffer.add(frame('rgb', sequence=2, received_ns=133, source_ns=133))
    before = buffer.snapshot()
    assert before.pairings[0].quality == ('source_time_bounded' if trusted else 'receipt_only')
    assert before.pairings[0].rgb_sequence == 1
    assert before.frames[0].received_ns == 100  # No rejuvenation of older pixels.
    buffer.add(frame('depth', sequence=2, received_ns=135, source_ns=135))
    after = buffer.snapshot()
    assert after.pairings[0].rgb_sequence == after.pairings[0].depth_sequence == 2


def test_missing_complete_pair_still_rejected():
    buffer = ObservationBuffer(max_skew_ns=20)
    buffer.add(frame('rgb', received_ns=100))
    buffer.add(frame('depth', received_ns=150))
    assert buffer.snapshot().pairings[0].quality == 'invalid'


def test_camera_pairs_are_selected_from_one_common_time_window():
    buffer=ObservationBuffer(clock=lambda:330_000_000,common_camera_window_ns=100_000_000)
    for camera,sequence,stamp in [('head',1,100_000_000),('right',1,110_000_000),
                                  ('right',2,300_000_000)]:
        for stream,offset in [('rgb',0),('depth',2_000_000)]:
            buffer.add(frame(stream,camera=camera,sequence=sequence,
                source_ns=stamp+offset,received_ns=stamp+offset))
    obs=buffer.snapshot()
    assert {p.camera:p.rgb_sequence for p in obs.pairings}=={'head':1,'right':1}
    assert max(f.received_ns for f in obs.frames)-min(f.received_ns for f in obs.frames)==12_000_000
    assert min(f.received_ns for f in obs.frames)==100_000_000


def test_independent_geometry_keeps_each_cameras_newest_complete_pair():
    buffer=ObservationBuffer(clock=lambda:330_000_000)
    for camera,sequence,stamp in [('head',1,100_000_000),('right',1,110_000_000),('right',2,300_000_000)]:
        for stream,offset in [('rgb',0),('depth',2_000_000)]:
            buffer.add(frame(stream,camera=camera,sequence=sequence,
                source_ns=stamp+offset,received_ns=stamp+offset))
    obs=buffer.snapshot()
    assert {p.camera:p.rgb_sequence for p in obs.pairings}=={'head':1,'right':2}
    assert all(p.quality=='receipt_only' for p in obs.pairings)


def test_identical_redelivery_is_dropped_without_poisoning_accepted_pair():
    buffer=ObservationBuffer(clock=lambda:140)
    for stream in ('rgb','depth'):buffer.add(frame(stream,received_ns=100,source_ns=100))
    for sequence in range(2,8):
        buffer.add(frame('rgb',sequence=sequence,received_ns=100+sequence,source_ns=100))
    obs=buffer.snapshot()
    assert not obs.faults and obs.pairings[0].quality=='receipt_only'
    assert obs.frames[0].sequence==1 and obs.frames[0].received_ns==100
    assert buffer.duplicate_source_frames==buffer.rejected_source_frames==6


def test_same_stamp_different_pixels_is_still_a_fault():
    buffer=ObservationBuffer(clock=lambda:140)
    for stream in ('rgb','depth'):buffer.add(frame(stream,received_ns=100,source_ns=100))
    buffer.add(frame('rgb',sequence=2,received_ns=120,source_ns=100,blob_sha256='c'*64))
    obs=buffer.snapshot()
    assert obs.pairings[0].quality=='invalid' and obs.faults==('duplicate_source_stamp:head',)
    assert buffer.duplicate_source_frames==0

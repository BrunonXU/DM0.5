"""H-26: short-window hold for same-source-stamp conflicting-pixel frames."""
from f1_harness.contracts import Frame
from f1_harness.observation import ObservationBuffer, SOURCE_STAMP_HOLD_NS

BLOB_A = 'a' * 64
BLOB_B = 'b' * 64
BLOB_D = 'd' * 64


def _frame(stream, *, sequence, received_ns, source_ns, blob, camera='head'):
    return Frame(
        camera=camera,
        stream=stream,
        sequence=sequence,
        frame_id=f'{camera}_color_optical_frame',
        received_ns=received_ns,
        source_ns=source_ns,
        clock_domain='source',
        width=640,
        height=480,
        encoding='rgb8' if stream == 'rgb' else '16UC1',
        blob_sha256=blob,
        camera_info_sha256='1' * 64,
        depth_scale_m=None if stream == 'rgb' else 0.001,
    )


def test_same_stamp_different_pixels_within_hold_window_discards_without_fault():
    assert SOURCE_STAMP_HOLD_NS == 250_000_000
    buf = ObservationBuffer(source_clock_verified=False, max_skew_ns=20_000_000)
    t0 = 1_000_000_000
    buf.add(_frame('rgb', sequence=1, received_ns=t0, source_ns=500_000_000, blob=BLOB_A))
    buf.add(_frame('depth', sequence=1, received_ns=t0 + 1_000_000, source_ns=500_000_000, blob=BLOB_D))
    # 33 ms later: same source_ns, different pixels -> discard silently, keep first frame
    buf.add(_frame('rgb', sequence=2, received_ns=t0 + 33_000_000, source_ns=500_000_000, blob=BLOB_B))

    obs = buf.snapshot()
    assert not obs.faults
    assert obs.pairings[0].quality == 'receipt_only'
    rgb_frame = next(f for f in obs.frames if f.camera == 'head' and f.stream == 'rgb')
    assert rgb_frame.sequence == 1
    assert rgb_frame.received_ns == t0
    assert rgb_frame.blob_sha256 == BLOB_A
    assert buf.held_source_frames == 1
    assert buf.rejected_source_frames == 1
    assert buf.duplicate_source_frames == 0
    diag = buf.diagnostic_events()
    assert diag[-1]['reason'] == 'duplicate_source_stamp'
    assert diag[-1]['held_ns'] == 33_000_000


def test_same_stamp_different_pixels_beyond_hold_window_sets_fault():
    buf = ObservationBuffer(source_clock_verified=False, max_skew_ns=20_000_000)
    t0 = 1_000_000_000
    buf.add(_frame('rgb', sequence=1, received_ns=t0, source_ns=500_000_000, blob=BLOB_A))
    buf.add(_frame('depth', sequence=1, received_ns=t0 + 1_000_000, source_ns=500_000_000, blob=BLOB_D))
    # 300 ms later (> 250 ms hold window): must raise transient fault
    buf.add(_frame('rgb', sequence=2, received_ns=t0 + 300_000_000, source_ns=500_000_000, blob=BLOB_B))

    obs = buf.snapshot()
    assert 'duplicate_source_stamp:head' in obs.faults
    assert obs.pairings[0].quality == 'invalid'
    assert buf.held_source_frames == 0
    assert buf.rejected_source_frames == 1
    diag = buf.diagnostic_events()
    assert diag[-1]['reason'] == 'duplicate_source_stamp'
    assert diag[-1]['held_ns'] == 300_000_000


def test_same_stamp_same_pixels_increments_duplicate_source_frames():
    buf = ObservationBuffer(source_clock_verified=False, max_skew_ns=20_000_000)
    t0 = 1_000_000_000
    buf.add(_frame('rgb', sequence=1, received_ns=t0, source_ns=500_000_000, blob=BLOB_A))
    buf.add(_frame('rgb', sequence=2, received_ns=t0 + 33_000_000, source_ns=500_000_000, blob=BLOB_A))

    obs = buf.snapshot()
    assert not obs.faults
    assert buf.duplicate_source_frames == 1
    assert buf.held_source_frames == 0
    assert buf.rejected_source_frames == 1


def test_source_time_reversed_still_sets_fault():
    buf = ObservationBuffer(source_clock_verified=False, max_skew_ns=20_000_000)
    t0 = 1_000_000_000
    buf.add(_frame('rgb', sequence=1, received_ns=t0, source_ns=500_000_000, blob=BLOB_A))
    buf.add(_frame('rgb', sequence=2, received_ns=t0 + 33_000_000, source_ns=400_000_000, blob=BLOB_B))

    obs = buf.snapshot()
    assert 'source_time_reversed:head' in obs.faults
    assert buf.held_source_frames == 0
    assert buf.rejected_source_frames == 1

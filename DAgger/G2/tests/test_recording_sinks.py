import base64
from dataclasses import replace
import json
import threading

import pytest

from g2dagger.recording_observation import (
    CAMERAS, DeviceTimestamp, TimestampedFrame, TimestampedObservation, SharedObservationStream,
    frame_from_gdk_message,
)
from g2dagger.recording_sinks import EpisodeVideoSink, RingBufferSink, make_recording_sink
from g2dagger.recording_audit import audit_payload_archive

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJuQAAAAASUVORK5CYII=")


def observation(i):
    t = 100 + i * 100_000_000
    return TimestampedObservation(i, t, t + 3, tuple(
        TimestampedFrame(name, i, DeviceTimestamp(i, "ns", "synthetic"),
                         t + j, PNG, "image/png", 1, 1)
        for j, name in enumerate(CAMERAS)), t, (None,) * 34, (False,) * 34, "synthetic_missing")


def test_three_modes_share_observation_and_capture_continues_during_hil(tmp_path):
    results = []
    for mode in ("inference-only", "hil-record", "prebuffer-record"):
        sink = make_recording_sink(mode, tmp_path / mode)
        iterator = iter(observation(i) for i in range(20))
        stream = SharedObservationStream(lambda: next(iterator), sink)
        requested = []
        for i in range(20):
            obs = stream.acquire()
            if i == 5:
                assert sink.trigger()
            if not 5 <= i < 15:  # inference paused, acquisition still advances
                assert stream.policy_observation() is obs
                requested.append(obs.observation_id)
        assert sink.close()
        results.append(requested)
        if mode != "inference-only":
            report = audit_payload_archive(tmp_path / mode)
            assert report["valid"] and report["count"] == 20
            assert not report["training_ready"]
    assert results[0] == results[1] == results[2]
    assert not (tmp_path / "inference-only").exists()


def test_ring_eviction_and_trigger_exactly_once(tmp_path):
    writer = EpisodeVideoSink(tmp_path / "ring")
    ring = RingBufferSink(writer, seconds=.3, max_items=3)
    for i in range(10):
        ring.submit(observation(i))
        assert ring.retained_bytes <= ring.max_bytes
    assert ring.capacity_evictions > 0
    assert ring.trigger() and ring.trigger()
    assert ring.submit(observation(10)) and ring.close()
    rows = [json.loads(s) for s in (writer.path / "frames.jsonl").read_text().splitlines()]
    assert [r["observation_id"] for r in rows] == [7, 8, 9, 10]
    assert audit_payload_archive(writer.path)["warnings"]


def test_slow_disk_inflight_bytes_overflow_and_timeout_remain_partial(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowSink(EpisodeVideoSink):
        def _write_observation(self, obs, index):
            entered.set()
            assert release.wait(5)
            super()._write_observation(obs, index)
    sink = SlowSink(tmp_path / "slow", max_items=1)
    try:
        assert sink.submit(observation(0))
        assert entered.wait(2)
        assert not sink.submit(observation(1))
        assert sink.pending_items == 1  # in-flight write still consumes capacity
        assert not sink.close(timeout=.001)
        assert not sink.path.exists()
    finally:
        release.set()
    assert not sink.close()
    manifest = json.loads((sink.partial_path / "manifest.json").read_text())
    assert manifest["drop_count"] == 1 and manifest["first_drop_id"] == 1
    assert not audit_payload_archive(sink.partial_path)["valid"]


def test_disk_failure_and_abnormal_close(tmp_path):
    class BrokenSink(EpisodeVideoSink):
        def _write_observation(self, obs, index):
            raise OSError("injected disk failure")
    sink = BrokenSink(tmp_path / "broken")
    sink.submit(observation(0))
    assert not sink.close()
    assert "injected" in sink.writer_error
    sink2 = EpisodeVideoSink(tmp_path / "aborted")
    sink2.submit(observation(0))
    assert not sink2.close(status="aborted")
    assert not sink2.close()  # later cleanup cannot turn an abort into completion


def test_payload_hash_and_duplicate_timestamp_detection(tmp_path):
    sink = EpisodeVideoSink(tmp_path / "duplicate")
    first, second = observation(0), observation(1)
    second = replace(second, frames=tuple(replace(f, timestamp=first.frames[j].timestamp)
                                        for j, f in enumerate(second.frames)))
    sink.submit(first)
    sink.submit(second)
    assert sink.close()
    assert not audit_payload_archive(sink.path)["valid"]
    (sink.path / "head/000000000.png").write_bytes(b"broken")
    assert "payload size/hash mismatch" in audit_payload_archive(sink.path)["errors"]


def test_raw_timestamp_adapter_and_missing_feedback():
    class Message:
        timestamp_ns = 1234567890123456789
        data = PNG
        width = height = 1
    f = frame_from_gdk_message(Message(), camera="head", frame_id=0,
                               host_received_monotonic_ns=5, media_type="image/png")
    assert f.timestamp.value == Message.timestamp_ns
    assert f.timestamp.unit == "ns" and f.timestamp.clock_domain == "gdk_device_unverified"
    with pytest.raises(ValueError, match="finite measured"):
        replace(observation(0), measured_valid_34d=(True,) * 34)


def test_journal_references_exact_shared_payload_acquisition(tmp_path):
    from g2dagger.hil_episode import HILEpisodeRecorder
    from g2dagger.recording_audit import audit_journal_payload_links
    journal = HILEpisodeRecorder(tmp_path / "journal", episode_id="j", task_prompt="test")
    sink = EpisodeVideoSink(tmp_path / "payload")
    obs = observation(0)
    assert journal.record_timestamped_observation(obs, hil_state="hil_latched", intervention_id=1, policy_epoch=1)
    assert sink.submit(obs)
    assert journal.close() and sink.close()
    assert audit_journal_payload_links(journal.path, sink.path) == []
    path = journal.path / "observations.jsonl"
    row = json.loads(path.read_text())
    row["acquisition"]["frames"][0]["timestamp"]["value"] += 1
    path.write_text(json.dumps(row) + "\n")
    assert audit_journal_payload_links(journal.path, sink.path) == ["journal/payload acquisition mismatch"]

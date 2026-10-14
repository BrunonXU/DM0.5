"""Bounded, non-waiting admission and asynchronous native frame archive.

Call submit/trigger from the acquisition/event worker, not the 100 Hz owner.
EpisodeVideoSink preserves source images and a frame index; it does not claim
that a JPEG directory is an encoded video or a training episode.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import threading
import time


class NullSink:
    def submit(self, observation):
        return True

    def trigger(self):
        return True

    def close(self, **kwargs):
        return True


class RingBufferSink:
    """Single acquisition-worker owner; trigger serialized with submit.

    Time eviction is expected pre-record retention. Capacity eviction is reported
    separately because it can shorten the requested pre-roll.
    """
    def __init__(self, downstream, *, seconds=4.0, max_bytes=128 * 1024**2, max_items=180):
        if not 0 < seconds <= 60 or min(max_bytes, max_items) <= 0:
            raise ValueError("positive finite retention and capacity required")
        self.downstream = downstream
        self.duration_ns = int(seconds * 1e9)
        self.max_bytes, self.max_items = max_bytes, max_items
        self._items = deque()
        self.retained_bytes = 0
        self.capacity_evictions = 0
        self.oversize_drops = 0
        self.active = False
        self.closed = False
        self._last_time = -1

    def submit(self, observation):
        if self.closed:
            return False
        now = observation.available_monotonic_ns
        if now < self._last_time:
            raise ValueError("acquisition clock regressed")
        self._last_time = now
        if self.active:
            return self.downstream.submit(observation)
        while self._items and self._items[0].available_monotonic_ns < now - self.duration_ns:
            self.retained_bytes -= self._items.popleft().retained_bytes
        if observation.retained_bytes > self.max_bytes:
            self.oversize_drops += 1
            return False
        self._items.append(observation)
        self.retained_bytes += observation.retained_bytes
        while len(self._items) > self.max_items or self.retained_bytes > self.max_bytes:
            self.retained_bytes -= self._items.popleft().retained_bytes
            self.capacity_evictions += 1
        return True

    def trigger(self):
        if self.closed:
            return False
        if self.active:
            return True
        self.active = True
        self.downstream.prebuffer = {
            "requested_seconds": self.duration_ns / 1e9,
            "retained_observations": len(self._items),
            "capacity_evictions": self.capacity_evictions, "oversize_drops": self.oversize_drops}
        accepted = self.downstream.submit_batch(tuple(self._items))
        self._items.clear()
        self.retained_bytes = 0
        return accepted

    def close(self, **kwargs):
        self.closed = True
        self._items.clear()
        self.retained_bytes = 0
        return self.downstream.close(**kwargs)


class EpisodeVideoSink:
    """Native payload writer; byte/item limits INCLUDE in-flight writes.

    Overflow rejects the whole batch, records exact count and first/last ID,
    and prevents publication. Disk errors likewise retain .partial evidence.
    No encoding, hashing, JSON or disk I/O occurs in submit/trigger.
    """
    def __init__(self, path, *, max_bytes=192 * 1024**2, max_items=256, training_blockers=None):
        if min(max_bytes, max_items) <= 0:
            raise ValueError("positive capacity required")
        self.training_blockers = training_blockers
        self.path = Path(path)
        self.partial_path = self.path.with_name(self.path.name + ".partial")
        if self.path.exists() or self.partial_path.exists():
            raise FileExistsError(self.path)
        self.partial_path.mkdir(parents=True)
        self.max_bytes, self.max_items = max_bytes, max_items
        self._condition = threading.Condition()
        self._queue = deque()
        self.pending_bytes = self.pending_items = 0
        self.peak_bytes = self.peak_items = 0
        self.drop_count = 0
        self.first_drop_id = self.last_drop_id = None
        self.writer_error = None
        self.prebuffer = None
        self._closed = False
        self._close_status = None
        self._published = False
        self.count = 0
        self._thread = threading.Thread(target=self._run, name="g2-payload-writer", daemon=True)
        self._thread.start()

    def submit(self, observation):
        return self.submit_batch((observation,))

    def submit_batch(self, observations):
        size = sum(o.retained_bytes for o in observations)
        with self._condition:
            if self._closed:
                return False
            if (self.writer_error or self.pending_bytes + size > self.max_bytes
                    or self.pending_items + len(observations) > self.max_items):
                self.drop_count += len(observations)
                if observations:
                    if self.first_drop_id is None:
                        self.first_drop_id = observations[0].observation_id
                    self.last_drop_id = observations[-1].observation_id
                return False
            self._queue.extend(observations)
            self.pending_bytes += size
            self.pending_items += len(observations)
            self.peak_bytes = max(self.peak_bytes, self.pending_bytes)
            self.peak_items = max(self.peak_items, self.pending_items)
            self._condition.notify()
            return True

    def trigger(self):
        return not self._closed

    def _write_observation(self, observation, index):
        row = observation.metadata()
        for frame, metadata in zip(observation.frames, row["frames"]):
            suffix = "jpg" if frame.media_type == "image/jpeg" else "png"
            relative = f"{frame.camera}/{self.count:09d}.{suffix}"
            path = self.partial_path / relative
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as handle:
                handle.write(frame.payload)
                handle.flush()
                os.fsync(handle.fileno())
            metadata.update(path=relative, payload_sha256=hashlib.sha256(frame.payload).hexdigest(),
                            payload_bytes=len(frame.payload), archive_frame_index=self.count)
        index.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")
        index.flush()
        self.count += 1

    def _run(self):
        try:
            with (self.partial_path / "frames.jsonl").open("x") as index:
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: self._queue or self._closed)
                        if not self._queue:
                            break
                        observation = self._queue.popleft()
                    self._write_observation(observation, index)
                    with self._condition:
                        self.pending_bytes -= observation.retained_bytes
                        self.pending_items -= 1
                os.fsync(index.fileno())
        except Exception as exc:
            with self._condition:
                self.writer_error = f"{type(exc).__name__}: {exc}"
                self._queue.clear()
                self.pending_bytes = self.pending_items = 0

    def close(self, *, status="completed", timeout=30.0):
        with self._condition:
            if self._close_status is None:
                self._close_status = status
            self._closed = True
            self._condition.notify()
        self._thread.join(timeout=max(0, timeout))
        if self._thread.is_alive():
            # Writer may still be in a syscall. Never rename underneath it.
            return False
        if self._published:
            return True
        status = self._close_status
        clean = status == "completed" and not self.writer_error and not self.drop_count
        manifest = dict(schema_version=1, format="native-image-sequence", complete=clean,
                        training_ready=False, video_encoded=False, count=self.count,
                        drop_count=self.drop_count, first_drop_id=self.first_drop_id,
                        last_drop_id=self.last_drop_id, writer_error=self.writer_error,
                        peak_retained_bytes=self.peak_bytes, peak_retained_items=self.peak_items,
                        prebuffer=self.prebuffer, status=status,
                        training_blockers=["device clock mapping unverified", "video mapping not encoded/audited",
                                           "34D command/feedback alignment and labels not approved"])
        if self.training_blockers is not None:
            manifest["training_blockers"] = list(self.training_blockers)
        temporary = self.partial_path / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(temporary, self.partial_path / "manifest.json")
        if clean:
            os.replace(self.partial_path, self.path)
            self._published = True
        return clean


def make_recording_sink(mode, path=None, *, prebuffer_seconds=4.0, **writer_options):
    if mode == "inference-only":
        return NullSink()
    if mode not in ("hil-record", "prebuffer-record"):
        raise ValueError(f"unknown recording mode {mode}")
    if path is None:
        raise ValueError("recording requires an output path")
    writer = EpisodeVideoSink(path, **writer_options)
    return writer if mode == "hil-record" else RingBufferSink(writer, seconds=prebuffer_seconds)

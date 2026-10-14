"""F1 native-image packs with bounded admission and grouped durable commits.

The camera bytes are stored unchanged. The index binds every image to its pack
offset, length and SHA256; decoding remains an offline operation. Capacity
includes the entire pending batch until all three packs and its index are
synced. Only a clean final sync and manifest publish remove ``.partial``.
"""
from collections import deque
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time

from .contract import CAMERAS

FORMAT = 'native-image-packs'
STORAGE = 'f1-camera-pack-v1'


def read_payload(root, frame):
    """Read and verify one unchanged image from either F1 archive generation."""
    root = Path(root).resolve()
    relative = Path(frame['path'])
    target = (root / relative).resolve()
    if relative.is_absolute() or root not in target.parents:
        raise ValueError('image escaped archive')
    size = frame['payload_bytes']
    if type(size) is not int or size <= 0:
        raise ValueError('invalid payload length')
    storage = frame.get('storage')
    if storage is None:
        if 'payload_offset' in frame or target.stat().st_size != size:
            raise ValueError('image size/storage mismatch')
        payload = target.read_bytes()
    elif storage == STORAGE:
        offset = frame.get('payload_offset')
        if (frame.get('camera') not in CAMERAS or relative.as_posix() != frame['camera'] + '.pack'
                or type(offset) is not int or offset < 0 or offset + size > target.stat().st_size):
            raise ValueError('invalid pack image bounds/path')
        with target.open('rb') as handle:
            handle.seek(offset)
            payload = handle.read(size)
    else:
        raise ValueError('unsupported image storage: ' + str(storage))
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != frame['payload_sha256']:
        raise ValueError('image hash/size mismatch')
    return payload


class F1PayloadSink:
    """Bounded single writer; no encoding or disk I/O in submit/diagnostics."""

    def __init__(self, path, *, max_bytes=192 * 1024**2, max_items=256,
                 checkpoint_observations=15, checkpoint_interval_s=.5,
                 training_blockers=()):
        if (any(type(v) is not int for v in (max_bytes, max_items, checkpoint_observations))
                or min(max_bytes, max_items, checkpoint_observations) <= 0
                or not math.isfinite(checkpoint_interval_s) or checkpoint_interval_s <= 0):
            raise ValueError('positive finite archive capacities and checkpoint interval required')
        self.path = Path(path)
        self.partial_path = self.path.with_name(self.path.name + '.partial')
        if self.path.exists() or self.partial_path.exists():
            raise FileExistsError(self.path)
        self.partial_path.mkdir(parents=True)
        self.max_bytes, self.max_items = max_bytes, max_items
        self.checkpoint_observations = min(checkpoint_observations, max_items)
        self.checkpoint_interval_s = checkpoint_interval_s
        self.training_blockers = list(training_blockers)
        self._condition = threading.Condition()
        self._queue = deque()
        self._closed = self._published = False
        self._close_status = None
        self.pending_bytes = self.pending_items = self.peak_bytes = self.peak_items = 0
        self.count = self.accepted_count = self.drop_count = 0
        self.first_drop_id = self.last_drop_id = None
        self.last_rejection_reason = self.writer_error = None
        self.prebuffer = None
        self.checkpoints = self.sync_calls = 0
        self.last_checkpoint_ms = self.max_checkpoint_ms = 0.
        self._started = time.monotonic()
        self._pack_bytes = dict.fromkeys(CAMERAS, 0)
        self._pack_hashes = {camera: hashlib.sha256() for camera in CAMERAS}
        self._thread = threading.Thread(target=self._run, name='f1-payload-writer', daemon=True)
        self._thread.start()

    def submit(self, observation):
        return self.submit_batch((observation,))

    def submit_batch(self, observations):
        size = sum(o.retained_bytes for o in observations)
        with self._condition:
            reason = ('closed' if self._closed else 'writer_error' if self.writer_error else
                      'pending_item_limit' if self.pending_items + len(observations) > self.max_items else
                      'pending_byte_limit' if self.pending_bytes + size > self.max_bytes else None)
            if reason:
                self.last_rejection_reason = reason
                if observations:
                    self.drop_count += len(observations)
                    if self.first_drop_id is None:
                        self.first_drop_id = observations[0].observation_id
                    self.last_drop_id = observations[-1].observation_id
                return False
            self._queue.extend(observations)
            self.pending_bytes += size
            self.pending_items += len(observations)
            self.accepted_count += len(observations)
            self.peak_bytes = max(self.peak_bytes, self.pending_bytes)
            self.peak_items = max(self.peak_items, self.pending_items)
            self._condition.notify()
            return True

    def trigger(self):
        with self._condition:
            return not self._closed and not self.writer_error

    def diagnostics(self):
        with self._condition:
            elapsed = max(time.monotonic() - self._started, 1e-9)
            return dict(format=FORMAT, storage_version=1, accepted_count=self.accepted_count,
                count=self.count, completed=self.count, pending_items=self.pending_items,
                pending_bytes=self.pending_bytes, max_items=self.max_items, max_bytes=self.max_bytes,
                peak_retained_items=self.peak_items, peak_retained_bytes=self.peak_bytes,
                drop_count=self.drop_count, writer_error=self.writer_error,
                last_rejection_reason=self.last_rejection_reason,
                last_rejected_observation_id=self.last_drop_id,
                checkpoint_observations=self.checkpoint_observations,
                checkpoint_interval_s=self.checkpoint_interval_s, checkpoints=self.checkpoints,
                sync_calls=self.sync_calls, last_checkpoint_ms=self.last_checkpoint_ms,
                max_checkpoint_ms=self.max_checkpoint_ms, completed_hz=self.count / elapsed)

    def _sync_file(self, handle):
        os.fsync(handle.fileno())
        with self._condition:
            self.sync_calls += 1

    @staticmethod
    def _sync_directory(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_batch(self, batch, packs, index):
        started = time.monotonic()
        for i, observation in enumerate(batch):
            row = observation.metadata()
            if tuple(f.camera for f in observation.frames) != CAMERAS:
                raise ValueError('ordered three-camera observation required')
            for frame, metadata in zip(observation.frames, row['frames']):
                camera = frame.camera
                payload = frame.payload
                offset = self._pack_bytes[camera]
                packs[camera].write(payload)
                self._pack_bytes[camera] += len(payload)
                self._pack_hashes[camera].update(payload)
                metadata.update(path=camera + '.pack', storage=STORAGE, payload_offset=offset,
                    payload_bytes=len(payload), payload_sha256=hashlib.sha256(payload).hexdigest(),
                    archive_frame_index=self.count + i)
            index.write(json.dumps(row, allow_nan=False, separators=(',', ':')) + '\n')
        # Flush every pack before syncing any of them, allowing the filesystem
        # to coalesce data writes. Pending capacity is released only afterward.
        for handle in (*packs.values(), index):
            handle.flush()
        for handle in (*packs.values(), index):
            self._sync_file(handle)
        duration_ms = (time.monotonic() - started) * 1000
        with self._condition:
            self.count += len(batch)
            self.pending_items -= len(batch)
            self.pending_bytes -= sum(o.retained_bytes for o in batch)
            self.checkpoints += 1
            self.last_checkpoint_ms = duration_ms
            self.max_checkpoint_ms = max(self.max_checkpoint_ms, duration_ms)

    def _run(self):
        try:
            with ExitStack() as stack:
                packs = {c: stack.enter_context((self.partial_path / (c + '.pack')).open('xb')) for c in CAMERAS}
                index = stack.enter_context((self.partial_path / 'frames.jsonl').open('x'))
                self._sync_directory(self.partial_path)
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: self._queue or self._closed)
                        if not self._queue:
                            break
                        batch = []
                        deadline = time.monotonic() + self.checkpoint_interval_s
                        while len(batch) < self.checkpoint_observations:
                            while self._queue and len(batch) < self.checkpoint_observations:
                                batch.append(self._queue.popleft())
                            remaining = deadline - time.monotonic()
                            if self._closed or remaining <= 0 or len(batch) == self.checkpoint_observations:
                                break
                            self._condition.wait(timeout=remaining)
                    self._write_batch(batch, packs, index)
                    batch.clear()  # Do not retain a durable batch while waiting for new input.
                # Empty archives still have durable, auditable pack/index files.
                if not self.count:
                    for handle in (*packs.values(), index):
                        handle.flush()
                        self._sync_file(handle)
        except Exception as exc:
            with self._condition:
                self.writer_error = f'{type(exc).__name__}: {exc}'
                self._queue.clear()
                self.pending_items = self.pending_bytes = 0

    def close(self, *, status='completed', timeout=30.):
        with self._condition:
            if self._published:
                return True
            if self._close_status is None:
                self._close_status = status
            self._closed = True
            self._condition.notify()
        self._thread.join(timeout=max(0., timeout))
        if self._thread.is_alive():
            return False
        clean = self._close_status == 'completed' and not self.writer_error and not self.drop_count
        manifest = dict(schema_version=2, format=FORMAT, storage_version=1, complete=clean,
            training_ready=False, video_encoded=False, count=self.count, drop_count=self.drop_count,
            first_drop_id=self.first_drop_id, last_drop_id=self.last_drop_id, writer_error=self.writer_error,
            peak_retained_bytes=self.peak_bytes, peak_retained_items=self.peak_items,
            prebuffer=self.prebuffer, status=self._close_status, training_blockers=self.training_blockers,
            durability='group_fsync_before_pending_release_and_final_manifest',
            writer=self.diagnostics(), pack_files={c: dict(path=c + '.pack', bytes=self._pack_bytes[c],
                sha256=self._pack_hashes[c].hexdigest()) for c in CAMERAS})
        temporary = self.partial_path / 'manifest.json.tmp'
        try:
            with temporary.open('w') as handle:
                json.dump(manifest, handle, indent=2, allow_nan=False)
                handle.write('\n')
                handle.flush()
                self._sync_file(handle)
            os.replace(temporary, self.partial_path / 'manifest.json')
            self._sync_directory(self.partial_path)
            if clean:
                os.replace(self.partial_path, self.path)
                try:
                    self._sync_directory(self.path.parent)
                except Exception:
                    os.replace(self.path, self.partial_path)
                    raise
                self._published = True
        except Exception as exc:
            with self._condition:
                self.writer_error = f'{type(exc).__name__}: {exc}'
                self._close_status = 'failed'
            raise
        return clean

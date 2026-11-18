"""Append-only run traces and content-addressed blobs; never loads credentials."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from collections import OrderedDict


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(exist_ok=True)
        self._lock = threading.Lock()

    def event(self, kind: str, **data) -> str:
        event_id = uuid.uuid4().hex
        row = dict(event_id=event_id, kind=kind, monotonic_ns=time.monotonic_ns(), **data)
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock, (self.root / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
        return event_id

    def diagnostic_event(self,kind,**data):
        """Optional supplemental facts cannot replace an original task error."""
        try:return self.event(kind,**data)
        except Exception:return None

    def put_blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / sha
        try:
            with path.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError("blob digest conflict")
        return sha

    def blob(self, sha: str) -> bytes:
        import re
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("invalid blob digest")
        data = (self.root / "blobs" / sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError("blob integrity failure")
        return data

    def write_json(self, name: str, data) -> Path:
        if Path(name).name != name:
            raise ValueError("artifact name must be a basename")
        path = self.root / name
        with path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        return path


class AsyncOwnerJournal:
    """Bounded nonblocking owner events; serialization/disk IO stay on a writer thread."""
    def __init__(self, store, capacity=4096):
        import queue
        self.store, self.root = store, store.root
        self.queue = queue.Queue(maxsize=capacity)
        self.diagnostic_queue = queue.Queue(maxsize=64)
        self.error = None
        self.error_detail = None
        self.closed = False
        self.thread = threading.Thread(target=self._write,daemon=True)
        self.thread.start()

    def event(self, kind, **data):
        import queue
        if self.error or self.closed:
            raise RuntimeError("owner_journal_unavailable")
        identity=uuid.uuid4().hex
        try:
            self.queue.put_nowait(dict(event_id=identity,kind=kind,monotonic_ns=time.monotonic_ns(),**data))
        except queue.Full:
            self.error="overflow"
            raise RuntimeError("owner_journal_overflow") from None
        return identity

    def diagnostic_event(self,kind,**data):
        """Drop optional data when busy; do not poison mandatory owner logging."""
        import queue
        if self.error or self.closed:return None
        identity=uuid.uuid4().hex
        row=dict(event_id=identity,kind=kind,monotonic_ns=time.monotonic_ns(),**data)
        try:
            # Validate/copy only this small diagnostic on the caller's RPC path.
            encoded=json.dumps(row,allow_nan=False)
            if len(encoded.encode())>8192:return None
            row=json.loads(encoded)
            self.diagnostic_queue.put_nowait(row)
        except (queue.Full,ValueError,TypeError):return None
        return identity

    def _write(self):
        import queue
        row = None
        try:
            with (self.root / "trace.jsonl").open("a") as stream:
                def drain_diagnostics():
                    # Flush/close waits are RPC-side; bounded optional work
                    # cannot occupy mandatory queue slots or extend indefinitely.
                    for _ in range(64):
                        try:optional=self.diagnostic_queue.get_nowait()
                        except queue.Empty:break
                        stream.write(json.dumps(optional,allow_nan=False)+'\n')
                while True:
                    try:row=self.queue.get(timeout=.05)
                    except queue.Empty:
                        try:row=self.diagnostic_queue.get_nowait()
                        except queue.Empty:continue
                    if row is None:
                        drain_diagnostics();break
                    if isinstance(row,threading.Event):
                        drain_diagnostics();stream.flush();row.set();continue
                    stream.write(json.dumps(row,allow_nan=False)+"\n")
                    stream.flush()
        except Exception as exc:
            self.error=type(exc).__name__
            # Preserve the failing event's identity, never its arbitrary payload.
            self.error_detail=dict(error_type=self.error,
                event_id=row.get('event_id') if isinstance(row,dict) else None,
                kind=row.get('kind') if isinstance(row,dict) else None)
            try:
                self.store.write_json('writer-error.json',self.error_detail)
            except Exception:
                pass  # Disk failure must remain visible in memory as well.

    def flush(self,timeout=1):
        """RPC/export only. Never call from the owner control tick."""
        if self.closed or self.error:raise RuntimeError('owner_journal_unavailable')
        barrier=threading.Event();self.queue.put(barrier,timeout=timeout)
        if not barrier.wait(timeout):raise RuntimeError('owner_journal_flush_timeout')

    def close(self):
        self.closed=True
        self.queue.put(None,timeout=1)
        self.thread.join(timeout=2)
        if self.thread.is_alive() or self.error:
            raise RuntimeError("owner_journal_not_flushed")


class FrameCache:
    """Bounded byte cache for camera callbacks; not a persistent evidence archive."""
    def __init__(self,capacity_bytes=256*1024*1024):
        self.capacity=capacity_bytes
        self.data=OrderedDict()
        self.size=0
        self.lock=threading.Lock()

    def put_blob(self,data):
        if len(data)>self.capacity:raise ValueError('frame_larger_than_cache')
        sha=hashlib.sha256(data).hexdigest()
        with self.lock:
            if sha in self.data:self.data.move_to_end(sha);return sha
            while self.size+len(data)>self.capacity:
                _,old=self.data.popitem(last=False);self.size-=len(old)
            self.data[sha]=data;self.size+=len(data)
        return sha

    def blob(self,sha):
        with self.lock:
            try:return self.data[sha]
            except KeyError:raise ValueError('frame_evicted_reobserve') from None

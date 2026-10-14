"""Asynchronous journal of HIL decision, submission and acquisition evidence.

Native payloads live in a separate sink. Cross-stream and raw clock/video/34D
training audits are distinct; journal completion never certifies training data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping, Sequence

import numpy as np


STREAMS = ("observations", "policy_chunks", "control", "events")

def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class EpisodeJournal:
    schema_version = 2
    action_contract = {}
    training_blockers = ("robot data audit required",)

    def __init__(
        self,
        episode_dir: Path,
        *,
        episode_id: str,
        task_prompt: str,
        capacity: int = 8192,
    ) -> None:
        if not episode_id or not task_prompt:
            raise ValueError("episode_id and task_prompt must be non-empty")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.path = Path(episode_dir)
        self.partial_path = self.path.with_name(self.path.name + ".partial")
        if self.path.exists() or self.partial_path.exists():
            raise FileExistsError(f"refusing to overwrite episode {self.path}")
        self.partial_path.mkdir(parents=True)
        self.episode_id = episode_id
        self.task_prompt = task_prompt
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
            capacity
        )
        self._lock = threading.Lock()
        self._counts = {stream: 0 for stream in STREAMS}
        self._next_sequence = {stream: 0 for stream in STREAMS}
        self._drops = 0
        self._closed = False
        self._writer_error: str | None = None
        self._started_wall_ns = time.time_ns()
        self._thread = threading.Thread(
            target=self._write_loop, name="g2-hil-episode-writer", daemon=True
        )
        self._thread.start()

    @property
    def drops(self) -> int:
        with self._lock:
            return self._drops

    @property
    def writer_error(self) -> str | None:
        with self._lock:
            return self._writer_error

    def _emit(self, stream: str, record: dict[str, Any]) -> bool:
        if stream not in STREAMS:
            raise ValueError(f"unknown episode stream {stream}")
        with self._lock:
            if self._closed:
                return False
            sequence = self._next_sequence[stream]
            self._next_sequence[stream] += 1
            envelope = {
                "schema_version": self.schema_version, "episode_id": self.episode_id,
                "stream_sequence": sequence, **record,
            }
            try:
                self._queue.put_nowait((stream, envelope))
                return True
            except queue.Full:
                self._drops += 1
                return False

    def record_event(self, event: str, *, monotonic_ns: int, **fields: Any) -> bool:
        if not event or monotonic_ns < 0:
            raise ValueError("event and monotonic_ns are required")
        return self._emit(
            "events", {"event": event, "monotonic_ns": monotonic_ns, **fields}
        )

    def close(self, *, status: str = "completed") -> bool:
        with self._lock:
            if self._closed:
                return self.path.exists()
            self._closed = True
        try:
            self._queue.put(None, timeout=10.0)
        except queue.Full:
            with self._lock:
                self._drops += 1
                self._writer_error = "episode queue did not drain within 10 seconds"
            return False
        self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            with self._lock:
                self._writer_error = "episode writer did not stop within 30 seconds"
            return False
        clean = status == "completed" and self.drops == 0 and self.writer_error is None
        manifest = {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "task_prompt": self.task_prompt,
            "status": status,
            "complete": clean,
            "structurally_auditable": clean,
            "training_ready": False,
            "training_blockers": list(self.training_blockers),
            "action_contract": dict(self.action_contract),
            "counts": dict(self._counts),
            "drop_count": self.drops,
            "writer_error": self.writer_error,
            "started_wall_ns": self._started_wall_ns,
            "closed_wall_ns": time.time_ns(),
        }
        manifest = self.format_manifest(manifest)
        manifest_path = self.partial_path / "manifest.json"
        temporary = self.partial_path / ".manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_path)
        if clean:
            os.replace(self.partial_path, self.path)
            return True
        return False

    def format_manifest(self, manifest):
        """Override robot metadata without changing queue/commit semantics."""
        return manifest

    def _write_loop(self) -> None:
        handles: dict[str, Any] = {}
        try:
            handles = {
                stream: (self.partial_path / f"{stream}.jsonl").open(
                    "x", encoding="utf-8"
                )
                for stream in STREAMS
            }
            pending = 0
            while True:
                item = self._queue.get()
                if item is None:
                    break
                stream, record = item
                handles[stream].write(
                    json.dumps(_jsonable(record), separators=(",", ":")) + "\n"
                )
                self._counts[stream] += 1
                pending += 1
                if pending >= 32 or self._queue.empty():
                    for handle in handles.values():
                        handle.flush()
                    pending = 0
            for handle in handles.values():
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:  # pragma: no cover - filesystem failure
            with self._lock:
                self._writer_error = f"{type(exc).__name__}: {exc}"
        finally:
            for handle in handles.values():
                handle.close()


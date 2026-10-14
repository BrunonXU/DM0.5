"""Non-blocking structured telemetry for the F710 powered control loop."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


class ControlTelemetry:
    """Move JSON serialization and disk I/O out of the 100 Hz control thread.

    ``emit`` is deliberately best-effort: a full queue increments ``dropped``
    and returns immediately.  Observability must never delay or fault motion.
    """

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        mode: str = "powered-f710",
        capacity: int = 8192,
    ) -> None:
        if capacity <= 0:
            raise ValueError("telemetry queue capacity must be positive")
        self.path = Path(path)
        self.partial_path = Path(f"{self.path}.partial")
        self.run_id = str(run_id)
        self.mode = str(mode)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.partial_path.exists():
            raise FileExistsError(
                f"refusing to overwrite telemetry run: {self.path}"
            )
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(capacity)
        self._lock = threading.Lock()
        self._sequence = 0
        self._dropped = 0
        self._closed = False
        self._writer_error: str | None = None
        self._thread = threading.Thread(
            target=self._write_loop,
            name="g2-f710-telemetry-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def writer_error(self) -> str | None:
        with self._lock:
            return self._writer_error

    def emit(self, event: str, **fields: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            sequence = self._sequence
            self._sequence += 1
            dropped = self._dropped
        record = {
            "schema_version": SCHEMA_VERSION,
            "telemetry_family": "f710_teleop",
            "event": str(event),
            "run_id": self.run_id,
            "sequence": sequence,
            "drop_count_before": dropped,
            "wall_time": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            **fields,
        }
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def emit_quiet(self, event: str, **fields: Any) -> bool:
        """Compatibility alias for compact dispatch telemetry adapters."""
        return self.emit(event, **fields)

    def close(self, *, status: str = "closed") -> bool:
        """Drain the writer and atomically publish the run.

        Shutdown is allowed to wait longer than the 100 Hz path: losing the
        terminal summary would make an otherwise complete run undiscoverable.
        Returns ``True`` only after the final ``.jsonl`` is published.
        """
        with self._lock:
            if self._closed:
                return self.path.exists()
        summary_enqueued = self.emit(
            "runtime_summary",
            mode=self.mode,
            status=status,
            drop_count_total=self.dropped,
            writer_error=self.writer_error,
        )
        with self._lock:
            self._closed = True
        if not summary_enqueued:
            with self._lock:
                self._writer_error = "telemetry queue full before runtime_summary"
            return False
        try:
            self._queue.put(None, timeout=10.0)
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._writer_error = "telemetry queue did not drain within 10 seconds"
            return False
        self._thread.join(timeout=30.0)
        if self._thread.is_alive():
            with self._lock:
                self._writer_error = "telemetry writer did not stop within 30 seconds"
            return False
        if self.writer_error is None:
            os.replace(self.partial_path, self.path)
            return True
        return False

    def _write_loop(self) -> None:
        try:
            with self.partial_path.open("x", encoding="utf-8") as handle:
                pending = 0
                while True:
                    record = self._queue.get()
                    if record is None:
                        break
                    handle.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    pending += 1
                    if pending >= 32 or self._queue.empty():
                        handle.flush()
                        pending = 0
                handle.flush()
        except Exception as exc:  # pragma: no cover - filesystem failure
            with self._lock:
                self._writer_error = f"{type(exc).__name__}: {exc}"

"""Non-blocking disk sidecar and validated feedback-probe contract."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
PROBE_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 1
ALLOWED_FEEDBACK_HZ = {5.0, 10.0, 20.0}


def load_validated_feedback_hz(path: Path | None) -> float:
    """Return an approved Powered rate, or zero for safe command-only mode."""
    if path is None or not path.exists():
        return 0.0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != PROBE_SCHEMA_VERSION:
            return 0.0
        selected = float(payload.get("selected_hz", 0.0))
        if selected not in ALLOWED_FEEDBACK_HZ:
            return 0.0
        result = next(
            item for item in payload.get("results", [])
            if float(item.get("hz", 0.0)) == selected
        )
        return selected if selected >= 10.0 and bool(result.get("passed")) else 0.0
    except (OSError, ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError):
        logger.warning("反馈probe结果无效；保持command-only")
        return 0.0


class JointFeedbackWriter:
    """Write JSONL in a non-GDK worker; submit() never waits for disk."""

    def __init__(self, path: Path, *, run_id: str, capacity: int = 4096) -> None:
        if capacity <= 0:
            raise ValueError("feedback queue capacity must be positive")
        self.path = path
        self.run_id = run_id
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(capacity)
        self._dropped = 0
        self._sequence = 0
        self._closed = False
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._write_loop,
            name="g2-joint-feedback-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def submit(self, **fields: Any) -> bool:
        with self._lock:
            if self._closed:
                return False
            sequence = self._sequence
            self._sequence += 1
            dropped = self._dropped
        record = {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "event": "joint_feedback_sample",
            "wall_time": time.time(),
            "run_id": self.run_id,
            "sequence": sequence,
            "drop_count_before": dropped,
            **fields,
        }
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
        self.submit(
            event="joint_feedback_summary",
            status="closed",
            drop_count_total=self.dropped,
            joints_rad=None,
            valid=[False] * 34,
        )
        with self._lock:
            self._closed = True
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            logger.error("反馈队列关闭时仍满；sidecar不完整，drop=%d", self.dropped)
            return
        self._thread.join(timeout=5.0)

    def _write_loop(self) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                while True:
                    record = self._queue.get()
                    if record is None:
                        break
                    handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
        except Exception:
            logger.exception("34维反馈sidecar写入失败；控制不受影响")

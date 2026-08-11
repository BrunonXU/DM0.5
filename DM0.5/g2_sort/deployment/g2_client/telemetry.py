"""G2客户端的结构化telemetry与延迟分布。

用途：记录请求、动作、观测年龄、跳步、限位、首动作跳变和hold原因。
输入：运行时事件字段、ActionChunk统计及可选JSONL输出路径。
输出：人可读日志、逐事件JSONL和P50/P95/P99/mean延迟汇总。
方法：线程锁保护追加写入；完整动作只进入结构化记录，控制台输出紧凑摘要。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class Telemetry:
    """Emit compact logs and optional JSONL records for post-run inspection."""

    def __init__(self, jsonl_path: Path | None = None) -> None:
        self._path = jsonl_path
        self._lock = threading.Lock()
        self._latencies: list[float] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "event": event,
            "wall_time": time.time(),
            **fields,
        }
        request_id = fields.get("request_id", "-")
        summary = fields.get("summary") or fields.get("reason") or ""
        logger.info("%s request=%s %s", event, request_id, summary)
        if self._path is not None:
            with self._lock:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )

    def record_chunk(
        self,
        *,
        mode: str,
        request_id: int,
        observation_at: float,
        request_started_at: float,
        response_received_at: float,
        latency_seconds: float,
        observation_age_seconds: float,
        actions: np.ndarray,
        used_start_index: int | None,
        used_intervals: int,
        skipped_steps: int,
        clip_count: int,
        max_clip_delta: float,
        start_jump: float | None,
    ) -> None:
        self._latencies.append(float(latency_seconds))
        array = np.asarray(actions, dtype=np.float64)
        self.emit(
            "action_chunk",
            mode=mode,
            request_id=request_id,
            observation_at_monotonic=float(observation_at),
            request_started_at_monotonic=float(request_started_at),
            response_received_at_monotonic=float(response_received_at),
            latency_seconds=float(latency_seconds),
            observation_age_seconds=float(observation_age_seconds),
            shape=list(array.shape),
            action_min=float(np.min(array)),
            action_max=float(np.max(array)),
            used_start_index=used_start_index,
            used_intervals=used_intervals,
            skipped_steps=skipped_steps,
            limit_clip_count=clip_count,
            max_limit_clip_delta=max_clip_delta,
            max_start_jump=start_jump,
            actions=array.tolist(),
            summary=(
                f"rtt={latency_seconds:.3f}s age={observation_age_seconds:.3f}s "
                f"skip={skipped_steps} use={used_intervals} "
                f"jump={start_jump if start_jump is not None else 'n/a'} "
                f"clips={clip_count}"
            ),
        )

    def latency_summary(self) -> dict[str, float | int]:
        if not self._latencies:
            return {"count": 0}
        values = np.asarray(self._latencies, dtype=np.float64)
        return {
            "count": int(len(values)),
            "p50_seconds": float(np.percentile(values, 50)),
            "p95_seconds": float(np.percentile(values, 95)),
            "p99_seconds": float(np.percentile(values, 99)),
            "mean_seconds": float(np.mean(values)),
        }

    def finish(self, mode: str) -> dict[str, float | int]:
        summary = self.latency_summary()
        self.emit("run_summary", mode=mode, latency=summary, summary=str(summary))
        return summary

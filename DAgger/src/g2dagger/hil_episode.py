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


ACTION_DIM = 34
CHUNK_STEPS = 50
CAMERA_NAMES = ("head", "left_wrist", "right_wrist")
SCHEMA_VERSION = 2
STREAMS = ("observations", "policy_chunks", "control", "events")


def _vector(values: Sequence[float], label: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.shape != (ACTION_DIM,) or not np.all(np.isfinite(array)):
        raise ValueError(f"{label} must be finite shape ({ACTION_DIM},)")
    return array.copy()


def _optional_vector(
    values: Sequence[float] | None, label: str
) -> np.ndarray | None:
    return None if values is None else _vector(values, label)


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


@dataclass(frozen=True)
class CameraFrameRef:
    """Reference to a separately persisted camera frame."""

    stream: str
    frame_index: int
    timestamp_ns: int

    def __post_init__(self) -> None:
        if not self.stream:
            raise ValueError("camera stream must be non-empty")
        if self.frame_index < 0 or self.timestamp_ns < 0:
            raise ValueError("camera frame index/timestamp must be non-negative")


class HILEpisodeRecorder:
    """Asynchronously write four append-only JSONL streams.

    A run is first visible as ``<episode>.partial``.  Only a clean close with
    no queue drops atomically publishes ``<episode>``.  Array conversion and
    JSON serialization happen in the writer thread, outside a future 100 Hz
    control caller.
    """

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
                "schema_version": SCHEMA_VERSION, "episode_id": self.episode_id,
                "stream_sequence": sequence, **record,
            }
            try:
                self._queue.put_nowait((stream, envelope))
                return True
            except queue.Full:
                self._drops += 1
                return False

    def record_timestamped_observation(self, observation, *, hil_state, intervention_id, policy_epoch):
        """Reference the same immutable acquisition sent to the payload sink.

        The archive resolves observation ID + camera, never an inferred video
        frame number. This method does not reacquire or duplicate payload bytes.
        """
        metadata = observation.metadata()
        return self._emit("observations", {
            "event": "observation", "monotonic_ns": observation.capture_started_monotonic_ns,
            "request_id": observation.observation_id,
            "observation_id": observation.observation_id,
            "payload_observation_id": observation.observation_id,
            "measured_34d": observation.measured_34d,
            "measured_valid_34d": observation.measured_valid_34d,
            "acquisition": metadata, "hil_state": hil_state,
            "intervention_id": intervention_id, "policy_epoch": policy_epoch,
            "camera_refs": {f.camera: {
                "stream": "native-payload/" + f.camera, "frame_index": f.frame_id,
                "reference_kind": "source_frame_id",
                "timestamp_ns": f.host_received_monotonic_ns,
                "timestamp_clock_domain": "host_monotonic",
            } for f in observation.frames},
        })

    def record_observation(
        self,
        *,
        monotonic_ns: int,
        request_id: int,
        measured_34d: Sequence[float],
        camera_refs: Mapping[str, CameraFrameRef],
        hil_state: str,
        intervention_id: int,
        policy_epoch: int,
    ) -> bool:
        if monotonic_ns < 0 or request_id < 0:
            raise ValueError("observation time/request ID must be non-negative")
        if set(camera_refs) != set(CAMERA_NAMES):
            raise ValueError(f"camera_refs must contain exactly {CAMERA_NAMES}")
        cameras = {
            name: {
                "stream": camera_refs[name].stream,
                "frame_index": camera_refs[name].frame_index,
                "timestamp_ns": camera_refs[name].timestamp_ns,
            }
            for name in CAMERA_NAMES
        }
        return self._emit(
            "observations",
            {
                "event": "observation",
                "monotonic_ns": monotonic_ns,
                "request_id": request_id,
                "observation_id": request_id,
                "measured_34d": _optional_vector(measured_34d, "measured_34d"),
                "camera_refs": cameras,
                "hil_state": str(hil_state),
                "intervention_id": int(intervention_id),
                "policy_epoch": int(policy_epoch),
            },
        )

    def record_policy_chunk(
        self,
        *,
        policy_epoch: int,
        request_id: int,
        observation_request_id: int,
        observation_monotonic_ns: int,
        response_monotonic_ns: int,
        request_monotonic_ns: int | None = None,
        actions_50x34: Sequence[Sequence[float]],
    ) -> bool:
        actions = np.asarray(actions_50x34, dtype=np.float64)
        if actions.shape != (CHUNK_STEPS, ACTION_DIM) or not np.all(
            np.isfinite(actions)
        ):
            raise ValueError("actions_50x34 must be finite shape (50, 34)")
        if min(policy_epoch, request_id, observation_request_id) < 0:
            raise ValueError("policy IDs must be non-negative")
        if observation_monotonic_ns < 0 or response_monotonic_ns < observation_monotonic_ns:
            raise ValueError("invalid policy observation/response timestamps")
        return self._emit(
            "policy_chunks",
            {
                "event": "policy_chunk",
                "policy_epoch": policy_epoch,
                "request_id": request_id,
                "observation_request_id": observation_request_id,
                "observation_monotonic_ns": observation_monotonic_ns,
                "response_monotonic_ns": response_monotonic_ns,
                "request_monotonic_ns": observation_monotonic_ns if request_monotonic_ns is None else request_monotonic_ns,
                "actions_50x34": actions.copy(),
            },
        )

    def record_control_tick(
        self,
        *,
        monotonic_ns: int,
        control_sequence: int,
        hil_state: str,
        control_source: str,
        intervention_id: int,
        policy_epoch: int,
        policy_request_id: int | None,
        policy_action_index: int | None,
        intervention_mask: bool,
        human_arm_mask: bool,
        human_hand_mask: bool,
        hold_34d: Sequence[float],
        policy_proposal_34d: Sequence[float] | None,
        human_candidate_34d: Sequence[float] | None,
        selected_command_34d: Sequence[float],
        executed_command_34d: Sequence[float] | None,
        measured_34d: Sequence[float] | None,
        submitted_command_34d: Sequence[float] | None = None,
        sdk_calls: Sequence[Mapping[str, Any]] = (),
        evidence_kind: str = "unverified",
        policy_sampling: Mapping[str, Any] | None = None,
    ) -> bool:
        if monotonic_ns < 0 or control_sequence < 0:
            raise ValueError("control time/sequence must be non-negative")
        return self._emit(
            "control",
            {
                "event": "control_tick",
                "monotonic_ns": monotonic_ns,
                "control_sequence": control_sequence,
                "hil_state": str(hil_state),
                "control_source": str(control_source),
                "intervention_id": int(intervention_id),
                "policy_epoch": int(policy_epoch),
                "policy_request_id": policy_request_id,
                "policy_action_index": policy_action_index,
                "intervention_mask": bool(intervention_mask),
                "human_arm_mask": bool(human_arm_mask),
                "human_hand_mask": bool(human_hand_mask),
                "hold_34d": _vector(hold_34d, "hold_34d"),
                "policy_proposal_34d": _optional_vector(
                    policy_proposal_34d, "policy_proposal_34d"
                ),
                "human_candidate_34d": _optional_vector(
                    human_candidate_34d, "human_candidate_34d"
                ),
                "selected_command_34d": _vector(
                    selected_command_34d, "selected_command_34d"
                ),
                "submitted_command_34d": _optional_vector(submitted_command_34d, "submitted"),
                "sdk_calls": _jsonable(sdk_calls),
                "policy_sampling": _jsonable(policy_sampling),
                "evidence_kind": evidence_kind,
                "executed_command_34d": _optional_vector(
                    executed_command_34d, "executed_command_34d"
                ),
                "measured_34d": _optional_vector(measured_34d, "measured_34d"),
            },
        )

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
            "schema_version": SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "task_prompt": self.task_prompt,
            "status": status,
            "complete": clean,
            "structurally_auditable": clean,
            "training_ready": False,
            "training_blockers": [
                "payload persistence and journal linkage require separate audit",
                "device clock mapping and 34D feedback/label alignment unverified",
                "training video/timestamp conversion not certified",
            ],
            "action_contract": {"dimension": ACTION_DIM, "chunk_steps": CHUNK_STEPS},
            "counts": dict(self._counts),
            "drop_count": self.drops,
            "writer_error": self.writer_error,
            "started_wall_ns": self._started_wall_ns,
            "closed_wall_ns": time.time_ns(),
        }
        manifest_path = self.partial_path / "manifest.json"
        temporary = self.partial_path / ".manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, manifest_path)
        if clean:
            os.replace(self.partial_path, self.path)
            return True
        return False

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


def _audit_hil_episode(episode_dir: Path) -> dict[str, Any]:
    """Audit decisions and causal references; SDK evidence is distinct from motion."""

    root = Path(episode_dir)
    errors: list[str] = []
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"valid": False, "training_ready": False, "errors": ["missing manifest"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"valid": False, "training_ready": False, "errors": [f"invalid manifest: {exc}"]}
    if manifest.get("drop_count") or manifest.get("writer_error"):
        errors.append("journal reports dropped records or writer error")
    if not manifest.get("complete"):
        errors.append("manifest is not complete")
    records: dict[str, list[dict[str, Any]]] = {}
    for stream in STREAMS:
        path = root / f"{stream}.jsonl"
        if not path.exists():
            errors.append(f"missing {path.name}")
            records[stream] = []
            continue
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append(f"invalid {path.name}: {exc}")
            rows = []
        records[stream] = rows
        if [row.get("stream_sequence") for row in rows] != list(range(len(rows))):
            errors.append(f"{stream} stream_sequence is not contiguous")
        if len(rows) != manifest.get("counts", {}).get(stream):
            errors.append(f"{stream} count differs from manifest")

    for stream in ("observations", "control", "events"):
        times = [row.get("monotonic_ns") for row in records[stream]]
        if any(not isinstance(value, int) or value < 0 for value in times):
            errors.append(f"{stream} contains invalid monotonic_ns")
        elif any(right < left for left, right in zip(times, times[1:])):
            errors.append(f"{stream} monotonic_ns regressed")

    for required in ("observations", "policy_chunks", "control"):
        if not records[required]:
            errors.append(f"{required} stream is empty")

    camera_times = {name: [] for name in CAMERA_NAMES}
    for row in records["observations"]:
        try:
            if "acquisition" not in row:
                _vector(row["measured_34d"], "measured_34d")
            elif len(row["measured_34d"]) != 34 or len(row["measured_valid_34d"]) != 34:
                raise ValueError("invalid timestamped feedback dimensions")
            refs = row["camera_refs"]
            if set(refs) != set(CAMERA_NAMES):
                raise ValueError("camera reference set mismatch")
            for name in CAMERA_NAMES:
                ref = refs[name]
                if not isinstance(ref.get("stream"), str) or not ref["stream"]:
                    raise ValueError(f"{name} stream is empty")
                if not isinstance(ref.get("frame_index"), int) or ref["frame_index"] < 0:
                    raise ValueError(f"{name} frame_index is invalid")
                if not isinstance(ref.get("timestamp_ns"), int) or ref["timestamp_ns"] < 0:
                    raise ValueError(f"{name} timestamp_ns is invalid")
                camera_times[name].append(ref["timestamp_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid observation {row.get('stream_sequence')}: {exc}")
    for name, times in camera_times.items():
        if any(right < left for left, right in zip(times, times[1:])):
            errors.append(f"{name} camera timestamp regressed")

    for row in records["policy_chunks"]:
        actions = np.asarray(row.get("actions_50x34"), dtype=np.float64)
        if actions.shape != (CHUNK_STEPS, ACTION_DIM) or not np.all(np.isfinite(actions)):
            errors.append(f"invalid policy chunk {row.get('stream_sequence')}")

    from .hil_causality import audit_causality
    errors.extend(audit_causality(records, manifest))

    ownership_counts: dict[str, int] = {}
    for row in records["control"]:
        try:
            hold = _vector(row["hold_34d"], "hold_34d")
            policy = _optional_vector(row.get("policy_proposal_34d"), "policy")
            human = _optional_vector(row.get("human_candidate_34d"), "human")
            selected = _vector(row["selected_command_34d"], "selected")
            _optional_vector(row.get("executed_command_34d"), "executed")
            _optional_vector(row.get("submitted_command_34d"), "submitted")
            _optional_vector(row.get("measured_34d"), "measured")
            source = str(row["control_source"])
            expected = (
                policy
                if source == "policy"
                else human
                if (source.startswith("human_") and source != "human_hold") or source == "home"
                else hold
            )
            if expected is None or not np.array_equal(selected, expected):
                raise ValueError(f"selected command disagrees with source={source}")
            intervention = bool(row["intervention_mask"])
            arm_mask = bool(row["human_arm_mask"])
            hand_mask = bool(row["human_hand_mask"])
            if source == "policy" and (intervention or arm_mask or hand_mask):
                raise ValueError("policy source carries a human intervention mask")
            if source == "human_arm" and not (intervention and arm_mask):
                raise ValueError("human_arm source is missing its masks")
            if source == "human_hand" and not (intervention and hand_mask):
                raise ValueError("human_hand source is missing its masks")
            if source == "human_arm_and_hand" and not (
                intervention and arm_mask and hand_mask
            ):
                raise ValueError("human_arm_and_hand source is missing its masks")
            if source == "human_hold" and not intervention:
                raise ValueError("human_hold source is missing intervention_mask")
            ownership_counts[source] = ownership_counts.get(source, 0) + 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"invalid control tick {row.get('stream_sequence')}: {exc}")

    return {
        "episode_id": manifest.get("episode_id"),
        "valid": not errors,
        "training_ready": False,
        "errors": errors,
        "training_blockers": manifest.get("training_blockers", []),
        "counts": {stream: len(records[stream]) for stream in STREAMS},
        "ownership_counts": ownership_counts,
    }


def audit_hil_episode(episode_dir: Path) -> dict[str, Any]:
    """Fail closed on malformed journals as well as causal violations."""
    try:
        return _audit_hil_episode(episode_dir)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        return {"valid": False, "training_ready": False, "errors": [f"malformed journal: {exc}"]}

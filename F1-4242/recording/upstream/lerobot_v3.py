"""Direct, append-only LeRobot v3.0 dataset writer for F1 recordings."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Mapping

import numpy as np

from recording.upstream.session_pose import ARM_JOINT_NAMES as JOINT_MOTOR_NAMES, GRIPPER_NAMES


CAMERA_KEYS = (
    "observation.images.base_0_rgb",
    "observation.images.left_wrist_0_rgb",
    "observation.images.right_wrist_0_rgb",
)
CAMERA_INTEGRITY_BASIS = {
    CAMERA_KEYS[0]: "source",
    CAMERA_KEYS[1]: "source",
    CAMERA_KEYS[2]: "source",
}
CHASSIS_NAMES = ()  # Upper-body contract excludes head, body and base.
FEATURE_NAMES = tuple(JOINT_MOTOR_NAMES) + tuple(GRIPPER_NAMES) + CHASSIS_NAMES
FEATURE_SIZE = len(FEATURE_NAMES)


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Return JPEG width/height without adding an image-decoding dependency."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("camera payload is not a JPEG")
    index = 2
    sof = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in sof:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            if width <= 0 or height <= 0:
                break
            return width, height
        index += length
    raise ValueError("JPEG dimensions are unavailable")


def feature_stats(values: np.ndarray) -> dict[str, list[float] | list[int]]:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim == 1:
        array = array[:, None]
    if array.ndim != 2 or not len(array) or not np.all(np.isfinite(array)):
        raise ValueError("statistics require a non-empty finite 2D array")
    result: dict[str, list[float] | list[int]] = {
        "min": np.min(array, axis=0).tolist(),
        "max": np.max(array, axis=0).tolist(),
        "mean": np.mean(array, axis=0).tolist(),
        "std": np.std(array, axis=0).tolist(),
        "count": [int(len(array))],
    }
    for name, q in (("q01", .01), ("q10", .10), ("q50", .50), ("q90", .90), ("q99", .99)):
        result[name] = np.quantile(array, q, axis=0).tolist()
    return result


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _fixed_list(values: np.ndarray, size: int):
    import pyarrow as pa

    array = np.asarray(values, dtype=np.float32)
    return pa.FixedSizeListArray.from_arrays(
        pa.array(array.reshape(-1), type=pa.float32()), size
    )


def _read_episode_rows(root: Path) -> list[dict]:
    import pyarrow.parquet as pq

    rows: list[dict] = []
    for path in sorted((root / "meta" / "episodes").glob("chunk-*/*.parquet")):
        rows.extend(pq.read_table(path).to_pylist())
    return sorted(rows, key=lambda row: int(row["episode_index"]))


def _merge_stats(rows: list[dict], feature: str) -> dict[str, list[float] | list[int]]:
    if not rows:
        raise ValueError("cannot merge empty episode statistics")
    counts = np.asarray([row[f"stats/{feature}/count"][0] for row in rows], dtype=np.float64)
    means = np.asarray([row[f"stats/{feature}/mean"] for row in rows], dtype=np.float64)
    stds = np.asarray([row[f"stats/{feature}/std"] for row in rows], dtype=np.float64)
    total = float(np.sum(counts))
    mean = np.sum(means * counts[:, None], axis=0) / total
    second = np.sum((stds * stds + means * means) * counts[:, None], axis=0) / total
    variance = np.maximum(0.0, second - mean * mean)
    result: dict[str, list[float] | list[int]] = {
        "min": np.min(np.asarray([row[f"stats/{feature}/min"] for row in rows]), axis=0).tolist(),
        "max": np.max(np.asarray([row[f"stats/{feature}/max"] for row in rows]), axis=0).tolist(),
        "mean": mean.tolist(),
        "std": np.sqrt(variance).tolist(),
        "count": [int(total)],
    }
    # Match LeRobot v3's aggregate_stats semantics. Episode quantiles are
    # weighted by frame count so every append keeps the normalization contract
    # complete without rereading the full dataset.
    for name in ("q01", "q10", "q50", "q90", "q99"):
        quantiles = np.asarray(
            [row[f"stats/{feature}/{name}"] for row in rows], dtype=np.float64
        )
        result[name] = np.sum(quantiles * counts[:, None], axis=0).tolist()
        result[name] = (np.asarray(result[name]) / total).tolist()
    return result


class LeRobotV3Dataset:
    """Append one operator-approved episode per Parquet/video file."""

    def __init__(self, root: Path, fps: float, task: str, max_source_gap_ns=55_000_000, source_basis="packet_gid") -> None:
        self.root = Path(root).resolve()
        self.fps = float(fps)
        self.max_source_gap_ns = int(max_source_gap_ns)
        self.source_basis = source_basis
        self.task = str(task).strip()
        if not math.isfinite(self.fps) or self.fps <= 0.0 or not self.task:
            raise ValueError("dataset fps/task is invalid")
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _schema():
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("observation.state", pa.list_(pa.float32(), FEATURE_SIZE)),
                pa.field("action", pa.list_(pa.float32(), FEATURE_SIZE)),
                pa.field("source.action_timestamp_ns", pa.list_(pa.int64(), FEATURE_SIZE)),
                pa.field("source.action_age_ns", pa.list_(pa.int64(), FEATURE_SIZE)),
                pa.field("source.action_held", pa.list_(pa.bool_(), FEATURE_SIZE)),
                pa.field("source.gripper_writer_gid", pa.list_(pa.string(), 2)),
                pa.field("source.gripper_epoch", pa.list_(pa.int64(), 2)),
                pa.field("source.timestamp_ns", pa.int64()),
                pa.field("source.camera_timestamp_ns", pa.list_(pa.int64(), 3)),
                pa.field(
                    "source.camera_integrity_timestamp_ns", pa.list_(pa.int64(), 3)
                ),
                pa.field("source.capture_monotonic_ns", pa.int64()),
                pa.field(
                    "source.camera_arrival_timestamp_ns", pa.list_(pa.int64(), 3)
                ),
                pa.field("source.state_max_age_ns", pa.int64()),
                pa.field("source.action_max_age_ns", pa.int64()),
                pa.field("timestamp", pa.float32()),
                pa.field("frame_index", pa.int64()),
                pa.field("episode_index", pa.int64()),
                pa.field("index", pa.int64()),
                pa.field("task_index", pa.int64()),
            ]
        )

    def _table(self, arrays: Mapping[str, np.ndarray], episode: int, global_index: int):
        import pyarrow as pa

        state = np.asarray(arrays["state"], dtype=np.float32)
        action = np.asarray(arrays["action"], dtype=np.float32)
        timestamps = np.asarray(arrays["timestamp_ns"], dtype=np.int64)
        camera_timestamps = np.asarray(arrays["camera_timestamp_ns"], dtype=np.int64)
        camera_integrity_timestamps = np.asarray(
            arrays["camera_integrity_timestamp_ns"], dtype=np.int64
        )
        capture_monotonic = np.asarray(
            arrays["capture_monotonic_ns"], dtype=np.int64
        )
        camera_arrivals = np.asarray(
            arrays["camera_arrival_timestamp_ns"], dtype=np.int64
        )
        state_age = np.asarray(arrays["state_max_age_ns"], dtype=np.int64)
        action_age = np.asarray(arrays["action_max_age_ns"], dtype=np.int64)
        length = len(timestamps)
        if state.shape != (length, FEATURE_SIZE) or action.shape != (length, FEATURE_SIZE):
            raise ValueError("state/action shape does not match the 16D F1-4242 upper-body contract")
        for key in ("action_source_timestamp_ns", "action_source_age_ns", "action_held"):
            if np.asarray(arrays[key]).shape != (length, FEATURE_SIZE):
                raise ValueError("per-dimension action provenance shape mismatch")
        age = np.asarray(arrays["action_source_age_ns"])
        stamp = np.asarray(arrays["action_source_timestamp_ns"])
        held = np.asarray(arrays["action_held"])
        if not np.all(np.isfinite(state)) or not np.all(np.isfinite(action)):
            raise ValueError("state/action must be finite real observations")
        if np.any(age < 0) or not np.array_equal(timestamps[:, None] - stamp, age):
            raise ValueError("action receipt provenance is not causal")
        if np.any(age[:, :14] > 50_000_000) or np.any(held[:, :14]):
            raise ValueError("arm commands cannot be held or stale")
        if np.any(held[:, 14:] != (age[:, 14:] > 50_000_000)):
            raise ValueError("gripper held markers do not match actual source age")
        if np.asarray(arrays["gripper_writer_gid"]).shape != (length, 2) or np.asarray(arrays["gripper_source_epoch"]).shape != (length, 2):
            raise ValueError("gripper writer provenance shape mismatch")
        if any(not value or not any(bytes.fromhex(value)) for value in np.asarray(arrays["gripper_writer_gid"]).reshape(-1)):
            raise ValueError("gripper writer GID is missing")
        if camera_timestamps.shape != (length, 3):
            raise ValueError("camera timestamps must have shape [frames, 3]")
        if camera_integrity_timestamps.shape != (length, 3):
            raise ValueError(
                "camera integrity timestamps must have shape [frames, 3]"
            )
        if capture_monotonic.shape != (length,):
            raise ValueError("capture monotonic timestamps must match frames")
        if camera_arrivals.shape != (length, 3):
            raise ValueError("camera arrival timestamps must have shape [frames, 3]")
        if state_age.shape != (length,) or action_age.shape != (length,):
            raise ValueError("state/action age arrays must match frames")
        if length < 2 or not np.all(np.diff(timestamps) > 0):
            raise ValueError("episode requires at least two strictly increasing frames")
        return pa.Table.from_arrays(
            [
                _fixed_list(state, FEATURE_SIZE),
                _fixed_list(action, FEATURE_SIZE),
                pa.array(np.asarray(arrays["action_source_timestamp_ns"], dtype=np.int64).tolist(), type=pa.list_(pa.int64(), FEATURE_SIZE)),
                pa.array(np.asarray(arrays["action_source_age_ns"], dtype=np.int64).tolist(), type=pa.list_(pa.int64(), FEATURE_SIZE)),
                pa.array(np.asarray(arrays["action_held"], dtype=np.bool_).tolist(), type=pa.list_(pa.bool_(), FEATURE_SIZE)),
                pa.array(np.asarray(arrays["gripper_writer_gid"]).tolist(), type=pa.list_(pa.string(), 2)),
                pa.array(np.asarray(arrays["gripper_source_epoch"], dtype=np.int64).tolist(), type=pa.list_(pa.int64(), 2)),
                pa.array(timestamps, type=pa.int64()),
                pa.FixedSizeListArray.from_arrays(
                    pa.array(camera_timestamps.reshape(-1), type=pa.int64()), 3
                ),
                pa.FixedSizeListArray.from_arrays(
                    pa.array(
                        camera_integrity_timestamps.reshape(-1), type=pa.int64()
                    ),
                    3,
                ),
                pa.array(capture_monotonic, type=pa.int64()),
                pa.FixedSizeListArray.from_arrays(
                    pa.array(camera_arrivals.reshape(-1), type=pa.int64()), 3
                ),
                pa.array(state_age, type=pa.int64()),
                pa.array(action_age, type=pa.int64()),
                pa.array(np.arange(length, dtype=np.float32) / self.fps, type=pa.float32()),
                pa.array(np.arange(length, dtype=np.int64), type=pa.int64()),
                pa.array(np.full(length, episode, dtype=np.int64), type=pa.int64()),
                pa.array(np.arange(global_index, global_index + length, dtype=np.int64), type=pa.int64()),
                pa.array(np.zeros(length, dtype=np.int64), type=pa.int64()),
            ],
            schema=self._schema(),
        )

    def _features(self, camera_info: Mapping[str, Mapping[str, object]]) -> dict:
        features = {
            "observation.state": {"dtype": "float32", "shape": [FEATURE_SIZE], "names": list(FEATURE_NAMES)},
            "action": {"dtype": "float32", "shape": [FEATURE_SIZE], "names": list(FEATURE_NAMES)},
            "source.timestamp_ns": {"dtype": "int64", "shape": [1], "names": None},
            "source.camera_timestamp_ns": {"dtype": "int64", "shape": [3], "names": list(CAMERA_KEYS)},
            "source.camera_integrity_timestamp_ns": {
                "dtype": "int64", "shape": [3], "names": list(CAMERA_KEYS)
            },
            "source.capture_monotonic_ns": {
                "dtype": "int64", "shape": [1], "names": None
            },
            "source.camera_arrival_timestamp_ns": {
                "dtype": "int64", "shape": [3], "names": list(CAMERA_KEYS)
            },
            "source.state_max_age_ns": {
                "dtype": "int64", "shape": [1], "names": None
            },
            "source.action_max_age_ns": {
                "dtype": "int64", "shape": [1], "names": None
            },
        }
        for key, dtype, size in (("source.action_timestamp_ns", "int64", FEATURE_SIZE),
                                 ("source.action_age_ns", "int64", FEATURE_SIZE),
                                 ("source.action_held", "bool", FEATURE_SIZE),
                                 ("source.gripper_writer_gid", "string", 2),
                                 ("source.gripper_epoch", "int64", 2)):
            features[key] = {"dtype": dtype, "shape": [size], "names": list(FEATURE_NAMES) if size == FEATURE_SIZE else list(GRIPPER_NAMES)}
        for key in CAMERA_KEYS:
            info = camera_info[key]
            width, height = int(info["width"]), int(info["height"])
            features[key] = {
                "dtype": "video",
                "shape": [height, width, 3],
                "names": ["height", "width", "channels"],
                "info": {
                    "video.height": height,
                    "video.width": width,
                    "video.codec": "h264",
                    "video.pix_fmt": "yuv420p",
                    "video.is_depth_map": False,
                    "video.fps": self.fps,
                    "video.channels": 3,
                    "has_audio": False,
                },
            }
        for key, dtype in (
            ("timestamp", "float32"), ("frame_index", "int64"),
            ("episode_index", "int64"), ("index", "int64"), ("task_index", "int64"),
        ):
            features[key] = {"dtype": dtype, "shape": [1], "names": None}
        return features

    def commit(
        self,
        staging: Path,
        arrays: Mapping[str, np.ndarray],
        camera_info: Mapping[str, Mapping[str, object]],
    ) -> tuple[int, Path]:
        """Atomically expose one complete episode after its videos are validated."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        staging = Path(staging).resolve()
        expected_parent = (self.root / ".staging").resolve()
        if expected_parent not in staging.parents:
            raise ValueError("staging directory is outside the dataset")
        lock_path = self.root / ".dataset.lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            capture_id = staging.name
            transactions = self.root / ".transactions"
            transactions.mkdir(parents=True, exist_ok=True)
            journal_path = transactions / (capture_id + ".json")
            payload_hash = hashlib.sha256()
            payload_hash.update(json.dumps({"task":self.task,"fps":self.fps,"contract":"f1_upper_16d_v1","max_source_gap_ns":self.max_source_gap_ns,"source_basis":self.source_basis},sort_keys=True).encode())
            for key in sorted(arrays):
                array = np.asarray(arrays[key])
                payload_hash.update(json.dumps([key,str(array.dtype),list(array.shape)]).encode())
                payload_hash.update(array.tobytes())
            payload_hash.update(json.dumps(camera_info, sort_keys=True).encode())
            signature = payload_hash.hexdigest()
            journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
            if journal and journal["payload_sha256"] != signature:
                raise ValueError("capture payload changed during commit retry")
            if journal and journal.get("status") == "committed":
                retained = self.root / "source" / f"episode-{int(journal['episode']):06d}"
                retained.parent.mkdir(parents=True, exist_ok=True)
                if staging.exists() and not retained.exists(): staging.rename(retained)
                return int(journal["episode"]), self.root / journal["data_path"]
            unfinished = [p for p in transactions.glob("*.json") if p != journal_path and json.loads(p.read_text()).get("status") != "committed"]
            if unfinished:
                raise ValueError("an earlier capture commit is unfinished; retry it before adding another")
            prior = _read_episode_rows(self.root)
            if journal:
                episode = int(journal["episode"])
                if any(int(row["episode_index"]) > episode for row in prior):
                    raise ValueError("commit ordering changed during retry")
                prior = [row for row in prior if int(row["episode_index"]) < episode]
            else:
                episode = 0 if not prior else int(prior[-1]["episode_index"]) + 1
                journal = {"capture_id": capture_id, "payload_sha256": signature, "episode": episode, "status": "pending"}
            global_index = sum(int(row["length"]) for row in prior)
            chunk, file_index = divmod(episode, 1000)
            table = self._table(arrays, episode, global_index)
            length = table.num_rows

            old_info_path = self.root / "meta" / "info.json"
            if old_info_path.exists():
                old = json.loads(old_info_path.read_text(encoding="utf-8"))
                if old.get("codebase_version") != "v3.0" or float(old.get("fps", -1)) != self.fps:
                    raise ValueError("existing dataset version/fps is incompatible")
                tasks_path = self.root / "meta" / "tasks.parquet"
                if tasks_path.exists() and [row["task"] for row in pq.read_table(tasks_path).to_pylist()] != [self.task]:
                    raise ValueError("task text cannot change inside an existing dataset")
                if old.get("source_identity_basis", "packet_gid") != self.source_basis:
                    raise ValueError("source identity proof cannot change within one dataset")
                if old.get("features") != self._features(camera_info):
                    raise ValueError("camera or F1 feature contract changed within the dataset")

            alignment = staging / "alignment"
            from recording.upstream.alignment_archive import read_extras, validate_coverage
            extras = read_extras(alignment)
            validate_coverage(arrays["camera_timestamp_ns"], extras, self.max_source_gap_ns)
            for key in CAMERA_KEYS:
                source = staging / f"{key}.mp4"
                if not source.is_file() or source.stat().st_size <= 0:
                    raise ValueError(f"missing staged video: {key}")
            _atomic_json(journal_path, journal)
            final_videos: dict[str, Path] = {}
            if alignment.exists():
                target_alignment = self.root / "alignment" / f"episode-{episode:06d}"
                # A retry after an interrupted commit may already have copied
                # the same files. The staging episode remains authoritative.
                shutil.copytree(alignment, target_alignment, dirs_exist_ok=True)
            for key in CAMERA_KEYS:
                source = staging / f"{key}.mp4"
                if not source.is_file() or source.stat().st_size <= 0:
                    raise ValueError(f"missing staged video: {key}")
                target = self.root / "videos" / key / f"chunk-{chunk:03d}" / f"file-{file_index:03d}.mp4"
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp.mp4")
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
                final_videos[key] = target

            data_path = self.root / "data" / f"chunk-{chunk:03d}" / f"file-{file_index:03d}.parquet"
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_tmp = data_path.with_suffix(".tmp.parquet")
            pq.write_table(table, data_tmp, compression="snappy", row_group_size=length)
            os.replace(data_tmp, data_path)

            stats = {
                "observation.state": feature_stats(np.asarray(arrays["state"])),
                "action": feature_stats(np.asarray(arrays["action"])),
            }
            row: dict[str, object] = {
                "episode_index": episode,
                "meta/episodes/chunk_index": chunk,
                "meta/episodes/file_index": file_index,
                "tasks": [self.task],
                "length": length,
                "data/chunk_index": chunk,
                "data/file_index": file_index,
                "dataset_from_index": global_index,
                "dataset_to_index": global_index + length,
            }
            for key in CAMERA_KEYS:
                row[f"videos/{key}/chunk_index"] = chunk
                row[f"videos/{key}/file_index"] = file_index
                row[f"videos/{key}/from_timestamp"] = 0.0
                row[f"videos/{key}/to_timestamp"] = length / self.fps
            for feature, values in stats.items():
                for name, value in values.items():
                    row[f"stats/{feature}/{name}"] = value

            episode_path = self.root / "meta" / "episodes" / f"chunk-{chunk:03d}" / f"file-{file_index:03d}.parquet"
            episode_path.parent.mkdir(parents=True, exist_ok=True)
            episode_tmp = episode_path.with_suffix(".tmp.parquet")
            pq.write_table(pa.Table.from_pylist([row]), episode_tmp, compression="snappy")
            os.replace(episode_tmp, episode_path)

            all_rows = prior + [row]
            total_frames = global_index + length
            info = {
                "codebase_version": "v3.0",
                "robot_type": "f1_09_upper16_omnipicker",
                "f1_contract": "f1_upper_16d_v1",
                "source_identity_basis": self.source_basis,
                "action_source": "motion_ctl/joint_ctl + motion_ctl/gripper/{left,right}",
                "action_fallback": "forbidden",
                "training_accepted": False,
                "total_episodes": len(all_rows),
                "total_frames": total_frames,
                "total_tasks": 1,
                "chunks_size": 1000,
                "data_files_size_in_mb": 128,
                "video_files_size_in_mb": 512,
                "fps": self.fps,
                "camera_integrity_basis": CAMERA_INTEGRITY_BASIS,
                "camera_sync_basis": "source_ros_system_time",
                "splits": {"train": f"0:{len(all_rows)}"},
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": self._features(camera_info),
            }
            tasks = pa.table(
                {"task_index": pa.array([0], type=pa.int64()), "task": [self.task]}
            )
            tasks_path = self.root / "meta" / "tasks.parquet"
            tasks_path.parent.mkdir(parents=True, exist_ok=True)
            tasks_tmp = tasks_path.with_suffix(".tmp.parquet")
            pq.write_table(tasks, tasks_tmp)
            os.replace(tasks_tmp, tasks_path)
            global_stats = {
                feature: _merge_stats(all_rows, feature)
                for feature in ("observation.state", "action")
            }
            _atomic_json(self.root / "meta" / "stats.json", global_stats)
            _atomic_json(self.root / "meta" / "info.json", info)
            _atomic_json(journal_path, dict(journal, status="committed", data_path=str(data_path.relative_to(self.root))))
            # Preserve the source journal/arrays for independent recovery and provenance.
            retained = self.root / "source" / f"episode-{episode:06d}"
            retained.parent.mkdir(parents=True, exist_ok=True)
            if staging.exists() and not retained.exists():
                staging.rename(retained)
            return episode, data_path

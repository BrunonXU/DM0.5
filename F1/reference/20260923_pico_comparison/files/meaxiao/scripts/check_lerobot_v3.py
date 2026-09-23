#!/usr/bin/env python3
"""Read-only structural and media audit for the local F1 LeRobot v3 dataset."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

import numpy as np
import pyarrow.parquet as pq

from pico_f1.config import load_control_config
from pico_f1.lerobot_v3 import CAMERA_INTEGRITY_BASIS, CAMERA_KEYS, FEATURE_SIZE, FEATURE_NAMES, LeRobotV3Dataset


def startup_structure(root, info, episodes, data_files, errors):
    """Append compatibility only: metadata, Parquet footers and file presence.

    Deliberately does NOT claim decoded media or row-level timing integrity.
    Full audit remains the default CLI mode and recording validation is unchanged.
    """
    expected_data = set()
    expected_videos = {key: set() for key in CAMERA_KEYS}
    total = 0
    for index, row in enumerate(episodes):
        length = int(row["length"])
        if (int(row["episode_index"]) != index or length <= 0
                or int(row["dataset_from_index"]) != total
                or int(row["dataset_to_index"]) != total + length):
            errors.append(f"episode {index}: invalid length/index/range")
        path = root / "data" / f"chunk-{int(row['data/chunk_index']):03d}" / f"file-{int(row['data/file_index']):03d}.parquet"
        if path in expected_data:
            errors.append(f"episode {index}: duplicate data file reference")
        expected_data.add(path)
        if not path.is_file():
            errors.append(f"missing {path}")
        else:
            footer = pq.read_metadata(path)
            if footer.num_rows != length:
                errors.append(f"{path}: footer row count differs from episode length")
            if not footer.schema.to_arrow_schema().equals(LeRobotV3Dataset._schema(), check_metadata=False):
                errors.append(f"{path}: data schema is incompatible")
        for key in CAMERA_KEYS:
            video = root / "videos" / key / f"chunk-{int(row[f'videos/{key}/chunk_index']):03d}" / f"file-{int(row[f'videos/{key}/file_index']):03d}.mp4"
            if video in expected_videos[key]:
                errors.append(f"episode {index}: duplicate video reference {key}")
            expected_videos[key].add(video)
            if not video.is_file() or video.stat().st_size <= 0:
                errors.append(f"missing or empty video {video}")
        total += length
    if set(data_files) != expected_data:
        errors.append("data files differ from committed episode index")
    for key in CAMERA_KEYS:
        if set((root / "videos" / key).glob("*/*.mp4")) != expected_videos[key]:
            errors.append(f"{key}: video files differ from committed episode index")
        feature = info["features"].get(key, {})
        if feature.get("dtype") != "video" or feature.get("info", {}).get("video.fps") != info["fps"]:
            errors.append(f"{key}: video metadata fps/type mismatch")
    for key in ("observation.state", "action"):
        feature = info["features"].get(key, {})
        if feature.get("names") != list(FEATURE_NAMES) or feature.get("dtype") != "float32":
            errors.append(f"{key}: F1 joint order or dtype mismatch")
    if len(episodes) != int(info.get("total_episodes", -1)) or total != int(info.get("total_frames", -1)):
        errors.append("dataset totals differ from episode index")
    for name in ("stats.json", "tasks.parquet"):
        if not (root / "meta" / name).is_file():
            errors.append(f"missing meta/{name}")
    result = {"startup_compatible": not errors, "media_audited": False,
              "row_timing_audited": False, "episodes": len(episodes), "frames": total,
              "errors": errors}
    if errors:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def video_probe(path: Path) -> dict[str, float | int]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_entries",
            "stream=nb_read_frames,r_frame_rate", "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    numerator, denominator = stream["r_frame_rate"].split("/", 1)
    return {
        "frames": int(stream["nb_read_frames"]),
        "fps": float(numerator) / float(denominator),
    }


def audit(
    root: Path,
    allow_empty: bool = False,
    expected_fps: float | None = None,
    strict_timing: bool = False,
    startup_only: bool = False,
) -> dict:
    root = root.resolve()
    info_path = root / "meta/info.json"
    if not info_path.is_file():
        if allow_empty and root.is_dir():
            housekeeping = {".staging", ".write.lock", ".dataset.lock", "recovery"}
            # collection.start binds the scene before preflight, even before
            # the first episode. A valid route is configuration, not payload.
            route = root / ".collection_route.json"
            if route.exists():
                binding = json.loads(route.read_text(encoding="utf-8"))
                if (not isinstance(binding, dict)
                        or not all(isinstance(binding.get(k), str) and binding[k]
                                   for k in ("source", "destination", "scene", "task"))
                        or Path(binding["source"]).resolve() != root):
                    raise RuntimeError(f"invalid collection route {route}")
                housekeeping.add(".collection_route.json")
            payload = [path for path in root.iterdir() if path.name not in housekeeping]
            staging = root / ".staging"
            staging_empty = not staging.exists() or (
                staging.is_dir() and not any(staging.iterdir())
            )
            if not payload and staging_empty:
                return {"valid": True, "empty": True, "root": str(root),
                        "recovery_episodes": len(list((root / "recovery").glob("episode-*")))}
        raise RuntimeError(f"missing {info_path}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if info.get("codebase_version") != "v3.0":
        errors.append("codebase_version is not v3.0")
    if info.get("robot_type") != "f1_omnipicker":
        errors.append("robot_type is not f1_omnipicker")
    dataset_fps = float(info.get("fps", -1))
    if expected_fps is not None and dataset_fps != float(expected_fps):
        errors.append(f"dataset fps mismatch ({dataset_fps} != {expected_fps})")
    features = info.get("features", {})
    for key in ("observation.state", "action"):
        if features.get(key, {}).get("shape") != [FEATURE_SIZE]:
            errors.append(f"{key} is not {FEATURE_SIZE}D")
    if {key for key in CAMERA_KEYS if key not in features}:
        errors.append("one or more required camera features are missing")
    if strict_timing and info.get("camera_integrity_basis") != CAMERA_INTEGRITY_BASIS:
        errors.append("camera integrity timing basis is missing or incorrect")
    if strict_timing and info.get("camera_sync_basis") != "source_ros_system_time":
        errors.append("camera synchronization basis is missing or incorrect")
    if strict_timing:
        strict_features = {
            "source.timestamp_ns",
            "source.camera_timestamp_ns",
            "source.camera_integrity_timestamp_ns",
            "source.capture_monotonic_ns",
            "source.camera_arrival_timestamp_ns",
            "source.state_max_age_ns",
            "source.action_max_age_ns",
        }
        missing_features = strict_features.difference(features)
        if missing_features:
            errors.append(
                f"metadata missing strict timing features {sorted(missing_features)}"
            )

    episode_files = sorted((root / "meta/episodes").glob("*/*.parquet"))
    episodes: list[dict] = []
    for path in episode_files:
        episodes.extend(pq.read_table(path).to_pylist())
    episodes.sort(key=lambda row: int(row["episode_index"]))
    data_files = sorted((root / "data").glob("*/*.parquet"))
    if startup_only:
        return startup_structure(root, info, episodes, data_files, errors)
    data_rows = 0
    expected_index = 0
    for path in data_files:
        table = pq.read_table(path)
        data_rows += table.num_rows
        if table.num_rows == 0:
            errors.append(f"{path}: empty episode data file")
            continue
        for key in ("observation.state", "action"):
            values = np.asarray(table[key].to_pylist(), dtype=np.float32)
            if values.ndim != 2 or values.shape[1] != FEATURE_SIZE:
                errors.append(f"{path}: {key} shape mismatch")
            elif not np.all(np.isfinite(values)):
                errors.append(f"{path}: {key} has non-finite values")
        indices = table["index"].to_pylist()
        if indices != list(range(expected_index, expected_index + table.num_rows)):
            errors.append(f"{path}: global index is not contiguous")
        expected_index += table.num_rows
        if strict_timing:
            required = {
                "source.timestamp_ns",
                "source.camera_timestamp_ns",
                "source.camera_integrity_timestamp_ns",
                "source.capture_monotonic_ns",
                "source.camera_arrival_timestamp_ns",
                "source.state_max_age_ns",
                "source.action_max_age_ns",
            }
            missing = required.difference(table.column_names)
            if missing:
                errors.append(f"{path}: missing strict timing fields {sorted(missing)}")
                continue
            frame_index = np.asarray(table["frame_index"].to_pylist(), dtype=np.int64)
            nominal = np.asarray(table["timestamp"].to_pylist(), dtype=np.float64)
            if not np.array_equal(frame_index, np.arange(table.num_rows)):
                errors.append(f"{path}: frame_index is not contiguous")
            if not np.allclose(nominal, frame_index / dataset_fps, atol=1e-6):
                errors.append(f"{path}: nominal 30 Hz timestamps are incorrect")
            capture = np.asarray(
                table["source.capture_monotonic_ns"].to_pylist(), dtype=np.int64
            )
            source_time = np.asarray(
                table["source.timestamp_ns"].to_pylist(), dtype=np.int64
            )
            camera_source = np.asarray(
                table["source.camera_timestamp_ns"].to_pylist(), dtype=np.int64
            )
            camera_integrity = np.asarray(
                table["source.camera_integrity_timestamp_ns"].to_pylist(),
                dtype=np.int64,
            )
            camera_arrival = np.asarray(
                table["source.camera_arrival_timestamp_ns"].to_pylist(), dtype=np.int64
            )
            if camera_source.shape != (table.num_rows, 3):
                errors.append(f"{path}: camera source timestamp shape mismatch")
            if camera_integrity.shape != (table.num_rows, 3):
                errors.append(f"{path}: camera integrity timestamp shape mismatch")
                continue
            if camera_source.shape != camera_integrity.shape:
                continue
            if not np.array_equal(camera_integrity, camera_source):
                errors.append(f"{path}: camera source timestamps were modified")
            if table.num_rows >= 2:
                if np.any(np.diff(capture) <= 0):
                    errors.append(f"{path}: capture timestamps are not unique/increasing")
                if np.any(np.diff(source_time) <= 0):
                    errors.append(f"{path}: ROS source timestamps are not unique/increasing")
                integrity_delta = np.diff(camera_integrity, axis=0)
                if np.any(integrity_delta <= 0):
                    errors.append(
                        f"{path}: camera integrity timestamps repeat or reverse"
                    )
                from pico_f1.alignment_archive import read_extras, validate_coverage
                episode_index = int(table["episode_index"][0].as_py())
                try:
                    extras = read_extras(root / "alignment" / f"episode-{episode_index:06d}")
                    validate_coverage(camera_source, extras, 55_000_000)
                except (ValueError, OSError, KeyError) as exc:
                    errors.append(f"{path}: camera integrity stream has a frame gap or invalid archive: {exc}")
                if np.any(np.diff(camera_arrival, axis=0) <= 0):
                    errors.append(f"{path}: camera arrival timestamps repeat or reverse")
                duration = (capture[-1] - capture[0]) / 1e9
                actual_fps = (table.num_rows - 1) / duration
                if table.num_rows >= 10 and abs(actual_fps - dataset_fps) / dataset_fps > 0.05:
                    errors.append(
                        f"{path}: capture rate {actual_fps:.3f}Hz is not strict {dataset_fps:g}Hz"
                    )
            if camera_arrival.shape != (table.num_rows, 3):
                errors.append(f"{path}: camera arrival timestamp shape mismatch")
            if np.max(np.ptp(camera_source, axis=1)) > int(0.05 * 1e9):
                errors.append(f"{path}: cross-camera source skew exceeds 50ms")
            for column, label in (
                ("source.state_max_age_ns", "state"),
                ("source.action_max_age_ns", "action"),
            ):
                ages = np.asarray(table[column].to_pylist(), dtype=np.int64)
                if np.any(ages < 0) or np.max(ages) > int(0.05 * 1e9):
                    errors.append(f"{path}: {label} age exceeds 50ms")

    for expected_episode, row in enumerate(episodes):
        length = int(row["length"])
        if int(row["episode_index"]) != expected_episode:
            errors.append("episode indices are not contiguous")
        if int(row["dataset_from_index"]) != sum(int(item["length"]) for item in episodes[:expected_episode]):
            errors.append(f"episode {expected_episode}: dataset_from_index mismatch")
        if int(row["dataset_to_index"]) - int(row["dataset_from_index"]) != length:
            errors.append(f"episode {expected_episode}: dataset range mismatch")

    video_summary = {}
    for key in CAMERA_KEYS:
        files = sorted((root / "videos" / key).glob("*/*.mp4"))
        probes = []
        for index, path in enumerate(files):
            print(f"AUDIT_VIDEO {key} {index + 1}/{len(files)} {path.name}", file=sys.stderr, flush=True)
            probes.append(video_probe(path))
        frames = sum(int(item["frames"]) for item in probes)
        rates = sorted({float(item["fps"]) for item in probes})
        video_summary[key] = {"files": len(files), "frames": frames, "fps": rates}
        if frames != data_rows:
            errors.append(f"{key}: video/data frame mismatch ({frames} != {data_rows})")
        if expected_fps is not None and any(
            abs(rate - float(expected_fps)) > 1e-6 for rate in rates
        ):
            errors.append(f"{key}: video fps is not {expected_fps:g}")
        for episode, data_path in enumerate(data_files):
            if episode >= len(files):
                break
            episode_rows = pq.read_metadata(data_path).num_rows
            episode_frames = int(probes[episode]["frames"])
            if episode_frames != episode_rows:
                errors.append(
                    f"{key}: episode {episode} video/data mismatch "
                    f"({episode_frames} != {episode_rows})"
                )

    if len(episodes) != int(info.get("total_episodes", -1)):
        errors.append("total_episodes mismatch")
    if data_rows != int(info.get("total_frames", -1)):
        errors.append("total_frames mismatch")
    if sum(int(row["length"]) for row in episodes) != data_rows:
        errors.append("episode length sum mismatch")
    stats_path = root / "meta/stats.json"
    if not stats_path.is_file():
        errors.append("meta/stats.json missing")
    else:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        required_stats = {"min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99"}
        for key in ("observation.state", "action"):
            if set(stats.get(key, {})) != required_stats:
                errors.append(f"{key}: incomplete statistics")
            elif not all(
                math.isfinite(float(value))
                for name, values in stats[key].items()
                for value in values
            ):
                errors.append(f"{key}: non-finite statistics")

    result = {
        "valid": not errors,
        "root": str(root),
        "episodes": len(episodes),
        "frames": data_rows,
        "data_files": len(data_files),
        "video": video_summary,
        "errors": errors,
        "recovery_episodes": len(list((root / "recovery").glob("episode-*"))),
    }
    if errors:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path)
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--startup-only", action="store_true",
                        help="Fast append-compatibility check; no video decoding or row-level integrity audit")
    parser.add_argument(
        "--legacy-compatible",
        action="store_true",
        help="audit an archived pre-v1.3.8 dataset without the strict 30 Hz fields",
    )
    args = parser.parse_args()
    if args.startup_only and args.legacy_compatible:
        parser.error("startup-only requires the current strict dataset contract")
    from pico_f1.collection import effective_settings
    default = Path(effective_settings(Path(__file__).resolve().parents[1])["source"])
    expected_fps = None
    strict_timing = False
    if not args.legacy_compatible:
        expected_fps = float(load_control_config()["recording"]["fps"])
        strict_timing = True
    print(json.dumps(
        audit(args.root or default, args.allow_empty, expected_fps, strict_timing, args.startup_only),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""审计一个已解压的 G2 episode 是否可以进入转换流程。

输入 meta、aligned H5、三路视频及相机时间戳，输出 JSON 审计报告。完整扫描
H5 数值和时间戳，但仅用 ffprobe 检查视频流，不逐帧解码，也不修改原始数据。
H5 帧号不得直接作为视频帧号。本文件属于 G2 项目定制代码。
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path

import h5py
import numpy as np

from g2_sort.data_tools.g2_contract import (
    ACTION_DIM,
    ACTION_NAMES,
    ACTION_PATHS,
    CAMERAS,
    CAMERA_TIMESTAMP_FILES,
    STATE_PATHS,
)


def _frame_keys(h5_file: h5py.File) -> list[str]:
    try:
        keys = sorted(h5_file.keys(), key=int)
    except ValueError as exc:
        raise ValueError("H5 top-level keys must be integer frame indices") from exc
    if keys != [str(i) for i in range(len(keys))]:
        raise ValueError("H5 frame indices are not contiguous from zero")
    return keys


def _vector(group: h5py.Group, paths: tuple[str, ...]) -> np.ndarray:
    values = [np.asarray(group[path][()], dtype=np.float64).reshape(-1) for path in paths]
    result = np.concatenate(values)
    if result.shape != (ACTION_DIM,):
        raise ValueError(f"expected {ACTION_DIM}D vector, got {result.shape}")
    return result


def _ffprobe(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    stream = json.loads(result.stdout)["streams"][0]
    return stream


def _read_camera_timestamps(path: Path) -> list[int]:
    timestamps = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        fields = line.split()
        if not fields:
            continue
        try:
            timestamps.append(int(fields[0]))
        except ValueError as exc:
            raise ValueError(f"invalid timestamp at {path}:{line_number}") from exc
    if not timestamps:
        raise ValueError(f"empty timestamp file: {path}")
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        raise ValueError(f"timestamps are not strictly increasing: {path}")
    return timestamps


def audit_episode(episode_dir: Path, probe_video: bool = True) -> dict:
    h5_path = episode_dir / "record" / "aligned_joints.h5"
    meta_path = episode_dir / "meta_info.json"
    errors: list[str] = []
    warnings: list[str] = []
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    if not h5_path.exists():
        return {
            "episode": episode_dir.name,
            "valid": False,
            "errors": ["missing record/aligned_joints.h5"],
            "meta": meta,
        }

    state_min = np.full(ACTION_DIM, np.inf)
    state_max = np.full(ACTION_DIM, -np.inf)
    action_min = np.full(ACTION_DIM, np.inf)
    action_max = np.full(ACTION_DIM, -np.inf)
    main_timestamps: list[int] = []
    camera_timestamps = {name: [] for name in CAMERAS}

    with h5py.File(h5_path, "r") as h5_file:
        keys = _frame_keys(h5_file)
        for key in keys:
            group = h5_file[key]
            state = _vector(group, STATE_PATHS)
            action = _vector(group, ACTION_PATHS)
            if not np.isfinite(state).all() or not np.isfinite(action).all():
                errors.append(f"non-finite state/action at frame {key}")
                break
            state_min = np.minimum(state_min, state)
            state_max = np.maximum(state_max, state)
            action_min = np.minimum(action_min, action)
            action_max = np.maximum(action_max, action)
            main_timestamps.append(int(group["main_timestamp"][()]))
            for image_key, (camera_name, _) in CAMERAS.items():
                value = group[f"timestamp/camera/{camera_name}"][()]
                camera_timestamps[image_key].append(int(np.asarray(value).reshape(-1)[0]))

    if any(b <= a for a, b in zip(main_timestamps, main_timestamps[1:])):
        errors.append("main_timestamp is not strictly increasing")
    for image_key, timestamps in camera_timestamps.items():
        if any(b < a for a, b in zip(timestamps, timestamps[1:])):
            errors.append(f"{image_key} timestamp is not monotonic")

    video_info = {}
    timestamp_alignment = {}
    for image_key, (_, relative_path) in CAMERAS.items():
        path = episode_dir / relative_path
        timestamp_path = episode_dir / CAMERA_TIMESTAMP_FILES[image_key]
        if not path.exists():
            errors.append(f"missing {relative_path}")
            continue
        if not timestamp_path.exists():
            errors.append(f"missing {CAMERA_TIMESTAMP_FILES[image_key]}")
            continue
        try:
            raw_timestamps = _read_camera_timestamps(timestamp_path)
            timestamp_to_index = {
                timestamp: index for index, timestamp in enumerate(raw_timestamps)
            }
            aligned_indices = [
                timestamp_to_index[timestamp]
                for timestamp in camera_timestamps[image_key]
            ]
            unique_ratio = len(set(aligned_indices)) / max(len(aligned_indices), 1)
            timestamp_alignment[image_key] = {
                "raw_timestamp_count": len(raw_timestamps),
                "aligned_timestamp_count": len(aligned_indices),
                "aligned_unique_frame_count": len(set(aligned_indices)),
                "aligned_unique_frame_ratio": unique_ratio,
                "first_raw_frame_index": aligned_indices[0],
                "last_raw_frame_index": aligned_indices[-1],
            }
            if unique_ratio < 0.9:
                warnings.append(
                    f"{image_key} reuses many aligned frames: "
                    f"unique_ratio={unique_ratio:.3f}"
                )
        except KeyError as exc:
            errors.append(
                f"{image_key} aligned timestamp is absent from raw timestamp file: "
                f"{exc.args[0]}"
            )
            continue
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if probe_video:
            try:
                video_info[image_key] = _ffprobe(path)
                decoded_count = video_info[image_key].get("nb_read_frames")
                if decoded_count is not None and int(decoded_count) != len(raw_timestamps):
                    errors.append(
                        f"{image_key} video frames={decoded_count}, "
                        f"timestamps={len(raw_timestamps)}"
                    )
            except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as exc:
                errors.append(f"ffprobe failed for {relative_path}: {exc}")

    duration = meta.get("duration")
    if duration is not None and (not math.isfinite(float(duration)) or duration >= 600):
        errors.append(f"abnormal duration={duration}")
    if meta.get("data_validate", {}).get("validate") is False:
        errors.append("meta_info marks data_validate=false")
    if meta.get("integrity", {}).get("integrity") is False:
        errors.append("meta_info marks integrity=false")
    if meta.get("fps_validate") is False:
        errors.append("meta_info marks fps_validate=false")

    return {
        "episode": episode_dir.name,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "frame_count": len(main_timestamps),
        "duration_seconds": duration,
        "aid": meta.get("AID"),
        "software_version": meta.get("sw_version"),
        "task_id": meta.get("task_id"),
        "action_contract": ACTION_NAMES,
        "state_min": state_min.tolist(),
        "state_max": state_max.tolist(),
        "action_min": action_min.tolist(),
        "action_max": action_max.tolist(),
        "video_info": video_info,
        "timestamp_alignment": timestamp_alignment,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-video-probe", action="store_true")
    args = parser.parse_args()
    report = audit_episode(args.episode_dir, not args.no_video_probe)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)
    raise SystemExit(0 if report["valid"] else 2)


if __name__ == "__main__":
    main()

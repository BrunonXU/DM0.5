#!/usr/bin/env python3
"""将一个审计通过的 G2 episode 转换为 OpenDM 显式 action JSONL。

输入 aligned H5、三路 H.265 视频和相机时间戳；输出三路无重编码 MP4及逐帧
34D state/action JSONL。视频帧必须通过相机时间戳精确映射，缺失或不匹配时
直接失败，禁止退化为 H5 帧号。本文件属于 G2 项目定制代码。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import h5py
import numpy as np

from g2_sort.data_tools.g2_contract import (
    ACTION_DIM,
    ACTION_PATHS,
    CAMERAS,
    CAMERA_TIMESTAMP_FILES,
    PROMPT,
    STATE_PATHS,
)


def _vector(group: h5py.Group, paths: tuple[str, ...]) -> list[float]:
    result = np.concatenate(
        [np.asarray(group[path][()], dtype=np.float32).reshape(-1) for path in paths]
    )
    if result.shape != (ACTION_DIM,) or not np.isfinite(result).all():
        raise ValueError(f"invalid {ACTION_DIM}D vector with shape {result.shape}")
    return result.tolist()


def _remux(source: Path, target: Path, fps: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-fflags",
            "+genpts",
            "-framerate",
            str(fps),
            "-i",
            str(source),
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
    )


def _timestamp_index(path: Path) -> dict[int, int]:
    result = {}
    for frame_index, line in enumerate(path.read_text().splitlines()):
        fields = line.split()
        if fields:
            timestamp = int(fields[0])
            if timestamp in result:
                raise ValueError(f"duplicate timestamp {timestamp} in {path}")
            result[timestamp] = frame_index
    if not result:
        raise ValueError(f"empty timestamp file: {path}")
    return result


def convert_episode(
    episode_dir: Path,
    output_root: Path,
    split: str,
    fps: int,
    remux: bool,
) -> Path:
    episode_id = episode_dir.name
    video_urls = {}
    timestamp_indices = {}
    for image_key, (_, relative_path) in CAMERAS.items():
        source = episode_dir / relative_path
        timestamp_path = episode_dir / CAMERA_TIMESTAMP_FILES[image_key]
        if not source.exists():
            raise FileNotFoundError(source)
        if not timestamp_path.exists():
            raise FileNotFoundError(timestamp_path)
        timestamp_indices[image_key] = _timestamp_index(timestamp_path)
        relative_target = Path(episode_id) / f"{image_key}.mp4"
        target = output_root / "videos" / relative_target
        if remux:
            _remux(source, target, fps)
        elif not target.exists():
            raise FileNotFoundError(
                f"{target} does not exist; use --remux or stage videos first"
            )
        video_urls[image_key] = relative_target.as_posix()

    h5_path = episode_dir / "record" / "aligned_joints.h5"
    jsonl_path = output_root / "jsonl" / split / f"{episode_id}.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(h5_path, "r") as h5_file, jsonl_path.open("w") as output:
        keys = sorted(h5_file.keys(), key=int)
        if keys != [str(i) for i in range(len(keys))]:
            raise ValueError("H5 frame indices are not contiguous from zero")
        for frame_idx, key in enumerate(keys):
            group = h5_file[key]
            sample = {}
            for image_key, video_url in video_urls.items():
                camera_name = CAMERAS[image_key][0]
                timestamp = int(
                    np.asarray(
                        group[f"timestamp/camera/{camera_name}"][()]
                    ).reshape(-1)[0]
                )
                try:
                    raw_frame_idx = timestamp_indices[image_key][timestamp]
                except KeyError as exc:
                    raise ValueError(
                        f"{image_key} timestamp {timestamp} at H5 frame {key} "
                        "does not exist in the camera timestamp file"
                    ) from exc
                sample[image_key] = {
                    "type": "video",
                    "url": video_url,
                    "frame_idx": raw_frame_idx,
                    "_camera_name": camera_name,
                }
            sample.update(
                {
                    "state": _vector(group, STATE_PATHS),
                    "action": _vector(group, ACTION_PATHS),
                    "prompt": PROMPT,
                    "is_robot": True,
                }
            )
            output.write(json.dumps(sample, ensure_ascii=False) + "\n")
    return jsonl_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--split", choices=("train", "validation"), required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--remux", action="store_true")
    args = parser.parse_args()
    path = convert_episode(
        args.episode_dir, args.output_root, args.split, args.fps, args.remux
    )
    print(path)


if __name__ == "__main__":
    main()

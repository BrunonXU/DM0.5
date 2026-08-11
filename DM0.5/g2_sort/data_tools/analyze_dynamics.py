#!/usr/bin/env python3
"""分析单个 G2 H5 中 34D 命令与反馈的动态关系。

输入已解压 episode 的 ``aligned_joints.h5``，输出逐维范围、唯一值数量和
action→state 最佳响应滞后。只读 H5，不修改原始数据；结果用于发现常量维、
单位异常和控制语义变化。本文件属于 G2 项目定制代码。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np

from g2_sort.data_tools.g2_contract import (
    ACTION_DIM,
    ACTION_NAMES,
    ACTION_PATHS,
    STATE_PATHS,
)


def _matrix(h5_file: h5py.File, paths: tuple[str, ...]) -> np.ndarray:
    rows = []
    for key in sorted(h5_file.keys(), key=int):
        rows.append(
            np.concatenate(
                [
                    np.asarray(h5_file[key][path][()], dtype=np.float64).reshape(-1)
                    for path in paths
                ]
            )
        )
    matrix = np.stack(rows)
    if matrix.shape[1] != ACTION_DIM:
        raise ValueError(f"expected Nx{ACTION_DIM}, got {matrix.shape}")
    return matrix


def analyze(h5_path: Path, max_lag: int) -> dict:
    with h5py.File(h5_path, "r") as h5_file:
        state = _matrix(h5_file, STATE_PATHS)
        action = _matrix(h5_file, ACTION_PATHS)

    per_lag = {}
    groups = {
        "left_arm": slice(0, 7),
        "right_arm": slice(7, 14),
        "left_hand": slice(14, 24),
        "right_hand": slice(24, 34),
        "all": slice(0, 34),
    }
    for lag in range(max_lag + 1):
        if lag == 0:
            aligned_action, aligned_state = action, state
        else:
            aligned_action, aligned_state = action[:-lag], state[lag:]
        error = np.abs(aligned_state - aligned_action)
        per_lag[str(lag)] = {
            name: float(error[:, indices].mean()) for name, indices in groups.items()
        }

    best_lag = {
        name: min(
            range(max_lag + 1),
            key=lambda lag: per_lag[str(lag)][name],
        )
        for name in groups
    }
    dimensions = []
    for index, name in enumerate(ACTION_NAMES):
        dimensions.append(
            {
                "index": index,
                "name": name,
                "state_min": float(state[:, index].min()),
                "state_max": float(state[:, index].max()),
                "state_range": float(np.ptp(state[:, index])),
                "action_min": float(action[:, index].min()),
                "action_max": float(action[:, index].max()),
                "action_range": float(np.ptp(action[:, index])),
                "action_unique_count": int(np.unique(action[:, index]).size),
            }
        )
    return {
        "h5": str(h5_path),
        "frame_count": int(state.shape[0]),
        "max_lag_frames": max_lag,
        "best_lag_frames_by_group": best_lag,
        "mae_by_lag_and_group": per_lag,
        "dimensions": dimensions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("h5", type=Path)
    parser.add_argument("--max-lag", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.h5, args.max_lag)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["best_lag_frames_by_group"], indent=2))


if __name__ == "__main__":
    main()

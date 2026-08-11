"""从正式训练H5提取不可直接执行的复位候选。

用途：统计每条episode第0帧的34D state/action初始姿态。
输入：冻结split manifest和H5 episode根目录。
输出：包含34D中位数、来源元数据且全部执行锁关闭的JSON。
方法：逐episode读取固定字段后取中位数，必须人工核对才能启用真机复位。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from g2_sort.config.dm05_contract import ACTION_DIM


def _vector(group: object, prefix: str) -> np.ndarray:
    return np.concatenate(
        (
            np.asarray(group[f"{prefix}/joint/position"]).reshape(-1)[:14],
            np.asarray(group[f"{prefix}/left_effector/position"]).reshape(-1)[:10],
            np.asarray(group[f"{prefix}/right_effector/position"]).reshape(-1)[:10],
        )
    )


def extract(split_manifest: Path, h5_root: Path) -> dict[str, object]:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError("h5py is required for reset-pose extraction") from exc
    split = json.loads(split_manifest.read_text(encoding="utf-8"))
    episodes = split.get("train")
    if not isinstance(episodes, list) or len(episodes) != 800:
        raise ValueError("formal split must contain exactly 800 train episodes")
    states: list[np.ndarray] = []
    actions: list[np.ndarray] = []
    for episode in episodes:
        path = h5_root / str(episode) / "record" / "aligned_joints.h5"
        with h5py.File(path, "r") as handle:
            frame = handle["0"]
            states.append(_vector(frame, "state"))
            actions.append(_vector(frame, "action"))
    state = np.asarray(states, dtype=np.float64)
    action = np.asarray(actions, dtype=np.float64)
    if (
        state.shape != (800, ACTION_DIM)
        or action.shape != (800, ACTION_DIM)
        or not np.all(np.isfinite(state))
        or not np.all(np.isfinite(action))
    ):
        raise ValueError("formal H5 frame-zero arrays are not finite 800x34")
    return {
        "version": "g2-sort-training-start-candidate",
        "dimension": ACTION_DIM,
        "source": {
            "kind": "formal_train_frame0",
            "episode_count": len(episodes),
            "split_manifest": str(split_manifest),
            "h5_root": str(h5_root),
        },
        "state_median": np.median(state, axis=0).tolist(),
        "action_median": np.median(action, axis=0).tolist(),
        "execution": {
            "left_hand_enabled": False,
            "right_hand_enabled": False,
            "left_arm_enabled": False,
            "right_arm_enabled": False,
            "all_enabled": False,
            "note": "Candidate only; requires physical G2 validation.",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--h5-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = extract(args.split_manifest, args.h5_root)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")
        print(args.output)


if __name__ == "__main__":
    main()

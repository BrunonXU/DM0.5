#!/usr/bin/env python3
"""Export small numeric FK-validation samples from DM0.5 H5 episodes.

This script reads only joint/body/base/end state. It exports no images, actions,
instructions, or complete trajectories and never imports robot-control code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np


FIELDS = (
    "state/joint/position",
    "state/waist/position",
    "state/robot/position",
    "state/robot/orientation",
    "state/end/position",
    "state/end/orientation",
    "state/end/arm_position",
    "state/end/arm_orientation",
    "state/end/pose",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evenly_spaced_indices(count: int, requested: int) -> list[int]:
    if count <= 0 or requested <= 0:
        raise ValueError("count and requested must be positive")
    return sorted(set(int(value) for value in np.linspace(0, count - 1, min(count, requested))))


def export_episode(path: Path, samples: int) -> dict[str, object]:
    with h5py.File(path, "r") as handle:
        frame_names = sorted((name for name in handle if name.isdigit()), key=int)
        indices = evenly_spaced_indices(len(frame_names), samples)
        rows: list[dict[str, object]] = []
        for index in indices:
            frame_name = frame_names[index]
            frame = handle[frame_name]
            missing = [field for field in FIELDS if field not in frame]
            if missing:
                raise RuntimeError(f"{path}: frame {frame_name} missing {missing}")
            values = {field: frame[field][()].tolist() for field in FIELDS}
            flat = np.concatenate(
                [np.asarray(value, dtype=np.float64).reshape(-1) for value in values.values()]
            )
            if not np.isfinite(flat).all():
                raise RuntimeError(f"{path}: frame {frame_name} contains non-finite state")
            rows.append({"frame": int(frame_name), **values})
    return {
        "episode": path.parents[1].name,
        "source_sha256": sha256_file(path),
        "source_frame_count": len(frame_names),
        "sample_count": len(rows),
        "samples": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export minimal numeric FK samples")
    parser.add_argument("h5", type=Path, nargs="+")
    parser.add_argument("--samples-per-episode", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = {
        "schema": "g2_dm05_fk_samples_v1",
        "scope": "numeric_state_only_no_images_no_actions",
        "quaternion_order_in_source": "xyzw_candidate_from_base_identity_requires_fk_confirmation",
        "side_order_in_end_arrays": "left_then_right_candidate_requires_fk_confirmation",
        "episodes": [export_episode(path.resolve(), args.samples_per_episode) for path in args.h5],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "episodes": len(report["episodes"]),
                "samples": sum(ep["sample_count"] for ep in report["episodes"]),
                "bytes": args.output.stat().st_size,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

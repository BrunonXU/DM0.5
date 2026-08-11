#!/usr/bin/env python3
"""Build deterministic, episode-balanced global indices from JsonlDataset cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index-cache", required=True)
    parser.add_argument("--samples-per-episode", type=int, default=16)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.samples_per_episode < 1:
        raise ValueError("samples-per-episode must be positive")
    cache = json.loads(Path(args.index_cache).read_text(encoding="utf-8"))["data"]
    episodes = []
    global_start = 0
    for episode_path, num_lines in cache.items():
        usable = max(0, int(num_lines) - 1)
        if usable < 1:
            raise ValueError(f"episode has no usable samples: {episode_path}")
        count = min(args.samples_per_episode, usable)
        # Midpoints of equal-width temporal bins avoid over-weighting endpoints.
        local_indices = [
            min(usable - 1, ((2 * slot + 1) * usable) // (2 * count))
            for slot in range(count)
        ]
        episodes.append(
            {
                "episode": Path(episode_path).stem,
                "num_usable_samples": usable,
                "indices": [global_start + index for index in local_indices],
            }
        )
        global_start += usable
    result = {
        "index_cache": args.index_cache,
        "samples_per_episode": args.samples_per_episode,
        "num_episodes": len(episodes),
        "num_unique_samples": sum(len(item["indices"]) for item in episodes),
        "dataset_total_samples": global_start,
        "episodes": episodes,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

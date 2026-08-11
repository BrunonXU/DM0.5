#!/usr/bin/env python3
"""Rank checkpoint FM-loss reports with episode-macro statistics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", required=True)
    parser.add_argument("--top-k", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    rows = []
    for path in sorted(result_dir.glob("checkpoint-*.json")):
        checkpoint = int(path.stem.split("-")[-1])
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "checkpoint": checkpoint,
                "episode_macro_mean_loss": payload["episode_macro_mean_loss"],
                "episode_macro_median_loss": payload[
                    "episode_macro_median_loss"
                ],
                "episode_macro_p90_loss": payload["episode_macro_p90_loss"],
                "num_episodes": payload["num_episodes"],
                "num_unique_samples": payload["num_unique_samples"],
                "seed_repeats": payload["seed_repeats"],
            }
        )
    if len(rows) < args.top_k:
        raise RuntimeError(
            f"found {len(rows)} reports in {result_dir}, need top {args.top_k}"
        )
    ranking = sorted(
        rows,
        key=lambda row: (
            row["episode_macro_mean_loss"],
            row["episode_macro_p90_loss"],
            row["checkpoint"],
        ),
    )
    output = {
        "ranking_rule": "episode_macro_mean_loss, then P90, then earlier checkpoint",
        "ranking": ranking,
        "selected_checkpoints": [
            row["checkpoint"] for row in ranking[: args.top_k]
        ],
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()

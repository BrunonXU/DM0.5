#!/usr/bin/env python3
"""Backfill and follow DM0.5 text logs into TensorBoard scalar events."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

from tensorboardX import SummaryWriter


ROW_PATTERN = re.compile(
    r"\{'loss':\s*'(?P<loss>[^']+)',\s*"
    r"'grad_norm':\s*'(?P<grad_norm>[^']+)',\s*"
    r"'learning_rate':\s*'(?P<learning_rate>[^']+)',\s*"
    r"'fm_loss':\s*'(?P<fm_loss>[^']+)',\s*"
    r"'step':\s*(?P<step>\d+),\s*"
    r"'epoch':\s*'(?P<epoch>[^']+)'\}"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def load_last_step(state_path: Path) -> int:
    if not state_path.exists():
        return 0
    try:
        return int(json.loads(state_path.read_text())["last_step"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def save_last_step(state_path: Path, step: int) -> None:
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last_step": step}) + "\n")
    temporary.replace(state_path)


def read_rows(path: Path, after_step: int) -> list[dict[str, float | int]]:
    if not path.exists():
        return []
    text = path.read_text(errors="ignore")
    rows: list[dict[str, float | int]] = []
    for match in ROW_PATTERN.finditer(text):
        row: dict[str, float | int] = {
            "step": int(match.group("step")),
            "loss": float(match.group("loss")),
            "fm_loss": float(match.group("fm_loss")),
            "grad_norm": float(match.group("grad_norm")),
            "learning_rate": float(match.group("learning_rate")),
            "epoch": float(match.group("epoch")),
        }
        if int(row["step"]) <= after_step:
            continue
        if not all(math.isfinite(float(value)) for value in row.values()):
            raise RuntimeError(f"non-finite training metric at step {row['step']}")
        rows.append(row)
    rows.sort(key=lambda row: int(row["step"]))
    return rows


def main() -> None:
    args = parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.log_dir / "export_state.json"
    last_step = load_last_step(state_path)
    writer = SummaryWriter(log_dir=str(args.log_dir))
    try:
        while True:
            rows = read_rows(args.input, last_step)
            for row in rows:
                step = int(row["step"])
                writer.add_scalar("train/loss", row["loss"], step)
                writer.add_scalar("train/fm_loss", row["fm_loss"], step)
                writer.add_scalar("train/grad_norm", row["grad_norm"], step)
                writer.add_scalar(
                    "train/learning_rate", row["learning_rate"], step
                )
                writer.add_scalar("train/epoch", row["epoch"], step)
                last_step = step
            if rows:
                writer.flush()
                save_last_step(state_path, last_step)
                print(f"tensorboard_export last_step={last_step}", flush=True)
            if args.once:
                break
            time.sleep(args.poll_seconds)
    finally:
        writer.close()


if __name__ == "__main__":
    main()

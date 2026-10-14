#!/usr/bin/env python3
"""Show Windows/pygame F710 controls without opening any network connection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g2teleop.gamepad_windows import WindowsF710Reader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read Windows F710 only; no network and no robot commands")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--hz", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("READ-ONLY: this program opens no network socket and contains no GDK code.")
    print("F710 must be in X mode. Move one control at a time; Ctrl-C stops.")
    previous: object = None
    try:
        with WindowsF710Reader(args.index) as reader:
            print(f"pygame device: {reader.name}")
            while True:
                sample = reader.snapshot()
                rounded = {
                    "connected": sample.connected,
                    "lx": round(sample.left_x, 3), "ly": round(sample.left_y, 3),
                    "rx": round(sample.right_x, 3), "ry": round(sample.right_y, 3),
                    "lt": round(sample.left_trigger, 3), "rt": round(sample.right_trigger, 3),
                    "hat": [sample.dpad_x, sample.dpad_y], "lb": sample.lb, "rb": sample.rb,
                    "a": sample.a, "b": sample.b, "x": sample.x, "y": sample.y,
                    "start": sample.start,
                }
                if rounded != previous:
                    print(json.dumps(rounded), flush=True)
                    previous = rounded
                time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        print("Stopped by operator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

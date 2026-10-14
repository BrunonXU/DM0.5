#!/usr/bin/env python3
"""Print iPad HEBI pose and mapped intent; never import or call GDK."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import time

from g2teleop.input_mapping import InputMapper
from g2teleop.ipad_hebi import IpadHebiReader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read HEBI Mobile I/O only; sends no robot commands."
    )
    parser.add_argument("--family", default="HEBI")
    parser.add_argument("--name", default="mobileIO")
    parser.add_argument("--print-hz", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.print_hz <= 0:
        raise ValueError("--print-hz must be positive")
    reader = IpadHebiReader(family=args.family, name=args.name)
    mapper = InputMapper()
    print("READ-ONLY: this process does not import GDK or send robot commands.")
    print(f"Discovering HEBI device {args.family}/{args.name} ...")
    reader.connect()
    print("Connected. Press Ctrl-C to stop.")
    next_print = 0.0
    try:
        while True:
            sample = reader.read()
            intent = mapper.map(sample)
            now = time.monotonic()
            if now >= next_print:
                payload = {
                    "sample": asdict(sample),
                    "intent": asdict(intent),
                }
                print(json.dumps(payload, ensure_ascii=False, default=str))
                next_print = now + 1.0 / args.print_hz
    except KeyboardInterrupt:
        print("Stopped by operator.")
    finally:
        reader.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


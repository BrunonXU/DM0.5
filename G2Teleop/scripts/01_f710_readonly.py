#!/usr/bin/env python3
"""Print raw F710 Linux joystick changes; never import or call GDK."""

from __future__ import annotations

import argparse
import json

from g2teleop.gamepad_linux import LinuxJoystickReader


DEFAULT_DEVICE = (
    "/dev/input/by-id/"
    "usb-Logitech_Wireless_Gamepad_F710_1BF994E3-joystick"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read F710 events only; sends no robot commands."
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument(
        "--show-initial",
        action="store_true",
        help="also print kernel initialization events",
    )
    parser.add_argument(
        "--axis-threshold",
        type=int,
        default=1024,
        help="minimum raw change before printing another axis sample",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.axis_threshold < 0:
        raise ValueError("--axis-threshold must be non-negative")
    print("READ-ONLY: no agibot_gdk import and no robot commands.")
    print(f"Opening {args.device}")
    print(
        "Operate one control at a time: left stick, right stick, LT, RT, "
        "D-pad, LB, RB, A/B/X/Y. Press Ctrl-C to stop."
    )
    last_axes: dict[int, int] = {}
    try:
        with LinuxJoystickReader(args.device) as reader:
            while True:
                event = reader.read_event()
                if event.is_initial:
                    if event.kind == "axis":
                        last_axes[event.number] = event.value
                    if not args.show_initial:
                        continue
                if event.kind == "axis":
                    previous = last_axes.get(event.number)
                    last_axes[event.number] = event.value
                    if (
                        previous is not None
                        and abs(event.value - previous) < args.axis_threshold
                        and abs(event.value) < 32000
                    ):
                        continue
                payload = {
                    "kind": event.kind,
                    "number": event.number,
                    "expected_name_unverified": event.expected_name,
                    "raw": event.value,
                    "normalized": round(event.normalized_value, 6),
                    "device_time_ms": event.device_time_ms,
                }
                print(json.dumps(payload, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        print("Stopped by operator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


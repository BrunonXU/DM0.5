"""Passively inspect G2 O10 tactile state published by HAL.

This diagnostic only subscribes to the left/right ``EndForceState`` topics. It
does not create a Robot, publish a DDS message, or send a CAN/GDK command.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


TOPICS = {
    "left": "/hal/left_ee_force_data",
    "right": "/hal/right_ee_force_data",
}


def _sensor_summary(sensor: Any) -> dict[str, object]:
    normal = [float(value) for value in sensor.normal_force]
    contact = [bool(value) for value in sensor.contact]
    valid = [bool(value) for value in sensor.valid]
    return {
        "sensor_type": int(sensor.sensor_type),
        "sensor_name": str(sensor.sensor_name),
        "frame": str(sensor.frame),
        "rows": int(sensor.rows),
        "cols": int(sensor.cols),
        "cell_count": int(sensor.rows) * int(sensor.cols),
        "normal_force_count": len(normal),
        "normal_force_min_mn": min(normal) if normal else None,
        "normal_force_max_mn": max(normal) if normal else None,
        "nonzero_normal_force_count": sum(value != 0.0 for value in normal),
        "contact_count": sum(contact),
        "valid_count": sum(valid),
        "err_code": int(sensor.err_code),
    }


def summarize_samples(
    samples: dict[str, Sequence[tuple[float, Any]]],
) -> dict[str, object]:
    sides: dict[str, object] = {}
    total_tactile_sensors = 0
    for side in TOPICS:
        entries = list(samples.get(side, ()))
        intervals = [
            entries[index][0] - entries[index - 1][0]
            for index in range(1, len(entries))
        ]
        latest = entries[-1][1] if entries else None
        sensors = (
            [_sensor_summary(sensor) for sensor in latest.force]
            if latest is not None
            else []
        )
        tactile_sensors = [
            sensor for sensor in sensors if sensor["sensor_type"] == 2
        ]
        total_tactile_sensors += len(tactile_sensors)
        sides[side] = {
            "topic": TOPICS[side],
            "message_count": len(entries),
            "observed_hz": (
                1.0 / (sum(intervals) / len(intervals)) if intervals else None
            ),
            "controlled": bool(latest.controlled) if latest is not None else None,
            "end_effector_type": int(latest.type) if latest is not None else None,
            "status": int(latest.status) if latest is not None else None,
            "err_code": int(latest.err_code) if latest is not None else None,
            "sensor_count": len(sensors),
            "tactile_sensor_count": len(tactile_sensors),
            "sensors": sensors,
        }
    if all(side["message_count"] == 0 for side in sides.values()):
        result = "NO_FORCE_TOPIC_MESSAGES"
    elif total_tactile_sensors == 0:
        result = "NO_TACTILE_SENSORS_IN_MESSAGES"
    else:
        result = "TACTILE_PRESENT"
    return {
        "read_only": True,
        "commands_sent": 0,
        "result": result,
        "sides": sides,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 2.0 <= args.duration_seconds <= 60.0:
        parser.error("--duration-seconds must be between 2 and 60")

    from agibot_gdk import dds
    from genie_msgs_pb.msg.EndForceState_pb2 import EndForceState

    lock = threading.Lock()
    samples: dict[str, list[tuple[float, Any]]] = {
        side: [] for side in TOPICS
    }

    def callback(side: str):
        def receive(message: Any, _info: Any) -> None:
            with lock:
                samples[side].append((time.monotonic(), message))

        return receive

    node = dds.Node("o10_tactile_read_only_probe")
    qos = dds.GDKQoS()
    qos.history = dds.GDKQoSHistory.KEEP_LAST
    qos.depth = 10
    qos.reliability = dds.GDKQoSReliability.RELIABLE
    qos.durability = dds.GDKQoSDurability.TRANSIENT_LOCAL
    subscribers = [
        node.create_subscriber(topic, EndForceState, qos, callback(side))
        for side, topic in TOPICS.items()
    ]
    if any(subscriber is None for subscriber in subscribers):
        raise RuntimeError("failed to create one or more tactile subscribers")

    print(
        f"只读订阅双手触觉 topic {args.duration_seconds:.1f}s；不会发送动作。",
        flush=True,
    )
    time.sleep(args.duration_seconds)
    with lock:
        report = summarize_samples(samples)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0 if report["result"] == "TACTILE_PRESENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())

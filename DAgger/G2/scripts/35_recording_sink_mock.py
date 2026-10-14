#!/usr/bin/env python3
"""Offline shared-source recording demonstration and admission timing; no GDK."""
import argparse
import json
from pathlib import Path
import struct
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# Source checkouts use a sibling shared tree; release archives bundle it in src.
from pathlib import Path as _DaggerPath
import sys as _dagger_sys
_dagger_root = _DaggerPath(__file__).resolve().parents[1]
_dagger_sources = [_dagger_root / "src"]
if not (_dagger_root / "src/dagger_core").is_dir():
    _dagger_sources.append(_dagger_root.parent / "shared/src")
for _dagger_source in reversed(_dagger_sources):
    if _dagger_source.is_dir() and str(_dagger_source) not in _dagger_sys.path:
        _dagger_sys.path.insert(0, str(_dagger_source))

from g2dagger.recording_observation import (
    CAMERAS, DeviceTimestamp, TimestampedFrame, TimestampedObservation, SharedObservationStream,
)
from g2dagger.recording_sinks import make_recording_sink
from g2dagger.recording_audit import audit_payload_archive


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\xff\0" * 2)) + chunk(b"IEND", b""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("inference-only", "hil-record", "prebuffer-record"), default="prebuffer-record")
    args = parser.parse_args()
    sink = make_recording_sink(args.mode, args.output)
    image = png()
    def capture():
        t = 1_000_000_000 + frame_id * 1_000_000_000 // 30
        return TimestampedObservation(frame_id, t, t + 3, tuple(
            TimestampedFrame(name, frame_id, DeviceTimestamp(t + j, "ns", "synthetic"),
                             t + j, image, "image/png", 2, 2)
            for j, name in enumerate(CAMERAS)), t, (None,) * 34, (False,) * 34, "synthetic_missing")
    stream = SharedObservationStream(capture, sink)
    times, requests = [], []
    started = time.monotonic()
    try:
        for frame_id in range(180):
            before = time.perf_counter_ns()
            obs = stream.acquire()
            times.append(time.perf_counter_ns() - before)
            if frame_id == 120:
                sink.trigger()
            # Sparse mock requests; no assumption of 30 inference requests/s.
            if frame_id < 120 and frame_id % 20 == 0:
                assert stream.policy_observation() is obs
                requests.append(obs.observation_id)
        closed = sink.close()
    except BaseException:
        sink.close(status="aborted")
        raise
    ordered = sorted(times)
    report = dict(mode=args.mode, synthetic=True, hardware_output=False, training_ready=False,
                  policy_request_observation_ids=requests, acquisition_count=180,
                  acquisition_and_submit_p95_us=ordered[int(.95 * (len(times)-1))] / 1000,
                  acquisition_and_submit_max_us=max(times) / 1000,
                  wall_seconds=time.monotonic() - started, finalized=closed,
                  timing_scope="tiny synthetic PNG, burst submission on development host; not control jitter acceptance")
    if args.mode != "inference-only":
        path = args.output if closed else args.output.with_name(args.output.name + ".partial")
        report["payload_audit"] = audit_payload_archive(path)
    print(json.dumps(report, indent=2))
    return 0 if closed and report.get("payload_audit", {"valid": True})["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

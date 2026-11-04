"""F1 CPU-only historical-image benchmark. No ROS, model or action publishers."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time

import numpy as np
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.rtc_pixels import PixelEncoder


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "inputs", "pixels", "output"):
        p.add_argument("--"+name, type=Path, required=True)
    p.add_argument("--count", type=int, default=150)
    args = p.parse_args()
    lock = open("/home/user/Bruno/F1/runtime/control.lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    args.output.mkdir(parents=True, exist_ok=False)
    identity = {key: hashlib.sha256((args.pixels/name).read_bytes()).hexdigest()
                for key, name in (("implementation_sha256", "processing.py"),
                                  ("contract_sha256", "contract.json"))}
    encoders = {n: PixelEncoder(args.pixels, identity, workers=n) for n in (1, 2, 3)}
    observations = {r["observation_id"]: r for r in
                    map(json.loads, (args.run / "payload/frames.jsonl").open())}
    samples = []
    for entry in json.loads(args.inputs.read_text()):
        row = observations[entry["observation_id"]]
        frames = []
        for frame in row["frames"]:
            with (args.run / "payload" / frame["path"]).open("rb") as stream:
                stream.seek(frame["payload_offset"])
                payload = stream.read(frame["payload_bytes"])
            assert hashlib.sha256(payload).hexdigest() == frame["payload_sha256"]
            frames.append(TimestampedFrame(frame["camera"], frame["frame_id"],
                DeviceTimestamp(**frame["timestamp"]), frame["host_received_monotonic_ns"],
                payload, frame["media_type"], frame["width"], frame["height"]))
        samples.append(tuple(frames))
    rows, exact = [], 0
    try:
        for i in range(args.count):
            frames = samples[i % len(samples)]
            outputs = {}
            for n in tuple(1+(i+j) % 3 for j in range(3)):
                started = time.perf_counter()
                result = encoders[n].encode(frames)
                elapsed = (time.perf_counter()-started)*1000
                outputs[n] = result
                if i >= 10:
                    rows.append(dict(pair=i, workers=n, elapsed_ms=elapsed))
            assert outputs[1] == outputs[2] == outputs[3], "Changed pixels/PNG/metadata/order"
            exact += 1
    finally:
        for encoder in encoders.values():
            encoder.close()
    summary = dict(samples=len(samples), pairs=args.count, exact_pairs=exact,
                   robot_commands=0, preprocessing=identity, timing_ms={})
    for n in (1, 2, 3):
        summary["timing_ms"][str(n)] = dict(zip(("p50", "p95", "p99", "max"),
            np.percentile([r["elapsed_ms"] for r in rows if r["workers"] == n],
                          [50, 95, 99, 100]).tolist()))
    (args.output/"results.json").write_text(json.dumps(rows, indent=2))
    (args.output/"summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()

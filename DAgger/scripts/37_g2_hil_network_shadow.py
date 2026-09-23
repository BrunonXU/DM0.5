#!/usr/bin/env python3
"""Loopback HIL network diagnostic: no GDK, cameras, IK, or robot commands."""
import argparse
import json
from pathlib import Path
import select
import socket
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g2dagger.hil_network_shadow import HILNetworkShadow
from g2dagger.hil_shadow_server import serve_shadow
from g2dagger.protocol import MAX_WIRE_BYTES, ProtocolError, decode_intent, encode_packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7906)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    if args.port == 7904:
        parser.error("7904 is reserved for the existing teleop entry; use 7906")
    partial = args.log.with_name(args.log.name + ".partial")
    if args.log.exists() or partial.exists():
        raise FileExistsError(args.log)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    completed = False
    conn = None
    with partial.open("x") as handle:
        def emit(event, **fields):
            nonlocal count
            row = dict(sequence=count, event=event, hardware_output=False,
                       training_ready=False, **fields)
            count += 1
            line = json.dumps(row, separators=(",", ":"))
            handle.write(line + "\n")
            handle.flush()
            if event not in ("operator_packet", "synthetic_observation"):
                print(line, flush=True)
        engine = HILNetworkShadow(emit)
        emit("runtime_start", monotonic_ns=time.monotonic_ns(),
             observation_source="synthetic_no_cameras", policy_source="synthetic_no_model",
             hand_completion_source="simulated_one_second", log_io="synchronous_diagnostic_only")
        try:
            serve_shadow(engine, args.port, lambda text: print(text + f"; log={args.log}", flush=True))
        except KeyboardInterrupt:
            engine.disconnect(time.monotonic_ns(), "operator_stopped_receiver")
            completed = True
        finally:
            if conn:
                conn.close()
            emit("runtime_stop", monotonic_ns=time.monotonic_ns(), complete=completed)
    if completed:
        partial.rename(args.log)
        print(f"Finalized: {args.log}")


if __name__ == "__main__":
    main()

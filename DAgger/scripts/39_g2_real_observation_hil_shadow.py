#!/usr/bin/env python3
"""Real cameras + reported 34D + real policy; no robot action output."""
import argparse
from pathlib import Path
import sys
import threading
import time
import json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from g2dagger.hil_real_shadow import RealObservationHILShadow
from g2dagger.hil_shadow_server import serve_shadow
from g2dagger.hil_episode import HILEpisodeRecorder
from g2dagger.recording_sinks import make_recording_sink
from g2dagger.real_observation_source import TimestampedG2Source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=Path("/home/agi/xza/DM0.5"))
    parser.add_argument("--endpoint", default="ws://10.130.153.114:7892/process_frame")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("inference-only", "hil-record", "prebuffer-record"), default="prebuffer-record")
    parser.add_argument("--capture-hz", type=float, default=10.)
    parser.add_argument("--duration-seconds", type=float, default=120.)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    if not 0 < args.capture_hz <= 30 or not 0 < args.duration_seconds <= 600:
        parser.error("capture-hz must be (0,30], duration (0,600]")
    sys.path.insert(0, str(args.client_root))
    from g2_sort.deployment.g2_client.cameras import G2CameraRig
    from g2_sort.deployment.g2_client.robot_state import StrictRobotStateReader
    from g2_sort.deployment.g2_client.dm05_websocket_backend import DM05WebSocketBackend
    from g2_sort.deployment.g2_client.image_transport import EncodedImage
    from g2_sort.deployment.g2_client.app import _acquire_pid_file, _release_pid_file
    import numpy as np
    backend = DM05WebSocketBackend(args.endpoint, timeout_seconds=5.)
    if args.preflight:
        print("FILES/IMPORTS READY: GDK not initialized, no camera/Robot, no policy request.")
        print(f"Client source: {args.client_root}; capture={args.capture_hz}Hz; endpoint={args.endpoint}")
        return
    if args.output is None:
        parser.error("--output required unless --preflight")
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    pid_file = Path("/tmp/g2_dm05_runtime.pid")
    _acquire_pid_file(pid_file)
    gdk = None
    initialized = False
    cameras = recorder = sink = engine = None
    status = "aborted"
    lock = threading.Lock()
    log = (output / "runtime.jsonl.partial").open("x")
    sequence = 0
    def emit(event, **fields):
        nonlocal sequence
        with lock:
            row = dict(sequence=sequence, event=event, hardware_output=False,
                       training_ready=False, **fields)
            sequence += 1
            log.write(json.dumps(row, separators=(",", ":")) + "\n")
            log.flush()
            if recorder is not None:
                recorder.record_event(event, **fields)
            if event != "operator_packet":
                print(json.dumps(row), flush=True)
    try:
        import agibot_gdk as gdk
        if gdk.gdk_init() != gdk.GDKRes.kSuccess:
            raise RuntimeError("gdk_init failed")
        initialized = True
        robot = gdk.Robot()  # The ONLY Robot owner; only StrictRobotStateReader receives it.
        cameras = G2CameraRig(gdk_module=gdk, stabilization_seconds=3.)
        source = TimestampedG2Source(cameras, StrictRobotStateReader(robot))
        recorder = HILEpisodeRecorder(output / "journal", episode_id=output.name,
            task_prompt="real acquisition and policy HIL shadow; no execution or training labels")
        sink = make_recording_sink(args.mode, output / "payload")
        def infer(obs, request_id):
            return backend.infer_encoded(request_id=request_id,
                captured_at=obs.capture_started_monotonic_ns / 1e9,
                state=np.asarray(obs.measured_34d, dtype=np.float32),
                images=tuple(EncodedImage(f.payload, f.media_type, f.width, f.height) for f in obs.frames),
                observation_timing={})
        emit("real_shadow_start", monotonic_ns=time.monotonic_ns(), mode=args.mode,
             capture_hz=args.capture_hz, endpoint=args.endpoint,
             hand_feedback="raw_reported_values_continuous_semantics_unverified",
             control_evidence="absent_no_human_IK_no_execution")
        engine = RealObservationHILShadow(emit, source, infer, recorder, sink, capture_hz=args.capture_hz)
        deadline = time.monotonic() + args.duration_seconds
        try:
            serve_shadow(engine, 7906, print, stop=lambda: time.monotonic() >= deadline)
        except KeyboardInterrupt:
            pass
        status = "completed"
    finally:
        if engine is not None:
            # On native read stall this raises BEFORE cleanup can release GDK.
            engine.close()
        backend.close()
        if cameras is not None:
            cameras.close()
        if initialized:
            gdk.gdk_release()
        emit("real_shadow_stop", monotonic_ns=time.monotonic_ns(), status=status)
        payload_closed = sink.close(status=status) if sink is not None else False
        journal_closed = recorder.close(status=status) if recorder is not None else False
        log.close()
        if status == "completed":
            (output / "runtime.jsonl.partial").rename(output / "runtime.jsonl")
        _release_pid_file(pid_file)
        print(f"Output: {output}; journal_finalized={journal_closed}; payload_finalized={payload_closed}; training_ready=false")


if __name__ == "__main__":
    main()

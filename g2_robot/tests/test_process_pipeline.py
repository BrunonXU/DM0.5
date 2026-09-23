"""Non-GDK tests for the process-isolated RTC observation/policy boundary."""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from g2_sort.deployment.g2_client.cli import parse_args
from g2_sort.deployment.g2_client.app import run
from g2_sort.deployment.g2_client.contracts import (
    ACTION_DIM,
    CHUNK_STEPS,
    ObservationError,
    PolicyError,
)
from g2_sort.deployment.g2_client.process_pipeline import (
    PolicyWorkerConfig,
    ProcessInferencePipeline,
)
from g2_sort.deployment.g2_client.rtc import RTCParameters, RTCRequest
from g2_sort.deployment.g2_client.servo import ServoStreamer
from g2_sort.deployment.g2_client.state_ring import SharedStateRingBuffer


class _ControlStateSource:
    def __init__(self, state: np.ndarray | None = None) -> None:
        self.state = (
            np.zeros(ACTION_DIM, dtype=np.float32)
            if state is None
            else np.asarray(state, dtype=np.float32).copy()
        )
        self.read_count = 0

    def read_state(self) -> np.ndarray:
        self.read_count += 1
        return self.state.copy()


class _FailingControlStateSource:
    def read_state(self) -> np.ndarray:
        raise ObservationError("synthetic control state failure")


class _PolicyHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        type(self).requests += 1
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        time.sleep(0.03)
        payload = json.dumps(
            {
                "response": np.zeros(
                    (CHUNK_STEPS, ACTION_DIM), dtype=float
                ).tolist(),
                "timing_seconds": {"server_model_seconds": 0.001},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


@pytest.fixture
def policy_endpoint():
    _PolicyHandler.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PolicyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/process_frame"
    finally:
        server.shutdown()
        thread.join(timeout=1.0)
        server.server_close()


def test_process_pipeline_roundtrip_keeps_images_inside_worker(policy_endpoint):
    state_source = _ControlStateSource()
    pipeline = ProcessInferencePipeline(
        PolicyWorkerConfig(
            endpoint=policy_endpoint,
            request_timeout_seconds=2.0,
            camera_timeout_ms=50.0,
            gdk_stabilization_seconds=0.0,
            mock=True,
        ),
        state_source=state_source,
        startup_timeout_seconds=5.0,
    )
    send_times: list[float] = []

    def sender(_target: np.ndarray) -> bool:
        send_times.append(time.monotonic())
        return True

    servo = ServoStreamer(
        sender,
        np.zeros(ACTION_DIM, dtype=np.float64),
        servo_hz=100.0,
    )
    servo.start()
    try:
        bootstrap = pipeline.infer()
        parameters = RTCParameters(predicted_delay_steps=16, execution_horizon=20)
        rtc_request = RTCRequest(
            np.zeros((parameters.committed_tail_steps, ACTION_DIM)),
            parameters,
        )
        replanned = pipeline.infer(rtc_request)
    finally:
        servo.stop()
        pipeline.close()

    assert bootstrap.request_id == 0
    assert replanned.request_id == 1
    assert bootstrap.actions.shape == (CHUNK_STEPS, ACTION_DIM)
    assert bootstrap.timing_seconds["client_jpeg_encode_seconds"] > 0
    assert bootstrap.timing_seconds["client_control_state_read_seconds"] >= 0
    assert bootstrap.timing_seconds["client_state_ipc_wait_seconds"] >= 0
    assert bootstrap.timing_seconds["client_state_sample_age_seconds"] >= 0
    assert bootstrap.timing_seconds["client_worker_to_control_seconds"] >= 0
    assert bootstrap.timing_seconds["client_process_pipeline_seconds"] >= (
        bootstrap.response_received_at - bootstrap.request_started_at
    )
    assert _PolicyHandler.requests == 2
    assert state_source.read_count == 2
    state_stats = pipeline.control_state_stats()
    assert state_stats["read_count"] == 2
    assert state_stats["read_failure_count"] == 0
    assert state_stats["latest_ring_sequence"] == 1
    assert not pipeline.is_alive()
    intervals = np.diff(np.asarray(send_times))
    assert len(send_times) >= 5
    assert np.min(intervals) >= 0.005
    assert np.max(intervals) < 0.05


def test_process_pipeline_detects_worker_death(policy_endpoint):
    pipeline = ProcessInferencePipeline(
        PolicyWorkerConfig(
            endpoint=policy_endpoint,
            request_timeout_seconds=0.2,
            camera_timeout_ms=50.0,
            gdk_stabilization_seconds=0.0,
            mock=True,
        ),
        state_source=_ControlStateSource(),
        startup_timeout_seconds=5.0,
    )
    pipeline._process.terminate()
    pipeline._process.join(timeout=1.0)
    try:
        with pytest.raises(PolicyError, match="connection failed|exited"):
            pipeline.infer()
    finally:
        pipeline.close()


def test_process_pipeline_propagates_control_state_failure(policy_endpoint):
    pipeline = ProcessInferencePipeline(
        PolicyWorkerConfig(
            endpoint=policy_endpoint,
            request_timeout_seconds=1.0,
            camera_timeout_ms=50.0,
            gdk_stabilization_seconds=0.0,
            mock=True,
        ),
        state_source=_FailingControlStateSource(),
        startup_timeout_seconds=5.0,
    )
    try:
        with pytest.raises(PolicyError, match="synthetic control state failure"):
            pipeline.infer()
        assert pipeline.control_state_stats()["read_failure_count"] == 1
        assert _PolicyHandler.requests == 0
    finally:
        pipeline.close()


def test_shared_state_ring_rejects_stale_and_overwritten_snapshots():
    ring = SharedStateRingBuffer(
        context=mp.get_context("spawn"),
        capacity=2,
    )
    first = ring.publish(
        np.arange(ACTION_DIM, dtype=np.float64),
        sampled_at=10.0,
    )
    snapshot = ring.read(first.sequence, max_age_seconds=1.0, now=10.5)
    np.testing.assert_array_equal(snapshot.state, np.arange(ACTION_DIM))
    with pytest.raises(ObservationError, match="stale"):
        ring.read(first.sequence, max_age_seconds=0.1, now=10.5)

    ring.publish(np.ones(ACTION_DIM), sampled_at=11.0)
    ring.publish(np.full(ACTION_DIM, 2.0), sampled_at=12.0)
    with pytest.raises(ObservationError, match="overwritten"):
        ring.read(first.sequence)


def test_cli_process_isolation_supports_shadow_rtc_and_powered_sync():
    args = parse_args(
        ["--mode", "powered-rtc", "--rtc-process-isolation"]
    )
    assert args.rtc_process_isolation is True
    shadow = parse_args(
        ["--mode", "rtc-shadow", "--rtc-process-isolation"]
    )
    assert shadow.rtc_process_isolation is True
    sync = parse_args(
        ["--mode", "powered-sync", "--rtc-process-isolation"]
    )
    assert sync.rtc_process_isolation is True
    plain_shadow = parse_args(
        ["--mode", "shadow", "--rtc-process-isolation"]
    )
    assert plain_shadow.rtc_process_isolation is True
    with pytest.raises(SystemExit):
        parse_args(["--mode", "powered-async", "--rtc-process-isolation"])


def test_full_mock_powered_rtc_uses_process_pipeline(policy_endpoint, tmp_path):
    telemetry_path = tmp_path / "isolated_powered_rtc.jsonl"
    args = parse_args(
        [
            "--mode",
            "powered-rtc",
            "--mock",
            "--rtc-process-isolation",
            "--policy-endpoint",
            policy_endpoint,
            "--request-timeout-seconds",
            "2",
            "--rtc-predicted-delay-steps",
            "16",
            "--rtc-execution-horizon",
            "20",
            "--max-requests",
            "2",
            "--max-start-jump-radians",
            "1",
            "--boundary-blend-seconds",
            "0",
            "--no-interactive-keys",
            "--pid-file",
            str(tmp_path / "runtime.pid"),
            "--last-hand-command-json",
            str(tmp_path / "last_hand.json"),
            "--telemetry-jsonl",
            str(telemetry_path),
        ]
    )

    assert run(args) == 0
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    runtime = next(event for event in events if event["event"] == "runtime_start")
    assert runtime["rtc_process_isolation"] is True
    assert runtime["policy_worker_pid"] != runtime.get("control_pid")
    assert sum(event["event"] == "action_chunk" for event in events) == 2
    assert any(event["event"] == "rtc_switch" for event in events)


def test_full_mock_powered_sync_uses_process_pipeline(policy_endpoint, tmp_path):
    telemetry_path = tmp_path / "isolated_powered_sync.jsonl"
    args = parse_args(
        [
            "--mode",
            "powered-sync",
            "--mock",
            "--split-control-profile",
            "--rtc-process-isolation",
            "--policy-endpoint",
            policy_endpoint,
            "--request-timeout-seconds",
            "2",
            "--execute-prefix-steps",
            "1",
            "--max-requests",
            "2",
            "--max-start-jump-radians",
            "1",
            "--boundary-blend-seconds",
            "0",
            "--no-interactive-keys",
            "--pid-file",
            str(tmp_path / "runtime.pid"),
            "--last-hand-command-json",
            str(tmp_path / "last_hand.json"),
            "--telemetry-jsonl",
            str(telemetry_path),
        ]
    )

    assert run(args) == 0
    events = [
        json.loads(line)
        for line in telemetry_path.read_text(encoding="utf-8").splitlines()
    ]
    runtime = next(event for event in events if event["event"] == "runtime_start")
    assert runtime["mode"] == "powered-sync"
    assert runtime["rtc_process_isolation"] is True
    assert runtime["robot_owner"] == "control_process"
    assert runtime["policy_robot_reader"] is False
    assert runtime["policy_worker_pid"] != runtime["control_pid"]
    assert sum(event["event"] == "action_chunk" for event in events) == 2
    assert not any(event["event"] == "rtc_switch" for event in events)

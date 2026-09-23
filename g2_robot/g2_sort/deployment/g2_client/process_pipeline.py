"""Process-isolated G2 observation and DM0.5 policy pipeline.

The control process is the sole Robot owner.  After the camera worker captures
three images it asks control for one measured-state sample; control writes that
timestamped 34D sample into a shared ring and returns only its sequence.  The
worker owns Camera, JPEG, HTTP and response parsing but never creates Robot.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np

from .cameras import G2CameraRig
from .cleanup import close_resources
from .contracts import ActionChunk, Observation, ObservationError, PolicyError
from .dm05_backend import DM05HTTPBackend
from .dm05_websocket_backend import DM05WebSocketBackend
from .mock import MockCameraRig
from .observation import ObservationSource, StateSource
from .rtc import RTCRequest
from .state_ring import SharedStateRingBuffer

logger = logging.getLogger(__name__)


class _ControlStateSource:
    """Worker-side reader for state sampled by the control process."""

    def __init__(
        self,
        connection: Connection,
        ring: SharedStateRingBuffer,
        *,
        max_age_seconds: float,
    ) -> None:
        self._connection = connection
        self._ring = ring
        self._max_age_seconds = float(max_age_seconds)
        self._request_sequence: int | None = None
        self.last_timing_seconds: dict[str, float] = {}

    def begin(self, request_sequence: int) -> None:
        self._request_sequence = int(request_sequence)
        self.last_timing_seconds = {}

    def read_state(self) -> np.ndarray:
        if self._request_sequence is None:
            raise ObservationError("control state request sequence was not initialized")
        wait_started = time.monotonic()
        self._connection.send(
            {"op": "state_request", "sequence": self._request_sequence}
        )
        response = self._connection.recv()
        wait_finished = time.monotonic()
        if not isinstance(response, dict):
            raise ObservationError("control returned an invalid state response")
        if response.get("sequence") != self._request_sequence:
            raise ObservationError("control state response sequence mismatch")
        if response.get("op") == "state_error":
            raise ObservationError(
                f"control state read failed: {response.get('reason')}"
            )
        if response.get("op") != "state_ready":
            raise ObservationError(
                f"unexpected control state response: {response.get('op')!r}"
            )
        snapshot = self._ring.read(
            int(response["state_sequence"]),
            max_age_seconds=self._max_age_seconds,
            now=wait_finished,
        )
        self.last_timing_seconds = {
            "client_control_state_read_seconds": max(
                0.0, float(response.get("state_read_seconds", 0.0))
            ),
            "client_state_ipc_wait_seconds": max(
                0.0, wait_finished - wait_started
            ),
            "client_state_sample_age_seconds": snapshot.age_seconds(
                now=wait_finished
            ),
        }
        return snapshot.state.astype(np.float32, copy=False)


@dataclass(frozen=True)
class PolicyWorkerConfig:
    endpoint: str
    request_timeout_seconds: float
    camera_timeout_ms: float
    gdk_stabilization_seconds: float
    transport: str = "http-multipart-json"
    mock: bool = False
    mock_image_height: int = 96
    mock_image_width: int = 128
    max_state_age_seconds: float = 0.25
    native_log_dir: str | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if self.transport not in {"http-multipart-json", "websocket-binary"}:
            raise ValueError(f"unsupported policy transport {self.transport!r}")
        if self.max_state_age_seconds <= 0:
            raise ValueError("policy worker max state age must be positive")
        if self.mock_image_height <= 0 or self.mock_image_width <= 0:
            raise ValueError("policy worker mock image dimensions must be positive")


def _worker_main(
    connection: Connection,
    config: PolicyWorkerConfig,
    state_ring: SharedStateRingBuffer,
) -> None:
    """Own the complete variable-latency pipeline in one spawned process."""

    import os

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s[policy-worker]: %(message)s",
    )
    cameras: object | None = None
    backend: object | None = None
    gdk_module: Any | None = None
    gdk_initialized = False
    try:
        if config.mock:
            cameras = MockCameraRig(
                height=config.mock_image_height,
                width=config.mock_image_width,
            )
        else:
            if config.native_log_dir:
                Path(config.native_log_dir).mkdir(parents=True, exist_ok=True)
                os.environ["GLOG_log_dir"] = config.native_log_dir
            import agibot_gdk as gdk_module

            init_result = gdk_module.gdk_init()
            if init_result != gdk_module.GDKRes.kSuccess:
                raise RuntimeError(f"policy worker gdk_init failed: {init_result}")
            gdk_initialized = True
            cameras = G2CameraRig(
                timeout_ms=config.camera_timeout_ms,
                stabilization_seconds=config.gdk_stabilization_seconds,
            )
        state_source = _ControlStateSource(
            connection,
            state_ring,
            max_age_seconds=config.max_state_age_seconds,
        )
        observations = ObservationSource(cameras, state_source)  # type: ignore[arg-type]
        if config.transport == "websocket-binary":
            backend = DM05WebSocketBackend(
                endpoint=config.endpoint,
                timeout_seconds=config.request_timeout_seconds,
            )
        else:
            backend = DM05HTTPBackend(
                endpoint=config.endpoint,
                timeout_seconds=config.request_timeout_seconds,
            )
        connection.send({"op": "ready", "pid": os.getpid()})

        while True:
            message = connection.recv()
            if not isinstance(message, dict):
                raise RuntimeError("policy worker received a non-dictionary message")
            operation = message.get("op")
            if operation == "close":
                break
            if operation != "infer":
                raise RuntimeError(f"unknown policy worker operation: {operation!r}")
            sequence = int(message["sequence"])
            rtc_request = message.get("rtc_request")
            if rtc_request is not None and not isinstance(rtc_request, RTCRequest):
                raise RuntimeError("policy worker received an invalid RTC request")
            try:
                state_source.begin(sequence)
                if config.transport == "websocket-binary":
                    captured_at = time.monotonic()
                    capture_started = time.perf_counter()
                    images = cameras.capture_encoded()  # type: ignore[attr-defined]
                    images_finished = time.perf_counter()
                    state = state_source.read_state()
                    state_finished = time.perf_counter()
                    chunk = backend.infer_encoded(  # type: ignore[attr-defined]
                        request_id=sequence,
                        captured_at=captured_at,
                        state=state,
                        images=images,
                        observation_timing={
                            "client_camera_capture_seconds": max(
                                0.0, images_finished - capture_started
                            ),
                            "client_state_read_seconds": max(
                                0.0, state_finished - images_finished
                            ),
                            "client_observation_capture_seconds": max(
                                0.0, state_finished - capture_started
                            ),
                            "client_camera_decode_seconds": 0.0,
                            "client_jpeg_encode_seconds": 0.0,
                            "client_native_image_bytes_total": float(
                                sum(len(image.data) for image in images)
                            ),
                            "client_native_head_image_bytes": float(
                                len(images[0].data)
                            ),
                            "client_native_left_wrist_image_bytes": float(
                                len(images[1].data)
                            ),
                            "client_native_right_wrist_image_bytes": float(
                                len(images[2].data)
                            ),
                            **state_source.last_timing_seconds,
                        },
                        rtc_request=rtc_request,
                    )
                else:
                    observation = observations.capture()
                    observation = Observation(
                        request_id=observation.request_id,
                        captured_at=observation.captured_at,
                        images=observation.images,
                        state=observation.state,
                        prompt=observation.prompt,
                        timing_seconds={
                            **observation.timing_seconds,
                            **state_source.last_timing_seconds,
                        },
                    )
                    chunk = backend.infer(observation, rtc_request)  # type: ignore[attr-defined]
                connection.send(
                    {"op": "result", "sequence": sequence, "chunk": chunk}
                )
            except Exception as exc:
                connection.send(
                    {
                        "op": "error",
                        "sequence": sequence,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
    except EOFError:
        pass
    except BaseException as exc:
        try:
            connection.send(
                {
                    "op": "fatal",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        close_resources(
            (("policy cameras", cameras), ("policy backend", backend))
        )
        if gdk_initialized and gdk_module is not None:
            try:
                gdk_module.gdk_release()
            except Exception:
                logging.getLogger(__name__).exception(
                    "policy worker gdk_release failed"
                )
        connection.close()


class ProcessInferencePipeline:
    """Synchronous client for a spawned observation/policy worker.

    Calls are synchronous on purpose: RTC permits only one request in flight.
    The 100 Hz sender remains independent because all expensive work occurs in
    another process and this side only waits on a pipe that releases the GIL.
    """

    def __init__(
        self,
        config: PolicyWorkerConfig,
        *,
        state_source: StateSource,
        state_ring_capacity: int = 8,
        startup_timeout_seconds: float = 15.0,
        response_grace_seconds: float = 3.0,
        context: mp.context.BaseContext | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._config = config
        self._context = context or mp.get_context("spawn")
        self._state_source = state_source
        self._state_ring = SharedStateRingBuffer(
            context=self._context,
            capacity=state_ring_capacity,
        )
        parent, child = self._context.Pipe(duplex=True)
        self._connection = parent
        self._process = self._context.Process(
            target=_worker_main,
            args=(child, config, self._state_ring),
            name="g2-observation-policy",
            daemon=False,
        )
        self._response_timeout = (
            config.request_timeout_seconds + max(0.0, response_grace_seconds)
        )
        self._lock = threading.Lock()
        self._sequence = 0
        self._control_state_read_count = 0
        self._control_state_read_failure_count = 0
        self._control_state_read_max_seconds = 0.0
        self._closed = False
        self._cancel_event = cancel_event
        self._process.start()
        child.close()
        if not self._connection.poll(max(0.0, startup_timeout_seconds)):
            self.close()
            raise PolicyError("process-isolated policy worker startup timed out")
        message = self._connection.recv()
        if not isinstance(message, dict) or message.get("op") != "ready":
            self.close()
            reason = message.get("reason") if isinstance(message, dict) else message
            raise PolicyError(f"process-isolated policy worker failed: {reason}")
        self.worker_pid = int(message["pid"])

    def infer(self, rtc_request: RTCRequest | None = None) -> ActionChunk:
        if self._closed:
            raise PolicyError("process-isolated policy worker is closed")
        if not self._lock.acquire(blocking=False):
            raise PolicyError("process-isolated policy request already in flight")
        try:
            sequence = self._sequence
            self._sequence += 1
            try:
                self._connection.send(
                    {
                        "op": "infer",
                        "sequence": sequence,
                        "rtc_request": rtc_request,
                    }
                )
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise PolicyError("policy worker connection failed") from exc

            deadline = time.monotonic() + self._response_timeout
            while True:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise PolicyError("process-isolated policy request was cancelled")
                if not self._process.is_alive() and not self._connection.poll():
                    raise PolicyError(
                        f"policy worker exited with code {self._process.exitcode}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PolicyError(
                        "process-isolated policy request exceeded bounded wait"
                    )
                if not self._connection.poll(min(0.05, remaining)):
                    continue
                message = self._connection.recv()
                if (
                    isinstance(message, dict)
                    and message.get("op") == "state_request"
                ):
                    self._serve_state_request(message, expected_sequence=sequence)
                    continue
                break
            control_received_at = time.monotonic()
            if not isinstance(message, dict):
                raise PolicyError("policy worker returned an invalid message")
            if message.get("op") == "fatal":
                raise PolicyError(
                    f"policy worker fatal response: {message.get('reason')}"
                )
            if message.get("sequence") != sequence:
                raise PolicyError("policy worker response sequence mismatch")
            if message.get("op") == "error":
                raise PolicyError(
                    f"policy worker {message.get('error_type')}: "
                    f"{message.get('reason')}"
                )
            if message.get("op") != "result":
                raise PolicyError(
                    f"policy worker fatal response: {message.get('reason')}"
                )
            chunk = message.get("chunk")
            if not isinstance(chunk, ActionChunk):
                raise PolicyError("policy worker returned a non-ActionChunk result")
            timing = dict(chunk.timing_seconds)
            timing["client_worker_to_control_seconds"] = max(
                0.0, control_received_at - chunk.response_received_at
            )
            timing["client_process_pipeline_seconds"] = max(
                0.0, control_received_at - chunk.observation_at
            )
            model_seconds = timing.get(
                "server_model_and_device_to_cpu_seconds", 0.0
            )
            timing["client_e2e_to_control_seconds"] = timing[
                "client_process_pipeline_seconds"
            ]
            timing["client_non_model_to_control_seconds"] = max(
                0.0,
                timing["client_e2e_to_control_seconds"] - model_seconds,
            )
            worker_accounted = timing.get(
                "client_non_model_accounted_worker_seconds", 0.0
            )
            timing["client_non_model_accounted_to_control_seconds"] = (
                worker_accounted
                + timing["client_worker_to_control_seconds"]
            )
            timing["client_non_model_residual_to_control_seconds"] = max(
                0.0,
                timing["client_non_model_to_control_seconds"]
                - timing["client_non_model_accounted_to_control_seconds"],
            )
            # The RTC delay gate must include serialization and IPC delivery,
            # not stop its clock when the worker merely finishes HTTP parsing.
            return ActionChunk(
                request_id=chunk.request_id,
                observation_at=chunk.observation_at,
                request_started_at=chunk.request_started_at,
                response_received_at=control_received_at,
                actions=chunk.actions,
                timing_seconds=timing,
            )
        finally:
            self._lock.release()

    def _serve_state_request(
        self,
        message: dict[str, object],
        *,
        expected_sequence: int,
    ) -> None:
        if message.get("sequence") != expected_sequence:
            raise PolicyError("policy worker state request sequence mismatch")
        read_started = time.perf_counter()
        try:
            state = self._state_source.read_state()
            sampled_at = time.monotonic()
            read_seconds = max(0.0, time.perf_counter() - read_started)
            snapshot = self._state_ring.publish(
                state,
                sampled_at=sampled_at,
            )
            self._control_state_read_count += 1
            self._control_state_read_max_seconds = max(
                self._control_state_read_max_seconds,
                read_seconds,
            )
            self._connection.send(
                {
                    "op": "state_ready",
                    "sequence": expected_sequence,
                    "state_sequence": snapshot.sequence,
                    "state_read_seconds": read_seconds,
                }
            )
        except Exception as exc:
            self._control_state_read_failure_count += 1
            try:
                self._connection.send(
                    {
                        "op": "state_error",
                        "sequence": expected_sequence,
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                    }
                )
            except (BrokenPipeError, EOFError, OSError) as send_exc:
                raise PolicyError("control state error response failed") from send_exc

    def control_state_stats(self) -> dict[str, int | float]:
        return {
            "read_count": self._control_state_read_count,
            "read_failure_count": self._control_state_read_failure_count,
            "read_max_seconds": self._control_state_read_max_seconds,
            "latest_ring_sequence": self._state_ring.latest_sequence,
            "ring_capacity": self._state_ring.capacity,
        }

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            try:
                self._connection.send({"op": "close"})
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=2.0)
        if self._process.is_alive():
            logger.error("Policy worker did not stop cleanly; terminating it")
            self._process.terminate()
            self._process.join(timeout=1.0)
        self._connection.close()

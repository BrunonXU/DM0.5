"""Persistent binary WebSocket backend for the isolated DM0.5 policy worker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .binary_protocol import BinaryRequest, decode_response, encode_request
from .contracts import ActionChunk, PolicyError
from .image_transport import EncodedImage
from .rtc import RTCRequest


class DM05WebSocketBackend:
    """Send native compressed frames and receive fixed float32 action bytes."""

    def __init__(
        self,
        endpoint: str,
        timeout_seconds: float = 10.0,
        *,
        connector: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not endpoint.startswith(("ws://", "wss://")):
            raise ValueError("binary policy endpoint must start with ws:// or wss://")
        if timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if connector is None:
            try:
                from websockets.sync.client import connect as connector
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise PolicyError(
                    "websockets is required for binary DM0.5 transport"
                ) from exc
        self.endpoint = endpoint
        self.timeout_seconds = float(timeout_seconds)
        self._connector = connector
        self._clock = clock
        self._timer = timer
        self._connection: Any | None = None
        self._inflight_lock = threading.Lock()

    def _connect(self) -> Any:
        if self._connection is None:
            self._connection = self._connector(
                self.endpoint,
                compression=None,
                open_timeout=self.timeout_seconds,
                close_timeout=1.0,
                max_size=32 * 1024 * 1024,
            )
        return self._connection

    def infer_encoded(
        self,
        *,
        request_id: int,
        captured_at: float,
        state: np.ndarray,
        images: tuple[EncodedImage, EncodedImage, EncodedImage],
        observation_timing: dict[str, float],
        rtc_request: RTCRequest | None = None,
    ) -> ActionChunk:
        if not self._inflight_lock.acquire(blocking=False):
            raise PolicyError("DM0.5 WebSocket backend already has a request in flight")
        request_started_at = self._clock()
        try:
            binary_request = BinaryRequest(
                request_id=request_id,
                state=state,
                images=images,
                rtc_prefix=(
                    None
                    if rtc_request is None
                    else rtc_request.prefix_actions_absolute
                ),
                rtc_delay=(
                    0
                    if rtc_request is None
                    else rtc_request.parameters.predicted_delay_steps
                ),
                rtc_execution=(
                    0
                    if rtc_request is None
                    else rtc_request.parameters.execution_horizon
                ),
            )
            encode_started = self._timer()
            request_payload = encode_request(binary_request)
            encode_finished = self._timer()
            wire_started = self._timer()
            try:
                connection = self._connect()
                connection.send(request_payload)
                response_payload = connection.recv(timeout=self.timeout_seconds)
            except Exception:
                self._discard_connection()
                raise
            wire_finished = self._timer()
            if not isinstance(response_payload, bytes):
                raise PolicyError("DM0.5 WebSocket response must be a binary frame")
            decode_started = self._timer()
            response = decode_response(response_payload)
            decode_finished = self._timer()
            if response.request_id != request_id:
                raise PolicyError("DM0.5 WebSocket response request_id mismatch")
        except Exception as exc:
            if isinstance(exc, PolicyError):
                raise
            raise PolicyError(f"DM0.5 WebSocket request failed: {exc}") from exc
        finally:
            self._inflight_lock.release()

        response_received_at = self._clock()
        timing = dict(observation_timing)
        timing.update(response.timing_seconds)
        timing.update(
            {
                "client_binary_request_encode_seconds": max(
                    0.0, encode_finished - encode_started
                ),
                "client_websocket_roundtrip_seconds": max(
                    0.0, wire_finished - wire_started
                ),
                "client_binary_response_decode_seconds": max(
                    0.0, decode_finished - decode_started
                ),
                "client_binary_request_bytes": float(len(request_payload)),
                "client_binary_response_bytes": float(len(response_payload)),
                "client_observation_to_backend_seconds": max(
                    0.0, request_started_at - captured_at
                ),
                "client_e2e_to_worker_seconds": max(
                    0.0, response_received_at - captured_at
                ),
            }
        )
        model = timing.get("server_model_and_device_to_cpu_seconds", 0.0)
        handler = timing.get("server_handler_seconds")
        timing["client_non_model_to_worker_seconds"] = max(
            0.0, timing["client_e2e_to_worker_seconds"] - model
        )
        if handler is not None:
            transport = max(
                0.0, timing["client_websocket_roundtrip_seconds"] - handler
            )
            timing["client_transport_wire_residual_seconds"] = transport
            accounted = sum(
                timing.get(key, 0.0)
                for key in (
                    "client_observation_to_backend_seconds",
                    "client_binary_request_encode_seconds",
                    "client_transport_wire_residual_seconds",
                    "server_non_model_handler_seconds",
                    "client_binary_response_decode_seconds",
                )
            )
            timing["client_non_model_accounted_worker_seconds"] = accounted
            timing["client_non_model_residual_worker_seconds"] = max(
                0.0, timing["client_non_model_to_worker_seconds"] - accounted
            )
        return ActionChunk(
            request_id=request_id,
            observation_at=captured_at,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            actions=response.actions,
            timing_seconds=timing,
        )

    def _discard_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def close(self) -> None:
        self._discard_connection()


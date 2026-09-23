from __future__ import annotations

import threading

import numpy as np
import pytest

websockets = pytest.importorskip("websockets")

from websockets.sync.server import serve

from g2_sort.deployment.g2_client.binary_protocol import (
    decode_request,
    encode_response,
)
from g2_sort.deployment.g2_client.contracts import ACTION_DIM, CHUNK_STEPS
from g2_sort.deployment.g2_client.dm05_websocket_backend import (
    DM05WebSocketBackend,
)
from g2_sort.deployment.g2_client.image_transport import EncodedImage


def test_real_websocket_connection_reuses_one_socket_for_two_binary_requests():
    connections = 0
    request_ids = []

    def handler(connection):
        nonlocal connections
        connections += 1
        for payload in connection:
            request = decode_request(payload)
            request_ids.append(request.request_id)
            connection.send(
                encode_response(
                    request.request_id,
                    np.full(
                        (CHUNK_STEPS, ACTION_DIM),
                        request.request_id,
                        dtype=np.float32,
                    ),
                    {
                        "server_handler_seconds": 0.01,
                        "server_model_and_device_to_cpu_seconds": 0.005,
                        "server_non_model_handler_seconds": 0.005,
                    },
                )
            )

    with serve(handler, "127.0.0.1", 0, compression=None) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.socket.getsockname()[1]
        backend = DM05WebSocketBackend(f"ws://127.0.0.1:{port}/process_frame")
        images = tuple(
            EncodedImage(b"\xff\xd8" + bytes([index]), "image/jpeg", 1, 1)
            for index in (1, 2, 3)
        )
        for request_id in (1, 2):
            chunk = backend.infer_encoded(
                request_id=request_id,
                captured_at=0.0,
                state=np.zeros(ACTION_DIM, dtype=np.float32),
                images=images,  # type: ignore[arg-type]
                observation_timing={},
            )
            assert np.all(chunk.actions == request_id)
        backend.close()
        server.shutdown()
        thread.join(timeout=2.0)

    assert connections == 1
    assert request_ids == [1, 2]

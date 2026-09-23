"""Multipart HTTP policy transport, with no model/robot-specific dependencies."""
import json
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from f1_replay.bundle import decode_json
from f1_replay.runtime import ReplayError
from . import CONTRACT

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class HttpPolicy:
    def __init__(self, endpoint, timeout_s):
        parts = urlsplit(endpoint)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ReplayError("endpoint必须是无凭据、无query的HTTP(S)地址")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout_s
        # Robot observations stay on the configured route, not an inherited proxy.
        self.opener = build_opener(ProxyHandler({}))

    def _read(self, request):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ReplayError("推理响应超过大小限制")
            result = decode_json(body)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ReplayError(f"推理HTTP失败：{exc}") from exc
        if not isinstance(result, dict) or result.get("contract") != CONTRACT:
            raise ReplayError("响应不是F1推理合同，拒绝G2或其他模型输出")
        return result

    def health(self):
        result = self._read(Request(self.endpoint + "/health"))
        if result.get("ready") is not True or not result.get("model_binding"):
            raise ReplayError("推理服务未就绪或尚未选择checkpoint")
        return result

    def infer(self, observation, request_id, *, seed=None):
        metadata = {"contract": CONTRACT, "request_id": request_id,
                    "state19": list(observation.state19),
                    "observation_monotonic_ns": observation.captured_ns,
                    "reference_joints14_deg": list(observation.anchor_joints14)}
        if seed is not None:
            if type(seed) is not int or not 0 <= seed < 2**32:
                raise ReplayError("seed must be an integer in [0,2^32)")
            metadata["seed"] = seed
        boundary = "f1-" + uuid.uuid4().hex
        parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
                  'Content-Type: application/json\r\n\r\n').encode(),
                 json.dumps(metadata, allow_nan=False).encode(), b"\r\n"]
        for name in ("head", "left", "right"):
            parts.extend([(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                           f'filename="{name}.image"\r\nContent-Type: application/octet-stream\r\n\r\n').encode(),
                          observation.images[name], b"\r\n"])
        parts.append(f"--{boundary}--\r\n".encode())
        return self._read(Request(self.endpoint + "/process_frame", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                 method="POST"))

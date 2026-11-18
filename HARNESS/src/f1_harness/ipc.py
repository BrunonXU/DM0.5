"""Bounded same-UID Unix JSON RPC. Cancellation uses a separate connection/thread."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import threading

MAX_MESSAGE = 65536


def _read(sock, length):
    chunks = bytearray()
    while len(chunks) < length:
        data = sock.recv(length - len(chunks))
        if not data:
            raise ConnectionError("incomplete IPC frame")
        chunks.extend(data)
    return bytes(chunks)


def receive(sock, maximum=MAX_MESSAGE):
    length = struct.unpack("!I", _read(sock, 4))[0]
    if not 0 < length <= maximum:
        raise ValueError("IPC message size rejected")
    return json.loads(_read(sock, length), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def send(sock, value, maximum=MAX_MESSAGE):
    data = json.dumps(value, allow_nan=False).encode()
    if len(data) > maximum:
        raise ValueError("IPC message too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


class UnixClient:
    def __init__(self, path: Path, *, timeout=15., maximum=MAX_MESSAGE, scene_evidence=False):
        self.scene_evidence = scene_evidence
        self.maximum = maximum
        self.path, self.timeout = str(path), timeout

    def call(self, method, **kwargs):
        if self.scene_evidence:
            from .scene_evidence import pack
            kwargs=pack(kwargs)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.path)
            send(sock, dict(method=method, kwargs=kwargs), self.maximum)
            response = receive(sock, self.maximum)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "IPC failure"))
        result=response['result']
        if self.scene_evidence:
            from .scene_evidence import unpack
            result=unpack(result,Path(self.path).parent)
        return result


class UnixServer:
    def __init__(self, path: Path, dispatch, *, allowed_uid=None, timeout=15., max_clients=8, maximum=MAX_MESSAGE, client_context=False, scene_evidence=False):
        self.scene_evidence = scene_evidence
        self.client_context = client_context
        self.maximum = maximum
        self.path, self.dispatch = Path(path), dispatch
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self.timeout = timeout
        self.stop_event = threading.Event()
        self.slots = threading.BoundedSemaphore(max_clients)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.thread = None

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError("IPC path already exists; do not replace another owner")
        self.socket.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.socket.listen(8)
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def _serve(self):
        while not self.stop_event.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            with client:
                client.settimeout(self.timeout)
                _, uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.allowed_uid:
                    raise PermissionError("IPC peer uid mismatch")
                request = receive(client, self.maximum)
                if set(request) != {"method", "kwargs"} or not isinstance(request["kwargs"], dict):
                    raise ValueError("invalid IPC envelope")
                try:
                    if self.scene_evidence:
                        from .scene_evidence import unpack
                        request['kwargs']=unpack(request['kwargs'],self.path.parent)
                    result = (self.dispatch(request["method"], request["kwargs"], client=client)
                              if self.client_context else self.dispatch(request["method"], request["kwargs"]))
                    response = dict(ok=True, result=result)
                except Exception as exc:
                    response = dict(ok=False, error=f"{type(exc).__name__}: {exc}")
                if self.scene_evidence:
                    from .scene_evidence import pack
                    response=pack(response)
                send(client, response, self.maximum)
        except (OSError, ValueError, TypeError):
            pass
        finally:
            self.slots.release()

    def close(self):
        self.stop_event.set()
        self.socket.close()
        if self.thread:
            self.thread.join(timeout=1)
            self.path.unlink(missing_ok=True)

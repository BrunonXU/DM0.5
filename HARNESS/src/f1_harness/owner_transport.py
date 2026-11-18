"""Run-scoped private SSH channel. A lost reply never causes request replay."""
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import time
import uuid

from .site import _safe_environment

MAX_MESSAGE = 65536


class OwnerChannel:
    def __init__(self, command, log: Path, *, timeout=12):
        self.command, self.log, self.timeout = command, Path(log), timeout
        self.lock = threading.Lock()
        self.process = self.stderr = None
        self.closed = False

    def _start(self):
        if self.closed:
            raise RuntimeError('private_owner_channel_closed')
        if self.process is not None and self.process.poll() is None:
            return
        self._discard()
        self.stderr = self.log.open('ab')
        try:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, bufsize=0,
                env=_safe_environment(), start_new_session=True)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except BaseException:
            self._discard()
            raise

    def _discard(self):
        process, self.process = self.process, None
        if process is not None:
            # EOF also terminates the remote stream; no robot command is sent.
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        if self.stderr:
            self.stderr.close()
            self.stderr = None

    def call(self, method, **kwargs):
        request_id = uuid.uuid4().hex
        body = json.dumps(dict(id=request_id, method=method, kwargs=kwargs), allow_nan=False).encode()
        if len(body) > MAX_MESSAGE:
            raise ValueError('private_owner_request_too_large')
        with self.lock:
            self._start()
            deadline = time.monotonic() + self.timeout
            try:
                remaining = memoryview(body + b'\n')
                fd = self.process.stdin.fileno()
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([], [fd], [], wait)[1]:
                        raise TimeoutError('private_owner_write_timeout_result_unknown')
                    sent = os.write(fd, remaining)
                    if sent <= 0:
                        raise ConnectionError('private_owner_write_failed_result_unknown')
                    remaining = remaining[sent:]
                data = bytearray()
                fd = self.process.stdout.fileno()
                while b'\n' not in data:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([fd], [], [], wait)[0]:
                        raise TimeoutError('private_owner_reply_timeout_result_unknown')
                    chunk = os.read(fd, min(4096, MAX_MESSAGE + 2 - len(data)))
                    if not chunk:
                        raise ConnectionError('private_owner_eof_result_unknown')
                    data.extend(chunk)
                    if len(data) > MAX_MESSAGE + 1:
                        raise ValueError('private_owner_reply_too_large')
                if data[-1:] != b'\n' or b'\n' in data[:-1]:
                    raise ValueError('private_owner_ambiguous_reply')
                reply = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
                if (not isinstance(reply, dict) or reply.get('id') != request_id
                        or type(reply.get('ok')) is not bool):
                    raise ValueError('private_owner_reply_identity_mismatch')
            except BaseException:
                self._discard()
                # Reconnect only on a later, explicit call. In particular a
                # timed-out web_start/web_stop must be queried, never resent.
                raise
            if not reply['ok']:
                raise RuntimeError('控制端请求失败：' + method + '；' + str(reply.get('error', 'RPC failure'))[:1000])
            return reply['result']

    def close(self):
        with self.lock:
            self.closed = True
            self._discard()

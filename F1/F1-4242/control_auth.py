"""Runtime-only control credential and bounded verification; no robot operations."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

ITERATIONS = 600_000

class AuthThrottled(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after) + 1)
        super().__init__('控制码尝试过多，请稍后重试')


def provision(path, code):
    if not isinstance(code, str) or not 8 <= len(code) <= 128:
        raise ValueError('控制码长度须为 8–128 字符')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    payload = {'version': 1, 'kdf': 'pbkdf2_sha256', 'iterations': ITERATIONS,
               'salt': salt.hex(), 'digest': hashlib.pbkdf2_hmac('sha256', code.encode(), salt, ITERATIONS).hex()}
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ControlCode:
    def __init__(self, path, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.Lock()
        self.attempts = {}
        self.global_attempts = []

    def configured(self):
        return self.path.is_file() and not self.path.is_symlink()

    def verify(self, code, client):
        # Key by transport IP, never by caller-supplied cookie or X-Forwarded-For.
        with self.lock:
            now = self.clock()
            self.attempts = {key: [t for t in values if now - t < 60]
                             for key, values in self.attempts.items() if values and now - values[-1] < 60}
            self.global_attempts = [t for t in self.global_attempts if now - t < 60]
            attempts = self.attempts.setdefault(client, [])
            if len(attempts) >= 5:
                raise AuthThrottled(60 - (now - attempts[0]))
            if len(self.global_attempts) >= 30:
                raise AuthThrottled(60 - (now - self.global_attempts[0]))
            # Consume before KDF so concurrent requests cannot bypass the limit.
            attempts.append(now); self.global_attempts.append(now)
            if not self.configured():
                raise ValueError('本机尚未配置控制码')
            if not isinstance(code, str) or not 8 <= len(code) <= 128:
                return False
            if self.path.stat().st_size > 4096:
                raise ValueError('控制码配置无效')
            try:
                payload = json.loads(self.path.read_text())
                if payload['kdf'] != 'pbkdf2_sha256' or payload['version'] != 1 or payload['iterations'] != ITERATIONS:
                    raise ValueError('控制码配置无效')
                salt, expected = bytes.fromhex(payload['salt']), bytes.fromhex(payload['digest'])
                if len(salt) != 32 or len(expected) != 32:
                    raise ValueError('控制码配置无效')
                valid = hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', code.encode(), salt, ITERATIONS), expected)
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError('控制码配置无效') from exc
            if valid:
                # Successful uses should not penalize normal release/reacquire.
                self.attempts.pop(client, None)
                self.global_attempts.pop()
            return valid


def main():
    parser = argparse.ArgumentParser(description='Set runtime control code from stdin; never pass the code in argv')
    parser.add_argument('--runtime', required=True)
    args = parser.parse_args()
    code = sys.stdin.readline().rstrip('\r\n')
    provision(Path(args.runtime) / 'control_code.json', code)
    print('Control credential provisioned (mode 0600); plaintext was not saved.')

if __name__ == '__main__':
    main()

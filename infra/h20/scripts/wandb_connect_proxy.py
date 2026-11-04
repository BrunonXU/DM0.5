#!/usr/bin/env python3
"""Local-only TLS CONNECT relay for H20 W&B over an authenticated SSH tunnel."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import os
from urllib.parse import urlsplit

ALLOWED = {"api.wandb.ai", "files.wandb.ai", "storage.googleapis.com"}


def connect_destination(host, port, upstream=None):
    """TLS stays end-to-end; optionally reuse this host's local egress proxy."""
    if host not in ALLOWED or str(port) != '443':
        raise ValueError('W&B destinations only')
    if not upstream:
        return socket.create_connection((host, int(port)), timeout=20)
    proxy = urlsplit(upstream)
    if (proxy.scheme != 'http' or proxy.hostname not in ('127.0.0.1', 'localhost')
            or not proxy.port or proxy.username or proxy.password or proxy.path not in ('', '/')
            or proxy.query or proxy.fragment):
        raise ValueError('Expected unauthenticated loopback HTTP CONNECT proxy')
    connection = socket.create_connection((proxy.hostname, proxy.port), timeout=20)
    try:
        connection.sendall(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n'.encode('ascii'))
        response = bytearray()
        while not response.endswith(b'\r\n\r\n'):
            byte = connection.recv(1)
            if not byte or len(response) >= 8192:
                raise OSError('Invalid upstream CONNECT response')
            response.extend(byte)
        if response.split(b'\r\n', 1)[0].split()[1:2] != [b'200']:
            raise OSError('Upstream CONNECT rejected')
        return connection
    except BaseException:
        connection.close()
        raise


class Handler(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        host, _, port = self.path.rpartition(":")
        if host not in ALLOWED or port != "443":
            self.send_error(403, "W&B destinations only")
            return
        try:
            remote = connect_destination(host, port, os.environ.get('WANDB_UPSTREAM_PROXY'))
        except (OSError, ValueError):
            self.send_error(502, "Destination unavailable")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self.connection.settimeout(None); remote.settimeout(None)
        try:
            while True:
                ready, _, _ = select.select([self.connection, remote], [], [], 180)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.connection else self.connection).sendall(data)
        except OSError:
            pass
        finally:
            remote.close()
            self.close_connection = True

    def log_message(self, *_):
        pass  # No URLs, headers, credentials, or payload logging.


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18096), Handler).serve_forever()

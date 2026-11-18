#!/usr/bin/env python3
"""One-time official browser consent; no robot connection or model generation."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from f1_harness.subscription_auth import SignIn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--port', type=int, default=1455)
    args = parser.parse_args()
    outcome = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes; never access-log them.

        def do_GET(self):
            parsed = urlsplit(self.path)
            headers = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}
            if self.headers.get('Host') != f'127.0.0.1:{args.port}':
                status, body = 400, b'Invalid callback host.'
            elif parsed.path == '/start' and not attempt.consumed:
                status, body = 302, b''
                headers['Location'] = attempt.authorization_url()
            elif parsed.path == '/auth/callback':
                try:
                    query = parse_qs(parsed.query, strict_parsing=True)
                    if any(len(v) != 1 for v in query.values()):
                        raise ValueError('duplicate_parameter')
                    attempt.complete({k: v[0] for k, v in query.items()})
                    status, body = 200, b'F1 Harness authorized. You may close this tab.'
                    outcome.append('authorized')
                except Exception:
                    status, body = 400, b'Authorization failed. Start a fresh sign-in; do not share this URL.'
                    if attempt.consumed:
                        outcome.append('authorization_failed')
            else:
                status, body = 404, b'Not found.'
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(('127.0.0.1', args.port), Handler) as server, httpx.Client(timeout=30) as client:
        server.timeout = 1
        attempt = SignIn(args.credentials, f'http://127.0.0.1:{args.port}/auth/callback', client)
        print(f'Continue with ChatGPT: http://127.0.0.1:{args.port}/start', flush=True)
        while not outcome and time.monotonic() < attempt.deadline:
            server.handle_request()
    print(outcome[0] if outcome else 'authorization_expired', flush=True)
    return 0 if outcome == ['authorized'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

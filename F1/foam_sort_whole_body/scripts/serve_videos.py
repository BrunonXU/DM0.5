#!/usr/bin/env python3
"""Local video browser with single-byte-range support for HTML5 MP4 seeking."""
import argparse
from contextlib import ExitStack
import errno
import functools
import http.server
from pathlib import Path
import re
import socket
import threading


class VideoHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()

    def send_head(self):
        self.remaining = None
        filename = Path(self.translate_path(self.path))
        requested = self.headers.get('Range')
        # An If-Range validator is not evaluated here: send the whole response.
        if not requested or not filename.is_file() or self.headers.get('If-Range'):
            return super().send_head()
        try:
            stream = filename.open('rb')
        except OSError:
            self.send_error(404, 'File not found')
            return None
        size = filename.stat().st_size
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested.strip())
        # Multiple/unsupported ranges may be ignored under HTTP semantics.
        if not match:
            stream.close()
            return super().send_head()
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), size-1) if last else size-1
        else:
            suffix = int(last) if last else 0
            start, end = max(0, size-suffix), size-1
        if start > end or start >= size or size == 0:
            stream.close()
            self.send_response(416, 'Range Not Satisfiable')
            self.send_header('Content-Range', f'bytes */{size}')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return None
        stream.seek(start)
        self.remaining = end-start+1
        self.send_response(206, 'Partial Content')
        self.send_header('Content-Type', self.guess_type(str(filename)))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(self.remaining))
        self.send_header('Last-Modified', self.date_time_string(filename.stat().st_mtime))
        self.end_headers()
        return stream

    def copyfile(self, source, outputfile):
        try:
            if self.remaining is None:
                return super().copyfile(source, outputfile)
            remaining = self.remaining
            while remaining:
                block = source.read(min(1024*1024, remaining))
                if not block:
                    break
                outputfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers cancel downloads when seeking or closing the player.
            return


class IPv6LoopbackServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1]/'results/20260914/videos')
    parser.add_argument('--port', type=int, default=8876)
    args = parser.parse_args()
    if not (args.directory/'index.html').is_file():
        parser.error('Video directory must contain index.html')
    handler = functools.partial(VideoHandler, directory=str(args.directory.resolve()))
    with ExitStack() as stack:
        server = stack.enter_context(http.server.ThreadingHTTPServer(('127.0.0.1', args.port), handler))
        print(f'Video browser: http://127.0.0.1:{args.port}/index.html', flush=True)
        try:
            ipv6 = stack.enter_context(IPv6LoopbackServer(('::1', args.port), handler))
        except OSError as exc:
            if exc.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                raise
            print(f'IPv6 loopback unavailable: {exc}', flush=True)
        else:
            threading.Thread(target=ipv6.serve_forever, daemon=True).start()
            print(f'Video browser: http://[::1]:{args.port}/index.html', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()

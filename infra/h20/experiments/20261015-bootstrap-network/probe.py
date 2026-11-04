#!/usr/bin/env python3
"""Bounded HTTPS mirror probes; no installs, credentials, or configuration writes."""
import argparse
import concurrent.futures
import datetime
import hashlib
import html.parser
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit, urldefrag

WHEEL = 'numpy-1.26.4-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl'
EXPECTED_SHA256 = '666dbfb6ec68962c033a450943ded891bed2d54e6755e35e5835d63f4f6931d5'
EXPECTED_BYTES = 18252005
OFFICIAL_URL = 'https://files.pythonhosted.org/packages/3a/d0/edc009c27b406c4f9cbc79274d6e46d634d139075492ad055e3d68445925/' + WHEEL
SOURCES = {
    'pypi': 'https://pypi.org/simple/numpy/',
    'tsinghua': 'https://pypi.tuna.tsinghua.edu.cn/simple/numpy/',
    'aliyun': 'https://mirrors.aliyun.com/pypi/simple/numpy/',
    'ustc': 'https://mirrors.ustc.edu.cn/pypi/simple/numpy/',
    'douban': 'https://pypi.doubanio.com/simple/numpy/',
}
CHANNELS = {
    'huggingface': 'https://huggingface.co/api/models/Physical-Intelligence/pi05_base',
    'hf_mirror': 'https://hf-mirror.com/api/models/Physical-Intelligence/pi05_base',
    'google_storage': 'https://storage.googleapis.com/',
    'github_releases': 'https://github.com/astral-sh/uv/releases',
    'pytorch_cu128': 'https://download.pytorch.org/whl/cu128/',
}


class Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href')


def curl(url, destination, mode, *, head=False, max_seconds=20):
    command = ['curl', '--silent', '--show-error', '--location', '--fail',
               '--proto', '=https', '--proto-redir', '=https',
               '--connect-timeout', '5', '--max-time', str(max_seconds),
               '--max-filesize', str(20 * 1024 * 1024),
               '--output', str(destination), '--write-out',
               '%{http_code}\t%{size_download}\t%{time_total}\t%{url_effective}']
    if mode == 'direct':
        command += ['--noproxy', '*']
    elif mode == 'company-proxy':
        command += ['--proxy', 'http://192.0.2.34:3128', '--noproxy', '']
    if head:
        command.append('--head')
    command.append(url)
    result = subprocess.run(command, capture_output=True, text=True, timeout=max_seconds + 5)
    fields = result.stdout.split('\t')
    record = {'request_url': url, 'returncode': result.returncode,
              'error': result.stderr.strip()[:600], 'http': None,
              'bytes_downloaded': 0, 'seconds': None}
    if len(fields) == 4:
        code, count, seconds, effective = fields
        record.update(http=int(code), bytes_downloaded=int(float(count)),
                      seconds=float(seconds), effective_url=effective)
    return record


def package_probe(item, mode, official_file_only=False):
    name, index = item
    record = {'source': name, 'index': index, 'mode': mode}
    with tempfile.TemporaryDirectory(prefix='h20-network-probe-') as temporary:
        index_file = Path(temporary) / 'index.html'
        metadata = ({'returncode': 0, 'skipped': 'known official artifact URL used'}
                    if official_file_only and name == 'pypi'
                    else curl(index, index_file, mode, max_seconds=10))
        record['metadata'] = metadata
        if metadata['returncode']:
            record['status'] = 'index_unavailable'
            return record
        links = Links()
        if index_file.exists():
            links.feed(index_file.read_text(errors='replace'))
        matches = ([OFFICIAL_URL] if official_file_only and name == 'pypi' else
                   [urljoin(metadata.get('effective_url', index), link) for link in links.links
                    if Path(urlsplit(link).path).name == WHEEL])
        if not matches:
            record['status'] = 'fixed_wheel_not_listed'
            return record
        wheel_url = urldefrag(matches[0])[0]
        wheel_file = Path(temporary) / WHEEL
        download = curl(wheel_url, wheel_file, mode)
        record['download'] = download
        if wheel_file.exists():
            size = wheel_file.stat().st_size
            digest = hashlib.sha256(wheel_file.read_bytes()).hexdigest()
            download.update(file_bytes=size, sha256=digest,
                            matches_official_sha256=digest == EXPECTED_SHA256)
        else:
            download.update(file_bytes=0, sha256=None, matches_official_sha256=False)
        valid = (download['returncode'] == 0 and download['file_bytes'] == EXPECTED_BYTES
                 and download['matches_official_sha256'])
        record['status'] = 'verified' if valid else 'download_incomplete_or_invalid'
        download['verified_mib_per_second'] = (
            EXPECTED_BYTES / 1048576 / download['seconds'] if valid and download['seconds'] else None)
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['direct', 'environment', 'company-proxy'], default='direct')
    parser.add_argument('--channels-only', action='store_true')
    parser.add_argument('--skip-channels', action='store_true')
    parser.add_argument('--source', choices=list(SOURCES), action='append')
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    parser.add_argument('--official-file-only', action='store_true',
                        help='For PyPI use official artifact URL when a large index times out')
    args = parser.parse_args()
    proxy_summary = []
    for name, value in os.environ.items():
        if name.lower() in ('http_proxy', 'https_proxy', 'all_proxy'):
            parsed = urlsplit(value)
            proxy_summary.append({'variable': name, 'scheme': parsed.scheme, 'host': parsed.hostname,
                                  'port': parsed.port, 'contains_auth': parsed.username is not None})
    output = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'hostname': socket.gethostname(), 'mode': args.mode, 'proxy_summary': proxy_summary,
              'wheel': WHEEL, 'official_url': OFFICIAL_URL, 'expected_sha256': EXPECTED_SHA256,
              'expected_bytes': EXPECTED_BYTES, 'packages': [], 'channels': []}
    if not args.channels_only:
        selected = {name: url for name, url in SOURCES.items() if not args.source or name in args.source}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            output['packages'] = list(pool.map(lambda item: package_probe(item, args.mode, args.official_file_only), selected.items()))
    output['package_workers'] = args.workers
    with tempfile.TemporaryDirectory(prefix='h20-channel-probe-') as temporary:
        for name, url in ({} if args.skip_channels else CHANNELS).items():
            response = curl(url, Path(temporary) / name, args.mode, head=True, max_seconds=10)
            response['channel'] = name
            output['channels'].append(response)
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

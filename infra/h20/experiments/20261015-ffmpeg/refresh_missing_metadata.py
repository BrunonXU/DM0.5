#!/usr/bin/env python3
"""Fetch and verify Ubuntu metadata in a private directory; never updates apt state."""
import datetime
import hashlib
import json
import lzma
from pathlib import Path
import re
import subprocess

BASE = 'https://archive.ubuntu.com/ubuntu/dists/jammy-updates/'
STAGING = Path('/u01/user/staging/ffmpeg-4.4.2-jammy')
DIRECTORY = STAGING / 'current-ubuntu-metadata'
DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)


def download(name, path):
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                    '--proto', '=https', '--proto-redir', '=https',
                    '--proxy', 'http://192.0.2.34:3128', '--noproxy', '',
                    '--connect-timeout', '5', '--max-time', '60', '--output', str(path),
                    BASE + name], check=True, timeout=65)


release = DIRECTORY / 'InRelease'
index = DIRECTORY / 'Packages.xz'
download('InRelease', release)
signature = subprocess.run(['gpgv', '--keyring', '/usr/share/keyrings/ubuntu-archive-keyring.gpg', str(release)],
                           capture_output=True, text=True, check=True)
text = release.read_text()
section = text.split('SHA256:\n', 1)[1].split('\nSHA512:', 1)[0]
expected_hash, expected_size = None, None
for line in section.splitlines():
    pieces = line.split()
    if len(pieces) == 3 and pieces[2] == 'main/binary-amd64/Packages.xz':
        expected_hash, expected_size = pieces[0], int(pieces[1])
        break
assert expected_hash and re.fullmatch('[0-9a-f]{64}', expected_hash)
download('main/binary-amd64/Packages.xz', index)
assert index.stat().st_size == expected_size
assert hashlib.sha256(index.read_bytes()).hexdigest() == expected_hash
wanted = {'libgfortran5', 'libopenjp2-7', 'libssh-gcrypt-4', 'libvpx7'}
packages = []
for paragraph in lzma.decompress(index.read_bytes()).decode().split('\n\n'):
    fields = {}
    for line in paragraph.splitlines():
        if line and not line.startswith(' ') and ': ' in line:
            key, value = line.split(': ', 1)
            fields[key] = value
    if fields.get('Package') in wanted and fields.get('Architecture') == 'amd64':
        packages.append({'name': fields['Package'], 'version': fields['Version'], 'size': int(fields['Size']),
                         'filename': fields['Filename'], 'sha256': fields['SHA256'],
                         'depends': fields.get('Depends'), 'metadata_source': BASE + 'main/binary-amd64/Packages.xz'})
assert {p['name'] for p in packages} == wanted
receipt = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'release_url': BASE + 'InRelease', 'release_sha256': hashlib.sha256(release.read_bytes()).hexdigest(),
           'release_date': re.search(r'^Date: (.*)$', text, re.M).group(1),
           'gpgv_stderr': signature.stderr, 'index_sha256': expected_hash,
           'index_size': expected_size, 'packages': packages}
(STAGING / 'replacements.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))

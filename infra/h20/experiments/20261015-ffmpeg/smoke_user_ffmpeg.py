#!/usr/bin/env python3
"""Test only the private FFmpeg runtime; no GPU or training execution."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess

PREFIX = Path('/u01/user/tools/ffmpeg-4.4.2-jammy')
STAGING = Path('/u01/user/staging/ffmpeg-4.4.2-jammy')
VIDEO = STAGING / 'smoke-64x64-8frames.mp4'
env = os.environ.copy()
env['LD_LIBRARY_PATH'] = ':'.join(str(PREFIX / relative) for relative in [
    'usr/lib/x86_64-linux-gnu', 'usr/lib/x86_64-linux-gnu/blas',
    'usr/lib/x86_64-linux-gnu/lapack', 'usr/lib/x86_64-linux-gnu/pulseaudio'])


def run(arguments):
    result = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


run([str(PREFIX / 'usr/bin/ffmpeg'), '-v', 'error', '-nostdin', '-n',
     '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=8', '-t', '1',
     '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-threads', '1',
     '-filter_threads', '1', '-filter_complex_threads', '1', str(VIDEO)])
probe = json.loads(run([str(PREFIX / 'usr/bin/ffprobe'), '-v', 'error', '-count_frames',
                       '-select_streams', 'v:0', '-show_entries',
                       'stream=codec_name,width,height,nb_read_frames', '-of', 'json', str(VIDEO)]))
assert probe['streams'][0] == {'codec_name': 'h264', 'width': 64, 'height': 64, 'nb_read_frames': '8'}
run([str(PREFIX / 'usr/bin/ffmpeg'), '-v', 'error', '-nostdin', '-threads', '1',
     '-i', str(VIDEO), '-f', 'null', '-'])
ldd = {}
for relative in ['usr/bin/ffmpeg', 'usr/bin/ffprobe', 'usr/lib/x86_64-linux-gnu/libavcodec.so.58',
                 'usr/lib/x86_64-linux-gnu/libavformat.so.58', 'usr/lib/x86_64-linux-gnu/libavutil.so.56']:
    ldd[relative] = run(['ldd', str(PREFIX / relative)])
    assert 'not found' not in ldd[relative]
status_sha = hashlib.sha256(Path('/var/lib/dpkg/status').read_bytes()).hexdigest()
receipt = {'timestamp_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'status': 'PASS',
           'scope': 'CPU FFmpeg encode, ffprobe frame count, FFmpeg decode; no TorchCodec or GPU test',
           'video': str(VIDEO), 'video_sha256': hashlib.sha256(VIDEO.read_bytes()).hexdigest(),
           'probe': probe, 'final_prefix_ldd': ldd,
           'dpkg_status_sha256_after': status_sha,
           'dpkg_status_sha256_during_download': 'a889cb7e57612379c50ea81676baef2b8ff3b564226ae7b588f8ad33ad48dca1'}
receipt['system_dpkg_status_unchanged'] = status_sha == receipt['dpkg_status_sha256_during_download']
assert receipt['system_dpkg_status_unchanged']
(STAGING / 'smoke.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))

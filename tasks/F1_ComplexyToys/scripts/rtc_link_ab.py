"""Interleaved equal-payload link comparison from F1's existing raw recordings.

Never imports a robot adapter or creates a command publisher. The existing
control lock prevents competing with an onsite run during the measurement.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import time
import uuid
from urllib.request import urlopen

import numpy as np
from websockets.sync.client import connect
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.rtc_pixels import PixelEncoder
from f1dagger.websocket_policy import encode_request


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--pixels', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--count', type=int, default=100)
    p.add_argument('--mode', choices=('echo', 'rtc'), default='echo')
    p.add_argument('--ports', type=int, nargs=2, default=[8135, 8136])
    a = p.parse_args()
    lock = open('/home/user/Bruno/F1/runtime/control.lock', 'a+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    a.output.mkdir(parents=True, exist_ok=False)
    health = [json.load(urlopen(f'http://127.0.0.1:{port}/health', timeout=5)) for port in a.ports]
    assert health[0] == health[1]
    encoder = PixelEncoder(a.pixels, health[0]['input_preprocessing'])
    rows = [json.loads(line) for line in (a.run/'payload/frames.jsonl').open()]
    samples = []
    for row in rows[::max(1, len(rows)//12)][:12]:
        frames = []
        for f in row['frames']:
            with (a.run/'payload'/f['path']).open('rb') as stream:
                stream.seek(f['payload_offset']); data = stream.read(f['payload_bytes'])
            assert hashlib.sha256(data).hexdigest() == f['payload_sha256']
            frames.append(TimestampedFrame(f['camera'], f['frame_id'], DeviceTimestamp(**f['timestamp']),
                f['host_received_monotonic_ns'], data, f['media_type'], f['width'], f['height']))
        samples.append((row, encoder.encode(frames)))
    suffix = '/echo' if a.mode == 'echo' else '/ws'
    sockets = [connect(f'ws://127.0.0.1:{port}{suffix}', compression=None, proxy=None,
                       open_timeout=5, close_timeout=1, max_size=4*1024**2) for port in a.ports]
    records = []; previous = None; session = uuid.uuid4().hex
    try:
        for i in range(a.count):
            row, frames = samples[i % len(samples)]
            bootstrap = previous is None or not previous.get('success') or a.mode == 'echo'
            meta = dict(contract='f1_upper_16d_v1', protocol='f1_eef_rtc_v2', session_id=session,
                request_id=i+1, observation_id=row['observation_id'], policy_epoch=0,
                observation_monotonic_ns=row['capture_started_monotonic_ns'],
                state16=row['state16'], last_submitted16=row['state16'],
                state_timestamps_ns=row['state_timestamps_ns'], prediction_horizon=50,
                execute_steps=20, chunk_steps=20, frequency_hz=30., shadow_only=True,
                model_id=health[0]['model_id'], model_version=health[0]['model_version'],
                prepared_pixels=encoder.identity,
                rtc=dict(bootstrap=bootstrap, delay_steps=20, shift_steps=20, coverage_steps=42,
                    plan_origin_ns=time.monotonic_ns(), base_plan_id=0 if bootstrap else i,
                    committed_actions16=[] if bootstrap else previous['actions'][20:40],
                    previous_absolute_eef10=[] if bootstrap else previous['absolute_eef10'][20:]))
            payload = encode_request(meta, frames); responses = {}
            for index in ((0, 1) if i % 2 == 0 else (1, 0)):
                start = time.perf_counter(); sockets[index].send(payload)
                response = json.loads(sockets[index].recv(timeout=10))
                elapsed = (time.perf_counter()-start)*1000
                assert response.get('session_id') == session and response.get('request_id') == i+1, response
                record = dict(pair=i, port=a.ports[index], rtt_ms=elapsed,
                    payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
                    success=response.get('success'), server=response.get('timing_ms'))
                records.append(record); responses[index] = response
                with (a.output/'requests.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n')
            previous = responses[1]
            if i % 20 == 0:print(json.dumps({'completed_pairs':i+1}), flush=True)
    finally:
        for socket in sockets:socket.close()
    summary = dict(mode=a.mode, pairs=a.count, robot_commands=0, ports={}, source_run=str(a.run))
    for port in a.ports:
        values = [r['rtt_ms'] for r in records if r['port'] == port and r['pair'] >= 5]
        summary['ports'][port] = dict(zip(('p50','p95','p99','max'), np.percentile(values,[50,95,99,100]).tolist()))
    (a.output/'report.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Recorded-input protocol check using the inference image's existing aiohttp."""
import argparse
import asyncio
import json
from pathlib import Path
import struct
import time
import uuid

import aiohttp
import numpy as np


async def check(args):
    rows = [json.loads(x) for x in (args.dataset / "samples.jsonl").read_text().splitlines()]
    arrays = np.load(args.dataset / "arrays.npz")
    session_id = uuid.uuid4().hex
    records = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client:
        async with client.get(args.endpoint + '/health') as response:
            health = await response.json()
        async with client.ws_connect(args.endpoint + '/ws', compress=0, max_msg_size=4*1024**2) as ws:
            for i, index in enumerate(np.linspace(0, len(rows)-1, 10, dtype=int)):
                chunks, frames = [], []
                for camera in ('head', 'left_wrist', 'right_wrist'):
                    chunk = (args.dataset / rows[index]['images'][camera]).read_bytes()
                    width, height = (1280, 720) if camera == 'head' else (640, 480)
                    chunks.append(chunk)
                    frames.append(dict(camera=camera, encoding='image/jpeg', width=width, height=height, length=len(chunk)))
                meta = dict(contract='f1_upper_16d_v1', protocol='f1_eef_sync_v2', request_id=i,
                    observation_id=int(index), policy_epoch=0, session_id=session_id,
                    observation_monotonic_ns=time.monotonic_ns(),
                    state16=arrays['states'][index].tolist(), last_submitted16=arrays['submitted'][index, 0].tolist(),
                    prediction_horizon=50, execute_steps=20, chunk_steps=20, frequency_hz=30,
                    model_id=health['model_id'], model_version=health['model_version'],
                    shadow_only=True, hold_source='recorded_submitted', frames=frames)
                header = json.dumps(meta, separators=(',', ':')).encode()
                started = time.perf_counter()
                await ws.send_bytes(struct.pack('!I', len(header)) + header + b''.join(chunks))
                result = json.loads((await ws.receive(timeout=15)).data)
                for key in ('request_id', 'observation_id', 'policy_epoch', 'session_id', 'model_id', 'model_version'):
                    assert result.get(key) == meta[key], key
                if result.get('success'):
                    actions = np.asarray(result['actions'])
                    assert actions.shape == (20, 16) and np.isfinite(actions).all()
                    np.testing.assert_allclose(actions[:, :7], np.broadcast_to(arrays['submitted'][index, 0, :7], (20, 7)))
                else:
                    assert result.get('reason') == 'ik_rejected', result
                    assert result.get('actions') is None
                records.append(dict(index=int(index), success=result['success'], reason=result.get('reason'),
                                    rtt_ms=(time.perf_counter()-started)*1000))
    report = dict(protocol='PASS', health=health, robot_commands=0, source='recorded_validation',
                  completed=len(records), accepted=sum(r['success'] for r in records), records=records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--endpoint', required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    asyncio.run(check(p.parse_args()))

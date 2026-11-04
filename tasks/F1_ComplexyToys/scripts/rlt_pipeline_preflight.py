#!/usr/bin/env python3
"""Synthetic live-prefix -> real WS/cache/RLinf channels; no robot publishers."""
import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

TASK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TASK/'src'), str(TASK/'tests')]


async def run(args):
    from aiohttp import web
    from PIL import Image
    from test_rlt_local_replay import fixture
    from f1_toys.server import Server
    from f1_toys.rlt_online import atomic_json, sha
    args.output.mkdir(parents=True, exist_ok=False)
    cfg = json.loads(args.config.read_text())
    streams, result = fixture(assisted=True)
    raw = args.output/'raw/e'
    (raw/'journal.partial').mkdir(parents=True)
    (raw/'payload.partial').mkdir()
    (raw/'local_segments').mkdir()
    frames = []
    for camera in ('head', 'left_wrist', 'right_wrist'):
        width, height = (1280, 720) if camera == 'head' else (640, 480)
        image = raw/'payload.partial'/f'{camera}.jpg'
        Image.new('RGB', (width, height)).save(image)
        frames.append(dict(camera=camera, path=image.name, width=width, height=height,
            payload_offset=0, payload_bytes=image.stat().st_size, payload_sha256=sha(image), media_type='image/jpeg'))
    payload = []
    for obs in streams['observations']:
        obs.update(policy_epoch=1, frames=[dict(f, host_received_monotonic_ns=obs['available_monotonic_ns']) for f in frames])
        payload.append(dict(obs))
    (raw/'payload.partial/frames.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in payload))
    streams.update(events=[dict(result, event='rlt_local_closed')], policy_chunks=[])
    metadata = {}
    for name, rows in streams.items():
        for i, row in enumerate(rows):
            row.update(episode_id='e', stream_sequence=i)
        path = raw/'journal.partial'/f'{name}.jsonl'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        metadata[name] = dict(bytes=path.stat().st_size, sha256=sha(path))
    receipt = dict(schema='f1_local_journal_prefix_v1', drop_count=0, episode_id='e',
        binding=dict(execution_mode='powered', recording_boundary='f1_rlt_local_v2', synthetic=True),
        streams=metadata, local_result=streams['events'][0])
    atomic_json(raw/'local_segments/placement-1.json', receipt)
    identity = cfg['f1']['replay']['feature_identity']

    class SyntheticFeatures:
        initial_identity = identity
        ready = True
        def predict(self, *args, **kwargs):
            assert kwargs.get('features_only') is True
            return dict(z_rl=[0.]*2048, proprio=[0.]*8, ref_chunk=[[0.]*10]*50)
    feature = SyntheticFeatures(); feature.identity = identity
    server = Server(SimpleNamespace(bundle_id=identity['rlt']['base_model_version']), rlt_policy=feature)
    app = web.Application(); app.router.add_get('/rlt/features', server.websocket)
    app.on_cleanup.append(server.close)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    cfg['f1'].update(preflight_synthetic=True, seed_caches=[], publish_updates=False,
        raw_root=str(raw.parent), cache_root=str(args.output/'cache'), snapshot_root=str(args.output/'snapshots'),
        feature_endpoint=f'ws://127.0.0.1:{port}/rlt/features', min_replay_transitions=1)
    cfg['runner'].update(max_steps=2, max_epochs=2, weight_sync_interval=1, save_interval=1)
    cfg['runner']['logger'].update(log_path=str(args.output/'logs'), experiment_name='synthetic-prefix-pipeline')
    atomic_json(args.output/'config.json', cfg)
    try:
        with (args.output/'runner.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(sys.executable, str(TASK/'scripts/rlt_async_train.py'),
                '--config', str(args.output/'config.json'), '--run-id', args.run_id,
                env=dict(os.environ, CUDA_VISIBLE_DEVICES=''), stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(process.wait(), 120)
            except asyncio.TimeoutError:
                process.terminate(); await process.wait(); raise
        if code:
            raise RuntimeError(f'Runner exit {code}; see {args.output}/runner.log')
        manifests = list((args.output/'cache').glob('*/manifest.json'))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text())
        assert manifest['transition_count'] == 2 and manifest['synthetic']
        assert not (args.output/'snapshots/latest.json').exists()
        assert not (raw/'journal/manifest.json').exists()  # ingested before whole-episode Y
        atomic_json(args.output/'acceptance.json', dict(synthetic=True, live_prefix_before_y=True,
            feature_ws=True, official_runner=True, transitions=2, training_steps=2,
            snapshot_advertised=False, robot_output=False, wandb_run_id=args.run_id))
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    asyncio.run(run(parser.parse_args()))

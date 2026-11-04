"""Real sockets and a spawned network worker; fake policy never touches hardware."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import time

from aiohttp import web
import numpy as np
from PIL import Image
import pytest

PROJECTS = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PROJECTS / "DAgger/F1/src"), str(PROJECTS / "DAgger/shared/src")]
from dagger_core.recording_observation import TimestampedFrame, DeviceTimestamp
from f1dagger.observation import Observation
from f1dagger.websocket_policy import WebSocketPolicy, encode_request
from f1_toys.server import Server, unpack


class FakePolicy:
    bundle_id = 'fake-nonrobot'

    def predict_eef(self, images, state, held, **kwargs):
        assert [im.size for im in images.values()] == [(1280,720),(640,480),(640,480)]
        return dict(success=True,actions=[list(held)]*20,timing_ms={}),np.zeros((50,32)),None


def observation():
    now=time.monotonic_ns()
    frames=[]
    for camera in ('head','left_wrist','right_wrist'):
        size=(1280,720) if camera=='head' else (640,480)
        stream=BytesIO();Image.new('RGB',size).save(stream,format='JPEG')
        frames.append(TimestampedFrame(camera,1,DeviceTimestamp(now,'ns','ros'),now+1,stream.getvalue(),'image/jpeg',*size))
    return Observation(1,now,now+2,tuple(frames),(0.,)*16,(now,)*16,(0.,)*22)


def test_persistent_child_transport_and_server_rejects_invalid_requests():
    async def run():
        server=Server(FakePolicy())
        app=web.Application();app.router.add_get('/health',server.health);app.router.add_get('/ws',server.websocket)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        cfg=dict(endpoint=f'http://127.0.0.1:{port}',timeout_s=5.,chunk_steps=20,frequency_hz=30.,max_age_s=10.)
        policy=WebSocketPolicy(cfg,'test',lambda a:None,held_getter=lambda:np.arange(16,dtype=float))
        try:
            await asyncio.to_thread(policy.health)
            a=await asyncio.to_thread(policy.infer,observation(),1,2)
            pid=policy.process.pid
            b=await asyncio.to_thread(policy.infer,observation(),2,2)
            assert policy.process.pid==pid
            np.testing.assert_array_equal(a,b)
            assert a.shape==(20,16) and policy.last_timing['network_rtt_ms']>0
            server.busy=True
            with pytest.raises(ValueError,match='busy'):
                await asyncio.to_thread(policy.infer,observation(),3,2)
            assert policy.process is None
        finally:
            policy.close();await runner.cleanup()
    asyncio.run(run())


def test_reject_short_and_malformed_binary():
    for raw in (b'',b'bad',b'\x00\x00\x01\x00{}'):
        with pytest.raises(ValueError):unpack(raw)


def test_checked_ik_rejection_same_socket_then_success_and_exact_failure_log(tmp_path):
    from f1dagger.policy_errors import IKRejected
    class RejectOnce(FakePolicy):
        count = 0
        def predict_eef(self, images, state, held, **kwargs):
            self.count += 1
            if self.count == 1:
                return dict(success=False, actions=None, reason='ik_rejected', timing_ms={},
                            diagnostics=[dict(success=False, joints=None)]),np.arange(1600).reshape(50,32),None
            return super().predict_eef(images,state,held,**kwargs)
    async def run():
        log = tmp_path/'requests.jsonl'
        server = Server(RejectOnce(), log=log)
        original_write=server.write_record
        def slow_log(record):
            time.sleep(.02)
            original_write(record)
        server.write_record=slow_log
        app = web.Application(); app.router.add_get('/health',server.health); app.router.add_get('/ws',server.websocket)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        policy = WebSocketPolicy(dict(endpoint=f'http://127.0.0.1:{port}',timeout_s=5.,chunk_steps=20,
            frequency_hz=30.,max_age_s=10.),'test',lambda a:None,held_getter=lambda:np.zeros(16))
        try:
            await asyncio.to_thread(policy.health)
            with pytest.raises(IKRejected): await asyncio.to_thread(policy.infer,observation(),1,0)
            assert policy.last_timing['outcome'] == 'ik_rejected'
            assert policy.last_timing['network_rtt_ms'] > 0
            assert policy.last_timing['response_verified'] is True
            assert policy.last_timing['server']['log_and_replay'] >= 20
            pid = policy.process.pid
            assert (await asyncio.to_thread(policy.infer,observation(),2,1)).shape == (20,16)
            assert policy.process.pid == pid
            records = [json.loads(l) for l in log.read_text().splitlines()]
            assert records[0]['failure_replay']['normalized_actions32'] == np.arange(1600).reshape(50,32).tolist()
            assert records[0]['failure_replay']['state16'] == [0]*16
            assert 'failure_replay' not in records[1]
            assert np.asarray(records[1]['prediction_replay']['normalized_actions32']).shape == (50,32)
            assert records[0]['payload_sha256'] != records[1]['payload_sha256']
        finally:
            policy.close(); await runner.cleanup()
    asyncio.run(run())

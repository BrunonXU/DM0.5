import asyncio
import numpy as np
import pytest
from aiohttp import web
from test_ws_transport import observation, FakePolicy
from f1_toys.server import Server
from f1dagger.rtc_policy import RTCPolicy
from f1dagger.websocket_policy import encode_request
from f1_toys.server import unpack


def test_real_network_child_rtc_context_prefix_and_shadow_identity():
    class Model(FakePolicy):
        corrupt=False
        def predict_rtc(self,images,state,held,rtc,**kwargs):
            actions=np.repeat(np.asarray(held)[None],rtc['coverage_steps'],axis=0)
            if not rtc['bootstrap']:
                actions[:rtc['delay_steps']]=rtc['committed_actions16']
                if self.corrupt:actions[0,7]+=1
            return dict(success=True,actions=actions.tolist(),absolute_eef10=np.zeros((50,10)).tolist(),timing_ms={}),np.zeros((50,32)),None
    async def run():
        model=Model();server=Server(model,rtc=True)
        app=web.Application();app.router.add_get('/health',server.health);app.router.add_get('/ws',server.websocket)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        policy=RTCPolicy(dict(endpoint=f'http://127.0.0.1:{port}',timeout_s=3,chunk_steps=20,
            frequency_hz=30.,max_age_s=10.,shadow_only=True),'test',lambda a:None,held_getter=lambda:np.zeros(16))
        context=dict(bootstrap=True,delay_steps=15,shift_steps=20,coverage_steps=37,plan_origin_ns=0,
            base_plan_id=0,committed_actions16=[],previous_absolute_eef10=[])
        try:
            await asyncio.to_thread(policy.health)
            actions,_=await asyncio.to_thread(policy.infer_rtc,observation(),1,0,context)
            assert actions.shape==(37,16)
            stamps=policy.last_timing['transport_stages']
            order=('exchange_started_ns','parent_send_started_ns','child_received_ns',
                'ws_send_started_ns','ws_send_completed_ns','ws_received_ns',
                'child_decoded_ns','child_reply_started_ns','parent_received_ns','parent_decoded_ns')
            assert all(stamps[b]>=stamps[a] for a,b in zip(order,order[1:]))
            assert policy.last_timing['validation_completed_ns']>=stamps['parent_decoded_ns']
            assert policy.last_timing['server']['worker_total']>=policy.last_timing['server']['handler']
            pid=policy.process.pid
            context.update(bootstrap=False,base_plan_id=1,committed_actions16=[[.2]*16]*15,
                           previous_absolute_eef10=[[0.]*10]*30,plan_origin_ns=1234)
            actions,_=await asyncio.to_thread(policy.infer_rtc,observation(),2,0,context)
            assert pid==policy.process.pid
            np.testing.assert_array_equal(actions[:15],context['committed_actions16'])
            model.corrupt=True
            with pytest.raises(ValueError,match='committed'):
                await asyncio.to_thread(policy.infer_rtc,observation(),3,0,context)
        finally:policy.close();await runner.cleanup()
    asyncio.run(run())


def test_prepared_pixels_cannot_bypass_bound_preprocessing():
    obs=observation()
    context=dict(bootstrap=True,delay_steps=20,shift_steps=20,coverage_steps=42,
        base_plan_id=0,plan_origin_ns=0,committed_actions16=[],previous_absolute_eef10=[])
    metadata=dict(protocol='f1_eef_rtc_v2',contract='f1_upper_16d_v1',request_id=1,
        observation_id=1,policy_epoch=0,observation_monotonic_ns=0,session_id='test',
        state16=[0.]*16,last_submitted16=[0.]*16,frequency_hz=30.,chunk_steps=20,
        execute_steps=20,prediction_horizon=50,rtc=context,prepared_pixels={'contract_sha256':'wrong'})
    with pytest.raises(ValueError,match='pixel identity'):
        unpack(encode_request(metadata,obs.frames),rtc_enabled=True,preprocessing={'contract_sha256':'bound'})

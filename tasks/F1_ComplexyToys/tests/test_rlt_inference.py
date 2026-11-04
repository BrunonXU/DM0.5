"""C16 semantics, immutable identities, and shared B/RLT service exclusion."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest
torch = pytest.importorskip("torch")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from f1_toys import rlt_inference as module
from f1_toys.rlt_contract import file_sha, load_rlt_identity, RLT_PROTOCOL, RLT_CONTROL_MODE
from f1_toys.server import Server, unpack


def test_rlt_rtc_requires_explicit_protocol_and_valid_committed_prefix():
    from f1_toys.rlt_contract import RLT_RTC_PROTOCOL
    context=dict(bootstrap=False,delay_steps=6,shift_steps=8,coverage_steps=16,
                 plan_origin_ns=100,base_plan_id=1,committed_actions16=[[0.]*16]*6,
                 previous_absolute_eef10=[[0.]*10]*8,rlt=True)
    wire=payload(protocol=RLT_RTC_PROTOCOL,request_id=2,rtc=context)
    unpack(wire,rlt_enabled=True,rlt_rtc_enabled=True)
    with pytest.raises(ValueError):unpack(wire,rlt_enabled=True)
    context['committed_actions16']=context['committed_actions16'][:-1]
    with pytest.raises(ValueError,match='committed'):
        unpack(payload(protocol=RLT_RTC_PROTOCOL,request_id=2,rtc=context),rlt_enabled=True,rlt_rtc_enabled=True)


def test_rtc_identity_is_distinct_without_modifying_actor(artifacts):
    head,encoder=artifacts
    sync=load_rlt_identity(head,encoder,'base')
    rtc=load_rlt_identity(head,encoder,'base',rtc=True)
    assert rtc['model_version'] != sync['model_version']
    assert rtc['rlt']['actor_version']==sync['rlt']['actor_version']
    assert rtc['runtime_manifest']['rtc_supported']


def test_reference_compute_choice_is_explicit_and_fixed_after_warmup():
    sidecar=object.__new__(module.RLTInference)
    sidecar.ready=False;sidecar.policy=SimpleNamespace()
    sidecar.configure_reference_compute('training_eager_prefix_and_suffix')
    with pytest.raises(ValueError,match='prepared shared prefix'):
        sidecar.configure_reference_compute('serving_graph_prefix_and_suffix')
    sidecar.policy.rtc_prefix_graph=object()
    sidecar.configure_reference_compute('serving_graph_prefix_and_suffix')
    sidecar.ready=True
    with pytest.raises(ValueError,match='ready service'):
        sidecar.configure_reference_compute('training_eager_prefix_and_suffix')


@pytest.fixture
def artifacts(tmp_path):
    head, encoder = tmp_path/"head", tmp_path/"encoder"
    head.mkdir(); encoder.mkdir()
    (head/"head.safetensors").write_bytes(b"test head")
    (encoder/"encoder.safetensors").write_bytes(b"test encoder")
    enc = dict(schema="f1_rlt_encoder_v1", parent_bundle_sha256="base",
               encoder_sha256=file_sha(encoder/"encoder.safetensors"))
    (encoder/"manifest.json").write_text(json.dumps(enc))
    manifest = dict(schema="f1_rlt_stage2_head_v1", phase="bc", q_guidance_updates=0,
        parent_bundle_sha256="base", encoder_manifest_sha256=file_sha(encoder/"manifest.json"),
        head_sha256=file_sha(head/"head.safetensors"),checkpoint_step=400,
        model_config=dict(z_dim=2048,proprio_dim=8,action_dim=10,chunk_len=16,ref_chunk_len=50),
        output_parameterization="linear_normalized_xyz_tanh_rotation6d_gripper",
        deployment_ready=False,reference_mode="offline_only")
    (head/"manifest.json").write_text(json.dumps(manifest))
    return head, encoder


def test_runtime_identity_preserves_historical_export_and_checks_weights(artifacts):
    head, encoder = artifacts
    original = (head/"manifest.json").read_bytes()
    identity = load_rlt_identity(head, encoder, "base")
    assert identity["rlt"]["actor_version"] == file_sha(head/"head.safetensors")
    assert identity["runtime_manifest"]["source_export_deployment_ready"] is False
    assert identity["runtime_manifest"]["rtc_supported"] is False
    assert (head/"manifest.json").read_bytes() == original
    with pytest.raises(ValueError, match="different base"):
        load_rlt_identity(head, encoder, "wrong")
    (head/"head.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="weights changed"):
        load_rlt_identity(head, encoder, "base")


def payload(**changes):
    meta = dict(contract="f1_upper_16d_v1",protocol=RLT_PROTOCOL,control_mode=RLT_CONTROL_MODE,
        prediction_horizon=16,reference_horizon=50,execute_steps=16,chunk_steps=16,frequency_hz=30,
        model_id="rlt",model_version="rlt-version",session_id="test",request_id=1,observation_id=2,
        policy_epoch=3,observation_monotonic_ns=100,state16=[0.]*16,last_submitted16=[0.]*16)
    blobs, frames = [], []
    for camera in ("head","left_wrist","right_wrist"):
        size=(1280,720) if camera=="head" else (640,480)
        stream=BytesIO(); Image.new("RGB",size).save(stream,format="PNG")
        blobs.append(stream.getvalue())
        frames.append(dict(camera=camera,width=size[0],height=size[1],encoding="image/png",length=len(blobs[-1])))
    meta.update(frames=frames,**changes)
    header=json.dumps(meta).encode()
    return struct.pack("!I",len(header))+header+b"".join(blobs)


@pytest.mark.parametrize("change",[{"rtc":{}},{"delay_steps":14},{"prediction_horizon":50},
    {"execute_steps":20},{"reference_horizon":16},{"control_mode":"rollout"},
    {"protocol":"f1_eef_rtc_v2"}])
def test_rlt_rejects_rtc_or_reinterpreted_chunk_contract(change):
    with pytest.raises(ValueError):
        unpack(payload(**change), rlt_enabled=True)


def test_rlt_wire_and_proposal_telemetry_keep_actor16_reference50(tmp_path):
    class Candidate:
        ready=True
        identity=dict(model_id="rlt",model_version="rlt-version",rlt={"actor_version":"head-sha"})
        def predict(self,images,state,held,**kwargs):
            return (dict(success=True,actions=[held]*16,timing_ms={}), np.ones((16,32)),
                    dict(reference_normalized_actions32=np.zeros((50,32)).tolist(),executed_transition=False))
    base=SimpleNamespace(bundle_id="base")
    server=Server(base,rtc=True,rlt_policy=Candidate(),log=tmp_path/"requests.jsonl")
    result=server.compute_rlt(payload())
    assert result["model_id"]=="rlt" and result["model_version"]=="rlt-version"
    assert len(result["actions"])==16 and result["proposal_only"] and not result["rtc"]
    record=json.loads((tmp_path/"requests.jsonl").read_text())
    assert not record["executed_transition"]
    assert np.shape(record["prediction_replay"]["normalized_actions32"])==(16,32)
    assert np.shape(record["prediction_replay"]["reference_normalized_actions32"])==(50,32)
    assert "z_rl" not in result and "prediction_replay" not in result
    with pytest.raises(ValueError,match="binding"):
        server.compute_rlt(payload(model_version="base"))
    health=json.loads(asyncio.run(server.rlt_health(None)).body)
    assert health["ready"] and health["capabilities"]["chunk_steps"]==16
    assert json.loads(asyncio.run(server.health(None)).body)["prediction_horizon"]==50


@pytest.mark.parametrize("first_path,second_path",[("/ws","/rlt/infer"),("/rlt/infer","/ws")])
def test_b_and_rlt_endpoints_share_one_busy_guard(first_path,second_path):
    from aiohttp import ClientSession, web
    async def run():
        entered, release = threading.Event(), threading.Event()
        def compute(*args):
            entered.set()
            assert release.wait(5)
            return dict(success=True)
        sidecar=SimpleNamespace(identity={"model_id":"rlt","model_version":"rv","rlt":{}},ready=True)
        server=Server(SimpleNamespace(bundle_id="base"),rlt_policy=sidecar)
        server.compute=server.compute_rlt=compute
        app=web.Application();app.router.add_get("/ws",server.websocket);app.router.add_get("/rlt/infer",server.websocket)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,"127.0.0.1",0);await site.start()
        endpoint="http://127.0.0.1:"+str(site._server.sockets[0].getsockname()[1])
        try:
            async with ClientSession() as client:
                async with client.ws_connect(endpoint+first_path) as first, client.ws_connect(endpoint+second_path) as second:
                    await first.send_bytes(b"test")
                    assert await asyncio.to_thread(entered.wait,3)
                    await second.send_bytes(b"test")
                    assert json.loads((await second.receive()).data)["error"]=="busy"
                    release.set()
                    assert json.loads((await first.receive()).data)["success"]
        finally:
            release.set();await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("fail",[False,True])
@pytest.mark.parametrize("optimized",[False,True])
@pytest.mark.parametrize('compute_path',['training_eager_prefix_and_suffix',
    'serving_graph_prefix_eager_suffix','serving_graph_prefix_and_suffix'])
def test_actor_uses_same_prefix_and_original_unshifted_c16_and_restores_hook(monkeypatch,fail,optimized,compute_path):
    calls=[]
    class Model:
        def run_suffix(self,*args):
            return None
        def build_prefix_cache(self,obs):
            return torch.ones(1,3,4),torch.ones(1,3,dtype=torch.bool),None
        def sample_actions(self,obs,**kwargs):
            assert kwargs["num_steps"]==5 and kwargs["noise"] is None
            calls.append("reference")
            self.build_prefix_cache(obs)
            self.run_suffix()
            if fail:raise RuntimeError("reference failed")
            ref=torch.zeros(1,50,32);ref[:,:,:10]=4
            return ref
    class Actor:
        def sac_forward(self,features,deterministic):
            assert deterministic and features["ref_chunk"].shape==(1,50,10)
            calls.append("actor")
            return torch.arange(160,dtype=torch.float32)[None],None,None
    policy=SimpleNamespace(model=Model(),device="cpu",contract={"active_state_indices":[7,8,9,10,11,12,13,15]},
        processor=SimpleNamespace(observation=lambda *args:{},last_state_diagnostics={}),
        observation_type=None,ik=object(),stats={"action":{"mean":[0,0,0],"std":[1,1,1]}})
    def optimized_path(*args):
        raise AssertionError("RLT must use its training eager path")
    if optimized:
        policy.rtc_optimization="compiled_suffix"
        policy.rlt_reference_eager_prefix=policy.model.build_prefix_cache
        policy.rlt_reference_eager_suffix=policy.model.run_suffix
        policy.model.build_prefix_cache=optimized_path
        policy.model.run_suffix=optimized_path
    if compute_path != 'training_eager_prefix_and_suffix':
        policy.rtc_prefix_graph=object()
        # Fake optimized functions reproduce the math; the test checks routing
        # and restoration on failures, not GPU numerical equivalence.
        policy.model.build_prefix_cache=Model.build_prefix_cache.__get__(policy.model)
        policy.model.run_suffix=Model.run_suffix.__get__(policy.model)
        monkeypatch.setattr(torch.compiler,'cudagraph_mark_step_begin',lambda:None)
    before_prefix=policy.model.build_prefix_cache
    before_suffix=policy.model.run_suffix
    owned_prefix='build_prefix_cache' in vars(policy.model)
    owned_suffix='run_suffix' in vars(policy.model)
    monkeypatch.setattr(module,"batched_observation",lambda *args:SimpleNamespace(state=torch.zeros(1,32)))
    def decode(ik,physical,state,held,execute_steps):
        calls.append("actor_ik")
        assert execute_steps==16 and np.array_equal(physical,np.arange(160).reshape(16,10))
        return dict(success=True,actions=[held]*16)
    monkeypatch.setattr(module,"decode_prefix",decode)
    sidecar=module.RLTInference.__new__(module.RLTInference)
    sidecar.policy=policy;sidecar.actor=Actor()
    sidecar.reference_compute_path=compute_path
    sidecar.reader=SimpleNamespace(encoder=lambda prefix,mask:torch.ones(1,1,2048))
    if fail:
        with pytest.raises(RuntimeError,match="reference failed"):
            sidecar.predict({},np.zeros(16),np.zeros(16),seed=123)
        assert calls==["reference"]
    else:
        decoded,raw,replay=sidecar.predict({},np.zeros(16),np.zeros(16),seed=123)
        assert calls==["reference","actor","actor_ik"]
        assert np.array_equal(raw[:,:10],np.arange(160).reshape(16,10))
        assert np.all(raw[:,10:]==0) and replay["reference_seed"]==123
        assert replay['reference_compute_path']==compute_path
    assert policy.model.build_prefix_cache==before_prefix
    assert policy.model.run_suffix==before_suffix
    assert ('build_prefix_cache' in vars(policy.model))==owned_prefix
    assert ('run_suffix' in vars(policy.model))==owned_suffix

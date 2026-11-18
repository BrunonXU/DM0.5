"""CAN evidence survives IPC, HTTP and experience without entering model text."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from f1_harness.contracts import HarnessError, MeasuredGripperFeedback, Observation, digest
from f1_harness.evidence import EvidenceStore
from f1_harness.gateway import Gateway, GatewayServer
from f1_harness.ipc import MAX_MESSAGE, UnixClient, UnixServer
from f1_harness.remote import OwnerProxy, RemoteTaskBackend
from f1_harness.scene_evidence import archive_observation, bind_store, observation_wire, pack
from test_measured_gripper_contract import feedback


def full_window():
    value=feedback()
    value['samples']=[dict(value['latest'],sequence=i,received_ns=1000+i) for i in range(1,65)]
    value.update(latest=value['samples'][-1],received_count=64)
    return MeasuredGripperFeedback.model_validate(value)


def prepared(tmp_path):
    from test_program_trial_owner import owner
    engine,_=owner(tmp_path/'owner')
    raw=engine.observation.model_copy(update={'measured_gripper':full_window(),
        'received_ns':2000,'state_received_ns':2000,
        'frames':tuple(f.model_copy(update={'received_ns':2000}) for f in engine.observation.frames)})
    store=EvidenceStore(tmp_path/'gateway');identity=bind_store(tmp_path,store)
    observation,_=archive_observation(raw,store,identity['store_id'])
    return engine,observation,store


def test_real_http_to_remote_keeps_full_measurement_and_owner_receives_exact_window(tmp_path):
    from rpent.utils.rpc.http_rpc import HttpRpcClient
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.program_trials import validate_program_trial
    engine,obs,store=prepared(tmp_path)
    bridge=OwnerBridge(engine,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=500_000_000,reference_tolerance_deg=.1,clock=lambda:10)
    bridge.serve(tmp_path/'owner.sock')
    proxy=OwnerProxy(tmp_path/'owner.sock')
    gateway=GatewayServer(Gateway(proxy,SimpleNamespace(observe=lambda:obs),store)).start()
    remote=RemoteTaskBackend.__new__(RemoteTaskBackend)
    remote.rpc=HttpRpcClient(gateway.url,enable_sessions=False)
    try:
        for method in ('observe','observe_with_status'):
            reply=remote.call(method)
            payload=reply['observation'] if method=='observe_with_status' else reply
            # This host must not resolve any robot-local filesystem reference.
            decoded=Observation.model_validate(payload)
            assert decoded.measured_gripper==obs.measured_gripper
            assert decoded.measured_gripper_ref==obs.measured_gripper_ref
            assert digest(decoded)==digest(obs)
            assert engine.observation.measured_gripper==obs.measured_gripper
        receipt=remote.call('register_program_trial',session_id=engine.session,
            observation_id=obs.observation_id)
        trial=receipt['trial'];validate_program_trial(trial)
        assert trial==engine.program_trial
        assert trial['observation']['measured_gripper']==obs.measured_gripper.model_dump(mode='json')
        assert len(trial['observation']['measured_gripper']['samples'])==64
        assert not engine.request_publications and not engine.task_requests
    finally:
        gateway.close();bridge.close()


@pytest.mark.parametrize('solved',[False,True])
def test_remote_model_observation_excludes_full_window_and_reference_in_both_paths(tmp_path,solved):
    _,obs,_=prepared(tmp_path)
    remote=RemoteTaskBackend.__new__(RemoteTaskBackend)
    remote.pending=None;remote._solved=solved
    remote.task=SimpleNamespace(operator_instruction='grasp')
    remote._await_observation=lambda **kwargs:(obs,{})
    remote.call=lambda method,**kwargs:observation_wire(obs,include_measured_gripper=True) if method=='observe' else {}
    result=remote.execute_tool('observe',{})
    assert result['gripper_feedback']['available']
    assert result['gripper_feedback']['sequence']==64
    assert 'measured_gripper' not in result['observation']
    assert 'measured_gripper_ref' not in result['observation']
    assert len(json.dumps(result))<10000


def test_reverse_proof_and_trial_rpc_preserve_full_window_and_hashes(tmp_path):
    from f1_harness.program_trials import build_program_trial,validate_program_trial
    engine,obs,_=prepared(tmp_path)
    scope=dict(owner_instance_id=engine.instance,session_id=engine.session,epoch=engine.epoch,
        task_id=engine.task.task_id,task_hash=digest(engine.task),calibration_id=engine.task.calibration_id)
    trial=build_program_trial(obs,engine.task,scope=scope,owner_run_id='transport')
    payload=dict(observation=obs.model_dump(mode='json'),observation_sha256=digest(obs),trial=trial)
    assert len(json.dumps(pack(dict(ok=True,result=payload))))<MAX_MESSAGE
    server=UnixServer(tmp_path/'proof.sock',lambda *args:payload,scene_evidence=True).start()
    try:
        result=UnixClient(server.path,scene_evidence=True).call('proof')
        assert result==payload
        assert digest(Observation.model_validate(result['observation']))==result['observation_sha256']
        validate_program_trial(result['trial'])
        changed=deepcopy(result['trial'])
        changed['observation']['measured_gripper']['samples'][0]['received_ns']-=1
        with pytest.raises(HarnessError,match='manifest_changed'):
            validate_program_trial(changed)
    finally:server.close()


@pytest.mark.parametrize('run',[
    'web-ef2c4aefe83f4aae968d64e5d6ef66e0',
    'web-ed3b53b979b243d4b6a9a169ad64924e',
    'web-9b88e86fda204c528e9bd22aa9afe75b'])
def test_actual_signed_trials_keep_pre_reference_identity(run):
    from f1_harness.program_trials import validate_program_trial
    path=Path(__file__).resolve().parents[1]/'runs/workspace'/run/'collected/owner/harness/trace.jsonl'
    if not path.exists():pytest.skip('Actual owner journals are local archived fixtures')
    original=path.read_bytes();count=0
    for line in original.splitlines():
        row=json.loads(line)
        if row.get('kind')!='program_trial_frozen':continue
        trial=row['trial'];validate_program_trial(trial);count+=1
        raw=trial['observation']
        assert 'measured_gripper_ref' not in raw
        assert Observation.model_validate(raw).model_dump(mode='json')==raw
        assert digest(raw)==trial['observation_sha256']
    assert count==1 and path.read_bytes()==original


def test_full_gripper_reference_blob_survives_existing_owner_collection(tmp_path):
    from test_scene_failure_export import setup,export
    from test_program_trials import observed
    request,reader,store=setup(tmp_path,max_bytes=8*1024**2)
    identity=bind_store(tmp_path,store)
    original=observed('capture').model_copy(update={'measured_gripper':full_window()})
    obs,_=archive_observation(original,store,identity['store_id'])
    trace=Path(request['owner_run_path'])/'harness/trace.jsonl'
    with trace.open('a') as stream:
        stream.write(json.dumps({'kind':'scene_read','observation':observation_wire(obs)})+'\n')
    metadata=export(tmp_path,request,reader)
    ref=obs.measured_gripper_ref
    assert ref.blob_sha256 in metadata['scene_manifest']['collected']
    data=(tmp_path/'exported/owner/scene/blobs'/ref.blob_sha256).read_bytes()
    assert hashlib.sha256(data).hexdigest()==ref.blob_sha256
    assert MeasuredGripperFeedback.model_validate_json(data)==original.measured_gripper

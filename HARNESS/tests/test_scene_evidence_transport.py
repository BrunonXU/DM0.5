"""Actual private RPC boundaries with content-addressed local depth, no actuators."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.contracts import HarnessError,Observation,digest
from f1_harness.evidence import EvidenceStore
from f1_harness.ipc import MAX_MESSAGE,UnixClient,UnixServer,send
from f1_harness.scene_evidence import (archive_observation,bind_store,observation_wire,
                                      resolve_observation)
from test_local_environment_motion import partial_setup


def prepared(tmp_path):
    service,obs,intent,clock=partial_setup()
    store=EvidenceStore(tmp_path/'gateway');identity=bind_store(tmp_path,store)
    from f1_harness.partial_scene import observed_free_space_view
    from f1_harness.contracts import Frame
    # An incompressible full-resolution depth fixture exceeds the old wire cap.
    depth=np.random.default_rng(5).uniform(2.,3.,(480,640))
    frames=tuple(Frame(camera='right',stream=stream,frame_id=stream,sequence=1,
        source_ns=clock[0],received_ns=clock[0],clock_domain='test',width=640,height=480,
        encoding='rgb8' if stream=='rgb' else '32FC1',blob_sha256=hashlib.sha256(stream.encode()).hexdigest())
        for stream in ('rgb','depth'))
    view=observed_free_space_view(depth,{'camera':'right','k_rgb':[[600,0,320],[0,600,240],[0,0,1]]},
        np.eye(4),frames)
    evidence=obs.local_collision_evidence.model_copy(update={'source_frames':frames,'free_space_views':(view,)})
    obs=obs.model_copy(update={'frames':frames,'local_collision_evidence':evidence})
    return service,obs,intent,clock,store,identity


def test_large_scene_crosses_verifier_owner_plan_and_reverse_proof_rpc(tmp_path):
    from f1_harness.skill_services import VerifyService
    from f1_harness.engine import TaskEngine
    from f1_harness.owner_bridge import OwnerBridge
    from f1_harness.contracts import Mode
    service,original,intent,clock,store,identity=prepared(tmp_path)
    assert len(original.model_dump_json())>1_000_000
    obs,added=archive_observation(original,store,identity['store_id'])
    wire=observation_wire(obs)
    assert len(json.dumps(wire))<MAX_MESSAGE
    assert 'fine_depth_zlib_base64' not in json.dumps(wire)
    verify=VerifyService(service.task,clock=lambda:clock[0])
    engine=TaskEngine(EvidenceStore(tmp_path/'owner'),mode=Mode.SHADOW,clock=lambda:clock[0])
    engine.open_session();engine.prepare_task(engine.session,service.task.model_dump())
    planner=UnixClient(tmp_path/'plan.sock',scene_evidence=True)
    bridge=OwnerBridge(engine,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
        feedback_age_ns=500_000_000,reference_tolerance_deg=.1,clock=lambda:clock[0])
    bridge.geometry_provider=lambda o,f,h:planner.call('geometry_context',observation=observation_wire(o),full22=f,held16=h)
    bridge.update_feedback(full22=[0.]*22,measured16=obs.state16,stamps16=[clock[0]]*16)
    servers=[UnixServer(tmp_path/'verify.sock',verify.dispatch,scene_evidence=True).start(),
        UnixServer(tmp_path/'plan.sock',service.dispatch,scene_evidence=True).start()]
    bridge.serve(tmp_path/'owner.sock');owner=UnixClient(tmp_path/'owner.sock',scene_evidence=True)
    try:
        assert UnixClient(tmp_path/'verify.sock',scene_evidence=True).call('update_scene',observation=wire)
        assert verify.latest.local_collision_evidence==original.local_collision_evidence
        owner.call('ingest_live_scene',observation=wire,context=engine.status()['perception_context'])
        owner.call('ingest_observation',observation=wire)
        assert engine.live_observation.local_collision_evidence==original.local_collision_evidence
        assert engine.observation.local_collision_evidence==original.local_collision_evidence
        context=owner.call('geometry_context',session_id=engine.session)
        assert context['geometry_scope']=='coarse_local_environment'
        assert len(json.dumps(context))<MAX_MESSAGE
        plan=planner.call('plan_skill',intent=intent.model_dump(),observation=wire,
            full22=[0.]*22,held16=list(obs.state16))
        clock[0]+=100_000_000
        frames=tuple(f.model_copy(update=dict(source_ns=clock[0],received_ns=clock[0],sequence=2)) for f in obs.frames)
        evidence=original.local_collision_evidence.model_copy(update=dict(observation_id='next',
            measured_at_ns=clock[0],state_received_ns=clock[0],source_frames=frames))
        later=original.model_copy(update=dict(observation_id='next',scene_version='next',received_ns=clock[0],
            state_received_ns=clock[0],frames=frames,local_collision_evidence=evidence))
        later,_=archive_observation(later,store,identity['store_id'])
        real_proof=planner.call('revalidate_plan',intent=intent.model_dump(),proposal=plan,
            original_observation=wire,observation=observation_wire(later),full22=[0.]*22,
            held16=list(obs.state16),after_ns=clock[0]-1)
        assert real_proof['observation_sha256']==digest(later)
        json.dumps(real_proof,allow_nan=False)  # Original asynchronous journal contract.
        # Revalidation returns an observation inside its proof. The same
        # production framing resolves both directions, including two inputs.
        def proof(method,kwargs):
            a=Observation.model_validate(kwargs['original_observation']);b=Observation.model_validate(kwargs['observation'])
            assert a.local_collision_evidence==b.local_collision_evidence
            return {'observation':b.model_dump(),'observation_sha256':digest(b)}
        server=UnixServer(tmp_path/'proof.sock',proof,scene_evidence=True).start();servers.append(server)
        result=UnixClient(server.path,scene_evidence=True).call('revalidate_plan',original_observation=wire,observation=wire)
        decoded=Observation.model_validate(result['observation'])
        assert decoded.local_collision_evidence==original.local_collision_evidence
        assert digest(decoded)==result['observation_sha256']==digest(obs)
        assert not engine.request_publications
    finally:
        bridge.close()
        for server in servers:server.close()
    assert archive_observation(original,store,identity['store_id'])[1]==0
    assert added>1_000_000


@pytest.mark.parametrize('corruption',['missing','bytes','symlink','size','store','observation','frame','camera'])
def test_invalid_reference_never_reaches_service(tmp_path,corruption):
    _,original,_,_,store,identity=prepared(tmp_path)
    obs,_=archive_observation(original,store,identity['store_id']);wire=observation_wire(obs)
    ref=wire['local_collision_evidence_ref'];path=store.root/'blobs'/ref['blob_sha256']
    if corruption=='missing':path.unlink()
    elif corruption=='bytes':path.write_bytes(b'X'*ref['byte_count'])
    elif corruption=='symlink':
        real=path.with_name('different');path.rename(real);path.symlink_to(real)
    elif corruption=='size':ref['byte_count']+=1
    elif corruption=='store':ref['store_id']='0'*64
    elif corruption=='observation':wire['observation_id']='different'
    elif corruption=='frame':wire['frames'][0]['blob_sha256']='0'*64
    elif corruption=='camera':ref['cameras']=['head']
    reached=[];server=UnixServer(tmp_path/'test.sock',lambda *args:reached.append(args),scene_evidence=True).start()
    try:
        with pytest.raises(RuntimeError):UnixClient(server.path,scene_evidence=True).call('update_scene',observation=wire)
        assert not reached
    finally:server.close()


def test_reference_is_readable_but_never_a_motion_evidence_substitute(tmp_path):
    from f1_harness.observation import require_geometry_identity
    from f1_harness.local_environment import require_local_evidence
    service,original,_,_,store,identity=prepared(tmp_path)
    obs,_=archive_observation(original,store,identity['store_id'])
    compact=Observation.model_validate(observation_wire(obs))
    assert require_geometry_identity(compact,admission_profile='development_coarse',skill='reobserve',read_only_reference=True)==('right',)
    with pytest.raises(ValueError):require_geometry_identity(compact,admission_profile='development_coarse',skill='move_tcp')
    with pytest.raises(HarnessError):require_local_evidence(service.task,compact,service.fixture)


def test_default_rpc_size_and_existing_archive_budget_remain_bounded(tmp_path):
    _,obs,_,_,store,identity=prepared(tmp_path)
    with pytest.raises(HarnessError,match='quota'):archive_observation(obs,store,identity['store_id'],remaining_bytes=1)
    assert not list((store.root/'blobs').iterdir())
    with socket.socketpair()[0] as peer:
        with pytest.raises(ValueError,match='too large'):send(peer,obs.model_dump())


def test_reference_blob_survives_existing_owner_collection(tmp_path):
    from test_scene_failure_export import setup,export
    request,reader,store=setup(tmp_path,max_bytes=8*1024**2)
    _,obs,_,_=partial_setup();identity=bind_store(tmp_path,store)
    obs,_=archive_observation(obs,store,identity['store_id'])
    path=Path(request['owner_run_path'])/'harness/trace.jsonl'
    with path.open('a') as stream:stream.write(json.dumps({'kind':'scene_read','observation':observation_wire(obs)})+'\n')
    metadata=export(tmp_path,request,reader)
    ref=obs.local_collision_evidence_ref
    assert ref.blob_sha256 in metadata['scene_manifest']['collected']
    data=(tmp_path/'exported/owner/scene/blobs'/ref.blob_sha256).read_bytes()
    assert hashlib.sha256(data).hexdigest()==ref.blob_sha256


def test_old_observation_serialization_keeps_other_nulls_and_nested_identity():
    from pydantic import BaseModel
    _,obs,_,_=partial_setup()
    class Envelope(BaseModel):
        observation: Observation
    raw=obs.model_dump(mode='json')
    assert 'local_collision_evidence_ref' not in raw
    assert 'owner_observation_id' in raw and raw['owner_observation_id'] is None
    assert json.loads(obs.model_dump_json())==raw
    assert Envelope(observation=obs).model_dump(mode='json')['observation']==raw
    assert obs.model_dump(include={'observation_id','owner_observation_id'})=={
        'observation_id':obs.observation_id,'owner_observation_id':None}
    assert obs.model_dump(mode='json',exclude={'local_collision_evidence'})['objects']==raw['objects']
    assert digest(obs)==digest(raw)


def test_actual_v29_program_trial_identity_survives_optional_reference_field():
    from f1_harness.program_trials import validate_program_trial
    path=Path(__file__).resolve().parents[1]/'artifacts/repair-v30-20261117/old-v29-program-trial.json'
    if not path.exists():pytest.skip('Actual V29 signed trial is an archived local fixture')
    payload=path.read_bytes();trial=json.loads(payload)
    assert 'local_collision_evidence_ref' not in trial['observation']
    validate_program_trial(trial)
    obs=Observation.model_validate(trial['observation'])
    assert obs.model_dump(mode='json')==trial['observation']
    assert digest(obs)==trial['observation_sha256']
    assert path.read_bytes()==payload


def test_new_reference_trial_roundtrip_keeps_content_identity(tmp_path):
    from f1_harness.program_trials import build_program_trial,validate_program_trial
    service,obs,_,_,store,identity=prepared(tmp_path)
    obs,_=archive_observation(obs,store,identity['store_id'])
    scope=dict(owner_instance_id='owner',session_id='session',epoch=1,
        task_id=service.task.task_id,task_hash=digest(service.task),calibration_id=service.task.calibration_id)
    trial=build_program_trial(obs,service.task,scope=scope,owner_run_id='transport-test')
    decoded=json.loads(json.dumps(trial,allow_nan=False))
    validate_program_trial(decoded)
    assert decoded==trial
    assert decoded['observation']['local_collision_evidence_ref']['blob_sha256']==obs.local_collision_evidence_ref.blob_sha256

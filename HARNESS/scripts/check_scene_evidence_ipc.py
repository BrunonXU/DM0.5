"""Archived scene through production local IPC/HTTP/worker; no device imports/actions.

Owner runs in SHADOW. Geometry uses the saved native FK/feedback for this exact
frame (a read-only numerical substitute, no solve path). Can also run on ARM.
"""
import argparse
from copy import deepcopy
import hashlib,json,tempfile,time
from pathlib import Path
from types import SimpleNamespace

from f1_harness.contracts import Observation,Task,Mode,digest
from f1_harness.evidence import EvidenceStore
from f1_harness.engine import TaskEngine
from f1_harness.gateway import Gateway,GatewayServer
from f1_harness.ipc import UnixClient,UnixServer,MAX_MESSAGE
from f1_harness.owner_bridge import OwnerBridge
from f1_harness.remote import OwnerProxy,RemoteTaskBackend
from f1_harness.scene_evidence import archive_observation,bind_store,observation_wire
from f1_harness.skill_services import PlanService,VerifyService
from rpent.utils.rpc.http_rpc import HttpRpcClient


def run(scene_path,snapshot_path,site,output):
    scene_path,snapshot_path,site,output=map(Path,(scene_path,snapshot_path,site,output))
    original=Observation.model_validate_json(scene_path.read_bytes());snapshot=json.loads(snapshot_path.read_text())
    cfg=json.loads((site/'plan.local.json').read_text());task=Task.model_validate(cfg['task'])
    output.mkdir(parents=True,exist_ok=False)
    clock=lambda:original.received_ns+1
    report=dict(schema='f1-scene-evidence-ipc-check-v1',physical_motion=False,robot_connected=False,
        scene_sha256=hashlib.sha256(scene_path.read_bytes()).hexdigest(),
        snapshot_sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        original_scene_bytes=len(scene_path.read_bytes()),default_ipc_maximum_bytes=MAX_MESSAGE,
        real_input_times_unchanged=True,numerical_source='archived native FK; no IK or motion',
        owner_mode='shadow',authorization='in-memory test only',timings_ms={},checks={})
    report['view_sizes']=[dict(camera=v.camera,json_bytes=len(json.dumps(v.model_dump()).encode()))
        for v in original.local_collision_evidence.free_space_views]
    with tempfile.TemporaryDirectory(prefix='f1-scene-ipc-') as directory:
        runtime=Path(directory);store=EvidenceStore(output/'gateway');binding=bind_store(runtime,store)
        started=time.perf_counter();scene,added=archive_observation(original,store,binding['store_id'])
        report['timings_ms']['archive_once']=(time.perf_counter()-started)*1000
        report['archived_bytes']=added;wire=observation_wire(scene)
        report['wire_scene_bytes']=len(json.dumps(wire).encode());assert report['wire_scene_bytes']<MAX_MESSAGE
        engine=TaskEngine(EvidenceStore(output/'owner'),mode=Mode.SHADOW,clock=clock)
        engine.open_session();engine.prepare_task(engine.session,task.model_dump())
        engine.host_boot_id=scene.host_boot_id;engine.authorized=True
        engine.recording_identity={'host_boot_id':scene.host_boot_id}
        # This read-only method-level check is portable to ARM without editing
        # old absolute release paths or pretending old admission certifies new
        # source. Release/ARM admission is validated independently by rollout.
        from f1_harness.frames import FrameChain
        import numpy as np
        plan=PlanService.__new__(PlanService);plan.task=task;plan.fixture=cfg['fixture'];plan.clock=clock
        frame=cfg['fixture']['frame_chain']
        plan.frames=FrameChain(np.asarray(frame['T_ik_world_native']),np.asarray(frame['T_ik_eef_native_eef']),
            frame['native_frame'],frame['ik_world_frame'])
        report['release_admission']='not exercised; archived frame chain algebra only'
        def recorded_fk(method,**kwargs):
            if method!='forward_pose':raise AssertionError('motion solver forbidden in this read-only check')
            return snapshot['base_eef']
        plan.numerics=SimpleNamespace(call=recorded_fk)
        verify=VerifyService(task,clock=clock,fixture=cfg['fixture'])
        servers=[];gateway=None;bridge=None
        try:
            servers.append(UnixServer(runtime/'plan.sock',plan.dispatch,scene_evidence=True).start())
            servers.append(UnixServer(runtime/'verify.sock',verify.dispatch,scene_evidence=True).start())
            numerical=UnixClient(runtime/'plan.sock',scene_evidence=True)
            bridge=OwnerBridge(engine,numerical_planner=lambda *a:None,result_verifier=lambda *a:None,
                feedback_age_ns=500_000_000,reference_tolerance_deg=.1,clock=clock)
            bridge.geometry_provider=lambda o,f,h:numerical.call('geometry_context',observation=observation_wire(o),full22=f,held16=h)
            bridge.update_feedback(full22=snapshot['feedback']['full22'],measured16=scene.state16,
                stamps16=snapshot['feedback']['stamps16'])
            bridge.serve(runtime/'owner.sock');owner=UnixClient(runtime/'owner.sock',scene_evidence=True)
            started=time.perf_counter()
            UnixClient(runtime/'verify.sock',scene_evidence=True).call('update_scene',observation=wire)
            owner.call('ingest_live_scene',observation=wire,context=engine.status()['perception_context'])
            owner.call('ingest_observation',observation=wire)
            report['timings_ms']['verifier_owner_ingestion']=(time.perf_counter()-started)*1000
            assert verify.latest.local_collision_evidence==original.local_collision_evidence
            assert engine.observation.local_collision_evidence==original.local_collision_evidence
            assert engine.live_observation.local_collision_evidence==original.local_collision_evidence
            report['checks']['verified_full_evidence_restored']=True
            started=time.perf_counter();context=owner.call('geometry_context',session_id=engine.session)
            report['timings_ms']['owner_plan_geometry_context']=(time.perf_counter()-started)*1000
            assert len(json.dumps(context).encode())<MAX_MESSAGE
            proxy=OwnerProxy(runtime/'owner.sock')
            observer=SimpleNamespace(observe=lambda:scene,readiness=lambda:dict(state='ready',code='ready',
                geometry_scope=scene.geometry_scope,geometry_status='partial'))
            gateway=GatewayServer(Gateway(proxy,observer,store)).start()
            remote=RemoteTaskBackend.__new__(RemoteTaskBackend)
            remote.rpc=HttpRpcClient(gateway.url,enable_sessions=False);remote.task=task
            remote.session=engine.session;remote.instance=engine.instance;remote.steps=0
            remote.pending=None;remote._solved=False;remote._closed=False;remote.observation_snapshot_supported=True
            reply=remote.call('observe_with_status')
            assert len(json.dumps(reply).encode())<MAX_MESSAGE
            assert reply['observation'].get('local_collision_evidence') is None
            assert reply['observation']['local_collision_evidence_ref']['blob_sha256']==scene.local_collision_evidence_ref.blob_sha256
            report['checks']['http_observation_has_reference_without_depth']=True
            # Actual host backend + private sandbox backend dispatch + worker
            # client, while not launching a model program or OS sandbox here.
            from f1_harness.sandbox import SandboxedArm
            from f1_harness.worker import RemoteBackend
            arm=SandboxedArm.__new__(SandboxedArm);arm.backend=remote;arm._program_execution=None
            servers.append(UnixServer(runtime/'backend.sock',arm._backend_call).start())
            worker=RemoteBackend(runtime/'backend.sock')
            elapsed=[];sizes=[]
            for _ in range(5):
                start=time.perf_counter();result=worker.execute_tool('observe',{})
                elapsed.append((time.perf_counter()-start)*1000)
                encoded=json.dumps(result).encode();sizes.append(len(encoded))
                assert len(encoded)<MAX_MESSAGE
                assert b'fine_depth' not in encoded and b'local_collision_evidence' not in encoded
                assert result['geometry']['observation_id']==scene.observation_id
            report['timings_ms']['five_state_reads']=elapsed
            report['worker_reply_bytes']=sizes
            report['checks']['worker_state_and_geometry_bounded']=True
            # Source-bound wrong references are rejected by real RPC dispatch.
            bad=deepcopy(wire);bad['local_collision_evidence_ref']['blob_sha256']='0'*64
            try:owner.call('ingest_observation',observation=bad)
            except RuntimeError:report['checks']['missing_reference_rejected']=True
            else:raise AssertionError('missing reference accepted')
            assert engine.observation.observation_id==scene.observation_id
            assert not engine.request_publications
            report['checks']['no_motion_publications']=True
            report['reference']=scene.local_collision_evidence_ref.model_dump()
            (output/'wire-observation.json').write_text(json.dumps(wire))
            (output/'geometry.json').write_text(json.dumps(context))
            import f1_harness
            package=Path(f1_harness.__file__).parent
            report['source_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.glob('*.py')}
        finally:
            if gateway is not None:gateway.close()
            if bridge is not None:bridge.close()
            for server in servers:server.close()
    report['passed']=all(report['checks'].values())
    (output/'report.json').write_text(json.dumps(report,indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('scene','snapshot','site','output'):parser.add_argument('--'+key,required=True,type=Path)
    args=parser.parse_args();value=run(args.scene,args.snapshot,args.site,args.output)
    print(json.dumps({k:value[k] for k in ('passed','wire_scene_bytes','timings_ms','checks')}))

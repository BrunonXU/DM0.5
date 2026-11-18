"""Saved model calls -> sandbox/RPent hints -> actual SAM/real RGB-D geometry.

Playback pauses for model/segmentation work. Source timestamps are never edited.
This local fixture has no motion transport. It is not a physical rollout or a
real-time camera validation; its resulting scenes can feed numerical replays.
"""
import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import numpy as np
from f1_harness.contracts import Mode,Observation,Task
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.gateway import Gateway,GatewayServer,blob_server
from f1_harness.head_kinematics import HeadKinematics
from f1_harness.live_scene import LiveSceneObserver
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.sandbox import SandboxedArm
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.task_runner import EvidenceRemoteBackend

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--capture',type=Path,required=True)
p.add_argument('--native-feedback',type=Path,required=True)
p.add_argument('--mask-config',type=Path,required=True)
p.add_argument('--model-receipt',type=Path,action='append',required=True)
p.add_argument('--sam-url',required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--simulate-single-slot',action='store_true',
               help='Counterfactual old mailbox: retain only the last camera prompt')
a=p.parse_args()
if urlparse(a.sam_url).hostname!='127.0.0.1':raise ValueError('local_sam_only')
a.output.mkdir(parents=True,exist_ok=False)
root=Path(__file__).resolve().parents[1];infra=root.parent/'F1-4242'
source_sha256={str(q):hashlib.sha256(q.read_bytes()).hexdigest() for q in
    [a.capture,a.native_feedback,a.mask_config,*a.model_receipt,Path(__file__),
     *sorted((root/'src/f1_harness').glob('*.py'))]}
capture=json.loads(a.capture.read_text());capture_sha=hashlib.sha256(a.capture.read_bytes()).hexdigest()
native=json.loads(a.native_feedback.read_text())
if native['capture_sha256']!=capture_sha:raise ValueError('native_capture_mismatch')
config=deepcopy(capture['configuration']['scene'])
config['geometry']['independent_fixture_localization']=True
head=HeadKinematics(dict(config['head_kinematics'],
    urdf_path=str(infra/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'),
    mapping_path=str(infra/'configs/model_mapping.json')))
masks=json.loads(a.mask_config.read_text())
for cfg in masks['views'].values():
    if cfg['backend']!='sam2_remote':raise ValueError('actual_sam_for_all_views_required')
    cfg.update(sam_url=a.sam_url,timeout_seconds=10.,require_explicit_target_prompt=True)
    cfg.pop('point_prompts',None)  # No manually reviewed point seeds.
provider=ConfiguredMaskProvider(masks)
segment_frame=provider.segment_frame;mask_counts=Counter();mask_samples=[]
def recorded_masks(rgb,*,view,source,boxes=None):
    result=segment_frame(rgb,view=view,source=source,boxes=boxes)
    mask_counts[view]+=1
    if mask_counts[view] in (2,20,40):
        import cv2
        prefix=f"{view}-{mask_counts[view]:03d}"
        overlay=np.array(rgb,copy=True)
        colors=[(255,70,40),(20,230,50),(20,170,255),(255,0,255),(255,200,0),(20,255,230)]
        for index,(name,mask) in enumerate(result.items()):
            color=colors[index%len(colors)]
            contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(overlay,contours,-1,color,2)
            cv2.putText(overlay,name,(10,20+index*21),cv2.FONT_HERSHEY_SIMPLEX,.5,color,1)
        cv2.imwrite(str(a.output/(prefix+'.png')),cv2.cvtColor(overlay,cv2.COLOR_RGB2BGR))
        np.savez_compressed(a.output/(prefix+'.npz'),**result)
        mask_samples.append(dict(view=view,source=source,overlay=prefix+'.png',masks=prefix+'.npz',
            areas_px={k:int(v.sum()) for k,v in result.items()}))
    return result
provider.segment_frame=recorded_masks
store=EvidenceStore(a.output/'owner');client_store=EvidenceStore(a.output/'planner')
for blob in (a.capture.parent/'blobs').iterdir():
    data=blob.read_bytes()
    if store.put_blob(data)!=blob.name:raise ValueError('source_blob_mismatch')
raw=[Observation.model_validate(s['observation']) for s in capture['snapshots']]
current=[raw[0]];clock=[raw[0].received_ns]
engine=TaskEngine(store,mode=Mode.HARDWARE,clock=lambda:clock[0])
# Only an in-memory authorization contract; no owner, actuator, ROS or robot.
engine.hardware_ready=True;engine.web_control_enabled=True
engine.recording_identity=dict(owner_run_id='offline-hint-pipeline',offline_fixture=True)
observer=LiveSceneObserver.__new__(LiveSceneObserver)
observer.raw=SimpleNamespace(store=store,observe=lambda:current[0]);observer.store=store
observer.owner=SimpleNamespace(call=lambda method:engine.status())
observer.config=config;observer.lock=threading.Lock();observer.image_issuances={}
observer.pending_target_hints={};observer.target_hint_generation=0
observer.latest=None;observer.error='mask_uncertain_requires_new_prompt'
observer.pipeline=CalibratedScenePipeline(config['geometry'],provider,store.blob)
wire=[]
class ReadOnlyGateway(Gateway):
    def dispatch(self,method,args=(),kwargs=None,**transport):
        if method not in ('capabilities','open_session','prepare_task','heartbeat','status',
                          'camera_observation','locate_target','close_session'):
            raise ValueError('offline_hint_fixture_forbids_motion_and_scene_injection')
        wire.append(method)
        result=super().dispatch(method,args=args,kwargs=kwargs,**transport)
        if method=='prepare_task':engine.authorize_web(request_id='offline-only',
            run_id='offline-hint-pipeline',feedback_healthy=True)
        return result
gateway=GatewayServer(ReadOnlyGateway(engine,observer,store)).start();images=blob_server(store)
backend=None;arm=None;rows=[];chain=[];tools=[]
try:
    task=Task(task_id='offline-hints',object_id=config['geometry']['object_id'],
        hole_id=config['geometry']['hole_id'],calibration_id=config['geometry']['calibration_id'],
        admission_profile='development_coarse')
    backend=EvidenceRemoteBackend(gateway.url,task,client_store,
        f'http://127.0.0.1:{images.server_address[1]}')
    arm=SandboxedArm(backend,a.output/'worker').start()
    response_calls=[];shown=set();receipts=[]
    for receipt_path in a.model_receipt:
        receipt=json.loads(receipt_path.read_text());receipts.append(receipt)
        if receipt['capture_sha256']!=capture_sha:raise ValueError('model_capture_mismatch')
        responses=list(receipt_path.parent.glob('llm-*-response.json'))
        contents=[json.loads(q.read_text())['content'] for q in responses]
        matching=[v for v in contents if v.get('id')==receipt['response_id']]
        if len(matching)!=1:raise ValueError('model_response_identity_missing')
        for call in matching[0]['output']:
            if call.get('type')!='function_call':continue
            if call['name']!='locate_entities':raise ValueError('unexpected_saved_model_tool')
            args=json.loads(call['arguments']);camera=args['camera']
            if camera not in shown:
                result=arm.execute_tool('python',dict(code=f'robo.show({camera!r})'))
                if result.result.get('exception') or not result.images:raise ValueError('formal_image_path_failed')
                shown.add(camera)
            meta=backend.last_viewed_images[camera];expected=receipt['metadata'][camera]
            if any(meta[k]!=expected[k] for k in ('frame','observation_id','source_image_size','model_image_size','image_sha256')):
                raise ValueError('shown_image_does_not_match_saved_model_response')
            response_calls.append(args)
    # Replay the saved head and wrist calls in ONE cell, before any new frame.
    code='\n'.join(f"robo.locate_entities({v['camera']!r}, {v['entities']!r})" for v in response_calls)
    result=arm.execute_tool('python',dict(code=code)).result;tools.append(result)
    if result.get('exception'):raise ValueError('formal_prompt_path_failed')
    queued=sorted(observer.pending_target_hints)
    if queued!=['head','right']:raise ValueError('one_camera_prompt_overwrote_other')
    if a.simulate_single_slot:
        last=response_calls[-1]['camera']
        observer.pending_target_hints={last:observer.pending_target_hints[last]}
    context=engine.status()['perception_context']
    for obs in raw:
        current[0]=obs
        observer._apply_target_hint(obs,context)
        feedback=native['rows'][obs.observation_id]
        # Replay measured feedback association exactly as the saved FK receipt.
        target=min(f.received_ns for f in obs.frames if f.camera=='right')
        state=min(capture['joint_samples'],key=lambda v:abs(v['received_ns']-target))
        obs=obs.model_copy(update=dict(state16=tuple(feedback['measured16']),
            state_valid_mask=tuple([True]*14+[False,True]),state_received_ns=state['received_ns']))
        started=time.perf_counter();scene=None;error=None
        try:scene=observer.pipeline.process(obs,base_eef=feedback['base_eef'],context=context,
                                          head_camera_pose=head.pose(feedback['full22']))
        except ValueError as exc:error=str(exc)
        rows.append(dict(observation_id=obs.observation_id,elapsed_ms=(time.perf_counter()-started)*1000,
            error=error,mask_diagnostics=provider.diagnostic_snapshot(),
            scene=None if scene is None else scene.model_dump(mode='json')))
        if scene is not None:
            # Partial scenes legitimately retain only the available camera.
            # Every retained frame must still be the exact original sample.
            assert scene.received_ns==obs.received_ns
            assert scene.frames and all(frame in obs.frames for frame in scene.frames)
            chain.append(dict(observation=scene.model_dump(mode='json'),full22=feedback['full22']))
    events=[json.loads(l) for l in (store.root/'trace.jsonl').read_text().splitlines()]
    applied=[e for e in events if e['kind']=='target_search_hint_applied']
    good=[v for v in rows if v['scene']]
    report=dict(scope=__doc__,robot_connected=False,physical_motion=False,api_calls=0,
        model='saved actual Astra calls, replayed through real sandbox/PyRUA/RPent',
        fault_injection='old_single_pending_hint_slot' if a.simulate_single_slot else None,
        source_clock_unchanged=True,queued_cameras=queued,applied=applied,wire_calls=wire,
        snapshots=len(rows),valid_scenes=len(good),full_scenes=sum(len(v['scene']['objects'])==2 for v in good),
        independent_fixtures=sum(v['scene'].get('fixture') is not None for v in good),
        median_ms=float(np.median([v['elapsed_ms'] for v in rows])),
        errors=dict(Counter(v['error'] for v in rows if v['error'])),rows=rows,mask_samples=mask_samples,
        source_sha256=source_sha256,
        source_unchanged=all(hashlib.sha256(Path(q).read_bytes()).hexdigest()==sha
                             for q,sha in source_sha256.items()))
    store.write_json('prompt-tool-result.json',tools)
    (a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    (a.output/'chain-inputs.json').write_text(json.dumps(chain,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('rows','source_sha256','wire_calls','scope')},indent=2))
finally:
    if arm is not None:arm.close()
    if backend is not None:backend.close_images();backend.cancel()
    gateway.close();images.shutdown();images.server_close()
    observer.pipeline.close();provider.close()

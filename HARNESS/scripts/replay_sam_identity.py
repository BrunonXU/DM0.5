"""Replay archived identity failures through SAM and calibrated scene code.

No owner, actuator, planner API, or timestamp rewriting. A supplied occlusion
JPEG exercises the recorded reseed boundary; complete RGB-D keyframes exercise
the real pipeline. Neither is a substitute for a continuous-motion recording.
"""
import argparse
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import uuid

import cv2
import numpy as np
import f1_harness

from f1_harness.contracts import Frame, Observation, digest
from f1_harness.images import decode
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.sam_service import RemoteSam2Segmenter
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.scene_views import rectified_view
from f1_harness.target_relocalization import source_search_box, rectified_search_box
import f1_harness.scene_pipeline as scene_module


def rows(path):return [json.loads(line) for line in Path(path).read_text().splitlines()]


def rectified_pixels(rgb,cfg):
    k=np.asarray(cfg['k_rgb']);maps=cv2.initUndistortRectifyMap(k,np.asarray(cfg['d_rgb']),
        None,k,(rgb.shape[1],rgb.shape[0]),cv2.CV_32FC1)
    rgb=cv2.remap(rgb,*maps,interpolation=cv2.INTER_LINEAR)
    x0,y0,x1,y1=cfg['roi'];return rgb[y0:y1,x0:x1]


def bounds(mask):
    y,x=np.nonzero(mask)
    return None if not len(x) else [int(x.min()),int(y.min()),int(x.max())+1,int(y.max())+1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--gateway-trace',type=Path,required=True)
    parser.add_argument('--kinematics',type=Path,required=True)
    parser.add_argument('--occlusion-image',type=Path,required=True)
    parser.add_argument('--sam-url',default='http://127.0.0.1:19844')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    original=rows(args.run/'planner/trace.jsonl');gateway=rows(args.gateway_trace)
    observations=[Observation.model_validate(r['observation']) for r in original if r['kind']=='remote_observation']
    config=json.loads((args.site/'gateway.local.json').read_text())['scene']['geometry']
    masks=json.loads((args.site/'masks.local.json').read_text())
    feedback={r['observation_id']:r for r in json.loads(args.kinematics.read_text())['rows']}
    blob_root=args.run/'planner/blobs'
    def blob(sha):
        data=(blob_root/sha).read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('recorded_blob_changed')
        return data
    def frame(obs,camera):return next(f for f in obs.frames if f.camera==camera and f.stream=='rgb')
    views={'right':config,'head':config['hole_camera']}
    def pixels(obs,camera):return rectified_view(obs,views[camera],blob,{},allow_receipt=True)[0]
    missing=next(r for r in gateway if r['kind']=='scene_timing' and
        r['timing'].get('pipeline',{}).get('segmentation',{}).get('head',{}).get('square-1',{}).get('tracking',{}).get('seed_reason')=='semantic_scope_ambiguous_or_empty')
    diag=missing['timing']['pipeline']['segmentation']['head']['square-1']
    hidden_ns=diag['tracking']['source_ns'];box=diag['attempted_boxes'][0]
    before=max((o for o in observations if frame(o,'head').source_ns<hidden_ns),key=lambda o:frame(o,'head').source_ns)
    after=min((o for o in observations if frame(o,'head').source_ns>hidden_ns),key=lambda o:frame(o,'head').source_ns)
    model_sha=masks['views']['right']['checkpoint_sha256']
    remote=RemoteSam2Segmenter(args.sam_url,model_sha256=model_sha,timeout=30.)
    receipt=dict(schema='f1-sam-identity-keyframe-replay-v1',robot_connected=False,
        physical_motion=False,paid_model_calls=0,experience_eligible=False,
        original_source_clocks_preserved=True,run_id=args.run.name,discovery=[],identity_sequence=[],geometry=[],measurement_inputs=[])
    receipt['source_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [args.kinematics,args.gateway_trace,args.occlusion_image,args.site/'masks.local.json',
         args.site/'gateway.local.json',Path(__file__),*sorted(Path(f1_harness.__file__).resolve().parent.glob('*.py'))]}
    receipt['runtime_root']=str(Path(f1_harness.__file__).resolve().parents[2])
    manifest=Path(receipt['runtime_root'])/'manifest.json'
    if manifest.exists():receipt['runtime_manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
    run=uuid.uuid4().hex
    def record(name,camera,image,ns,*,stream,discover=False,reseed=False):
        start=time.perf_counter()
        if discover:(mask,score),meta=remote.discover(image,stream_id=stream,object_id='square-1',source_ns=ns)
        else:(mask,score),meta=remote.track(image,box=box,stream_id=stream,object_id='square-1',source_ns=ns,reseed=reseed)
        np.savez_compressed(args.output/(name+'.npz'),mask=mask)
        return dict(name=name,camera=camera,source_ns=ns,area_px=int(mask.sum()),box=bounds(mask),
            score=score,metadata=meta,elapsed_ms=(time.perf_counter()-start)*1000)
    for camera in views:
        o=observations[0]
        receipt['discovery'].append(record('discovery-'+camera,camera,pixels(o,camera),frame(o,camera).source_ns,
            stream=run+'-discover-'+camera,discover=True))
    stream=run+'-recorded-reseed'
    receipt['identity_sequence'].append(record('before-occlusion','head',pixels(before,'head'),
        frame(before,'head').source_ns,stream=stream))
    hidden=rectified_pixels(cv2.cvtColor(cv2.imread(str(args.occlusion_image)),cv2.COLOR_BGR2RGB),views['head'])
    receipt['identity_sequence'].append(record('recorded-reseed-boundary','head',hidden,hidden_ns,
        stream=stream,reseed=True))
    for i,o in enumerate([after,*[v for v in observations if v.received_ns>after.received_ns]]):
        receipt['identity_sequence'].append(record('after-occlusion-'+str(i),'head',pixels(o,'head'),
            frame(o,'head').source_ns,stream=stream))
    remote.close()
    # Reapply only the actual model-issued localization, mapped with the same
    # source-image contract. Do not seed the faulty post-occlusion hand box.
    hints=None
    for path in (args.run/'planner').glob('*-tool-*.json'):
        code=json.loads(path.read_text())['content'].get('arguments',{}).get('code','')
        for node in ast.parse(code).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='hints' for t in node.targets):
                hints=ast.literal_eval(node.value)
    if hints is None:raise ValueError('recorded_entity_hints_missing')
    for v in masks['views'].values():v.update(sam_url=args.sam_url,timeout_seconds=30.)
    provider=ConfiguredMaskProvider(masks)
    for camera,entities in hints.items():
        issued=next(r for r in original if r['kind']=='planner_image_requested' and r['camera']==camera)
        f=frame(observations[0],camera).model_copy(update=dict(blob_sha256=issued['source_blob_sha256'],source_ns=issued['source_ns']))
        meta=dict(camera=camera,frame=f.model_dump(),observation_id=issued['observation_id'],
            roi=issued['roi'],model_image_size=issued['size'],source_image_size=[f.width,f.height])
        image=rectified_pixels(decode(f,blob(f.blob_sha256)),views[camera])
        source=dict(source_ns=f.source_ns,stream=dict(acquisition_instance=observations[0].acquisition_instance,
            host_boot_id=observations[0].host_boot_id,camera=camera,frame_id=f.frame_id,source_epoch=f.source_epoch,
            publisher_id=f.publisher_id,width=f.width,height=f.height))
        for entity in entities:
            mapped=rectified_search_box(source_search_box(meta,entity['box_normalized']),views[camera])
            provider.set_search_hint(view=camera,object_id=entity['object_id'],box=mapped['box'])
            provider.seed_issued_image(view=camera,object_id=entity['object_id'],rgb=image,source=source)
    pipeline=CalibratedScenePipeline(deepcopy(config),provider,blob)
    original_measure=scene_module.object_from_mask;current_id=None
    def record_measure(**kw):
        name='measurement-input-'+str(len(receipt['measurement_inputs']))+'.npz'
        np.savez_compressed(args.output/name,aligned_depth_m=kw['aligned_depth_m'],mask=kw['mask'],
            k_rgb=kw['k_rgb'],base_camera=kw['base_camera'])
        entry={key:value for key,value in kw.items() if key not in
            ['aligned_depth_m','mask','k_rgb','base_camera','diagnostics']}
        entry.update(file=name,observation_id=current_id,source='unchanged_object_from_mask_inputs_pass_through')
        receipt['measurement_inputs'].append(entry)
        try:return original_measure(**kw)
        except ValueError as exc:entry['error']=str(exc);raise
    scene_module.object_from_mask=record_measure
    context=json.loads(next((args.run/'collected/owner/scene/failures').glob('*.json')).read_text())['context']
    events=rows(args.run/'collected/owner/harness/trace.jsonl')
    try:
        for o in observations:
            current_id=o.observation_id
            phase='observe'
            for event in events:
                if event['monotonic_ns']<=o.received_ns and event['kind']=='harness_planning_started':phase=event['intent']['skill']
            ctx=dict(context,phase=phase,authorized=False,request_id=None)
            raw=o.model_copy(update=dict(objects=(),fixture=None,quality_warnings=()))
            start=time.perf_counter();result=None;error=None
            try:result=pipeline.process(raw,base_eef=feedback[o.observation_id]['base_eef'],context=ctx,
                                       head_camera_pose=feedback[o.observation_id]['head_pose'])
            except ValueError as exc:error=str(exc)
            receipt['geometry'].append(dict(observation_id=o.observation_id,phase=phase,error=error,
                elapsed_ms=(time.perf_counter()-start)*1000,geometry_camera=None if result is None else result.geometry_camera,
                geometry_scope=None if result is None else result.geometry_scope,
                output_observation=None if result is None else result.model_dump(),
                objects=[] if result is None else [x.model_dump() for x in result.objects],
                timing=pipeline.last_timing,segmentation=provider.diagnostic_snapshot()))
    finally:pipeline.close();provider.close();scene_module.object_from_mask=original_measure
    receipt['limitations']=['Six original RGB-D keyframes plus one archived JPEG are not a continuous video replay.',
        'Recorded reseed request is explicitly exercised; intermediate tracker states are not reconstructed.',
        'Geometry is recomputed with recorded feedback/FK; no new motion, attachment or task success is established.']
    (args.output/'report.json').write_text(json.dumps(receipt,indent=2,allow_nan=False)+'\n')
    (args.output/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(dict(report=str(args.output/'report.json'),
        sequence=[{k:r[k] for k in ('name','area_px','box')} for r in receipt['identity_sequence']],
        geometry=[dict(observation_id=r['observation_id'],error=r['error'],camera=r['geometry_camera']) for r in receipt['geometry']])))


if __name__=='__main__':main()

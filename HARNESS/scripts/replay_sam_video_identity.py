"""Replay original camera-pack RGB and timestamps through the existing mask provider.

Depth is intentionally not synthesized for this motion recording. Geometry is
validated separately from archived RGB-D samples by replay_sam_identity.py.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
import f1_harness

from f1_harness.contracts import Observation
from f1_harness.images import decode
from f1_harness.mask_service import ConfiguredMaskProvider
from replay_sam_identity import rows,rectified_pixels,bounds


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--site',type=Path,required=True)
    p.add_argument('--gateway-trace',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--frames',type=Path,required=True)
    p.add_argument('--sam-url',default='http://127.0.0.1:19844')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    config=json.loads((a.site/'gateway.local.json').read_text())['scene']['geometry']
    masks=json.loads((a.site/'masks.local.json').read_text());views={'right':config,'head':config['hole_camera']}
    for cfg in masks['views'].values():
        cfg.update(sam_url=a.sam_url,timeout_seconds=30.,prompts={'square-1':cfg['prompts']['square-1']},
            sam_objects=['square-1'],sam_streaming_objects=['square-1'],optional_ids=[],
            scene_validated_overlap_pairs=[],mask_regions={},color_profiles={})
        cfg.pop('fixture_tracking_group',None)
    provider=ConfiguredMaskProvider(masks)
    original=rows(a.run/'planner/trace.jsonl');gateway=rows(a.gateway_trace)
    o=Observation.model_validate(next(r['observation'] for r in original if r['kind']=='remote_observation'))
    def stream(camera):
        f=next(f for f in o.frames if f.camera==camera and f.stream=='rgb')
        return dict(acquisition_instance=o.acquisition_instance,host_boot_id=o.host_boot_id,
            camera=camera,frame_id=f.frame_id,source_epoch=f.source_epoch,publisher_id=f.publisher_id,
            width=f.width,height=f.height)
    # Original image-bound right hint and independently projected head search
    # region, exactly as present before motion in the actual run. No answer
    # masks, human-drawn moving boxes, or future pixels are fed into tracking.
    issued=next(r for r in original if r['kind']=='planner_image_requested' and r['camera']=='right')
    f=next(f for f in o.frames if f.camera=='right' and f.stream=='rgb')
    image=rectified_pixels(decode(f,(a.run/'planner/blobs'/issued['source_blob_sha256']).read_bytes()),views['right'])
    right_hint=next(r['box'] for r in gateway if r['kind']=='target_search_hint_applied' and r['camera']=='right')
    provider.set_search_hint(view='right',object_id='square-1',box=right_hint)
    provider.seed_issued_image(view='right',object_id='square-1',rgb=image,
        source=dict(source_ns=issued['source_ns'],stream=stream('right')))
    head_hint=next(r['timing']['pipeline']['head_object_search_hint']['boxes'] for r in gateway
        if r['kind']=='scene_timing' and r['timing'].get('pipeline',{}).get('head_object_search_hint'))
    manifest=json.loads(a.manifest.read_text());samples=[];first_head=True
    try:
        for row in manifest['records']:
            for f in row['frames']:
                camera='right' if f['camera']=='right_wrist' else f['camera']
                data=(a.frames/f['relative_path']).read_bytes()
                if hashlib.sha256(data).hexdigest()!=f['payload_sha256']:raise ValueError('recorded_image_changed')
                rgb=cv2.cvtColor(cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR),cv2.COLOR_BGR2RGB)
                image=rectified_pixels(rgb,views[camera]);start=time.perf_counter();error=None;mask=None
                try:
                    value=provider.segment_frame(image,view=camera,
                        source=dict(source_ns=f['source_ns'],stream=stream(camera)),
                        boxes=head_hint if camera=='head' and first_head else None)
                    mask=value.get('square-1')
                    if camera=='head':first_head=False
                except ValueError as exc:error=str(exc)
                name=camera+'-'+str(f['source_ns'])
                if mask is not None:np.savez_compressed(a.output/(name+'.npz'),mask=mask)
                samples.append(dict(source_ns=f['source_ns'],camera=camera,original_payload_sha256=f['payload_sha256'],
                    original_feedback_observation_id=row['observation_id'],error=error,
                    area_px=None if mask is None else int(mask.sum()),box=None if mask is None else bounds(mask),
                    elapsed_ms=(time.perf_counter()-start)*1000,
                    diagnostics=provider.diagnostic_snapshot().get(camera,{}).get('square-1')))
                if len(samples)%40==0:print(json.dumps(dict(processed=len(samples))),flush=True)
    finally:provider.close()
    summary={}
    for camera in views:
        data=[r for r in samples if r['camera']==camera]
        times=[r['elapsed_ms'] for r in data]
        tracking=[(r.get('diagnostics') or {}).get('tracking') or {} for r in data]
        summary[camera]=dict(frames=len(data),returned_masks=sum(r['error'] is None for r in data),
            missing_or_truncated=sum(r['error'] is not None for r in data),
            semantic_seed_calls=sum(t.get('seed_inference_count',0) for t in tracking),
            sam2_tracking_calls=sum(t.get('tracking_inference_count',0) for t in tracking),
            seed_total_ms=sum(t.get('seed_inference_ms',0) for t in tracking),
            tracking_total_ms=sum(t.get('tracking_inference_ms',0) for t in tracking),
            elapsed_p50_ms=float(np.percentile(times,50)),elapsed_p95_ms=float(np.percentile(times,95)))
    report=dict(schema='f1-original-rgb-motion-identity-replay-v1',robot_connected=False,physical_motion=False,
        paid_model_calls=0,depth_available=False,geometry_verified=False,original_timestamps_preserved=True,
        source_manifest=str(a.manifest),source_manifest_sha256=hashlib.sha256(a.manifest.read_bytes()).hexdigest(),
        source_sha256={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in [Path(__file__),
            a.site/'masks.local.json',a.site/'gateway.local.json',*sorted(Path(f1_harness.__file__).resolve().parent.glob('*.py'))]},
        runtime_root=str(Path(f1_harness.__file__).resolve().parents[2]),
        initial_search=dict(right=right_hint,head=head_hint),summary=summary,samples=samples,
        limitations=['Original archived JPEGs at 0.2-second minimum source-time intervals, not uncompressed gateway input.',
            'No depth exists in these camera packs. Original full joint feedback remains in the manifest, not fabricated geometry.',
            'Head initialization uses the recorded pre-motion wrist-geometry projection; right uses the original image-bound hint.'])
    manifest=Path(report['runtime_root'])/'manifest.json'
    if manifest.exists():report['runtime_manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
    (a.output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    print(json.dumps(dict(report=str(a.output/'report.json'),summary=summary)))


if __name__=='__main__':main()

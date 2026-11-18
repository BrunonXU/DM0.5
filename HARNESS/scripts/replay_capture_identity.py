"""Consume a read-only native RGB-D/HAL capture with the normal observation/scene code.

Gripper feedback is absent from calibration_capture and stays explicitly unknown.
All original frame/state times are retained. This tool has no robot transport.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pinocchio as pin
import f1_harness
import f1_harness.scene_pipeline as scene_module

from f1dagger.contract import FULL_NAMES,MODEL_NAMES
from f1_harness.contracts import Frame
from f1_harness.head_kinematics import HeadKinematics
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.observation import ObservationBuffer
from f1_harness.scene_pipeline import CalibratedScenePipeline, SecondaryCameraUnavailable
from f1_harness.scene_views import rectified_view
from f1_harness.target_relocalization import rectified_search_box


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True);parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--sam-url',default='http://127.0.0.1:19844')
    parser.add_argument('--diagnostic-right-box',type=int,nargs=4,
        help='Optional human diagnostic box in the CURRENT raw right RGB, never persisted into site configuration.')
    parser.add_argument('--record-measurement-inputs',action='store_true',
        help='Save exact geometry inputs through a pass-through recorder; never alter the geometry result.')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    measurement_inputs=[];original_measure=scene_module.object_from_mask
    if args.record_measurement_inputs:
        def record_measure(**kw):
            index=len(measurement_inputs);name='measurement-input-'+str(index)+'.npz'
            np.savez_compressed(args.output/name,aligned_depth_m=kw['aligned_depth_m'],mask=kw['mask'],
                k_rgb=kw['k_rgb'],base_camera=kw['base_camera'])
            record={k:v for k,v in kw.items() if k not in
                ['aligned_depth_m','mask','k_rgb','base_camera','diagnostics']}
            record.update(file=name,source='unchanged_object_from_mask_inputs_pass_through')
            measurement_inputs.append(record)
            try:return original_measure(**kw)
            except Exception as exc:record['error']=str(exc);raise
        scene_module.object_from_mask=record_measure
    root=Path(__file__).resolve().parents[2];infra=root/'F1-4242'
    capture=json.loads((args.capture/'capture.json').read_text());cfg=json.loads((args.site/'gateway.local.json').read_text())
    geometry=cfg['scene']['geometry'];mask_cfg=json.loads((args.site/'masks.local.json').read_text())
    for v in mask_cfg['views'].values():v.update(sam_url=args.sam_url,timeout_seconds=30.)
    provider=ConfiguredMaskProvider(mask_cfg)
    urdf=infra/'web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf';mapping=infra/'configs/model_mapping.json'
    head_cfg=dict(cfg['scene']['head_kinematics'],urdf_path=str(urdf),mapping_path=str(mapping))
    head=HeadKinematics(head_cfg);model=pin.buildModelFromUrdf(str(urdf));data=model.createData()
    signs=json.loads(mapping.read_text())['joint_signs'];eef_id=model.getFrameId('arm_R_Link7')
    def full(row):
        lookup=dict(zip(row['names'],row['position']));return [lookup[n] for n in FULL_NAMES]
    def pose(values):
        values=np.deg2rad(values)*[signs.get(n,1) for n in FULL_NAMES];q=pin.neutral(model)
        for name,value in zip(MODEL_NAMES,values):
            jid=model.getJointId(name);q[model.idx_qs[jid]]=value
        pin.forwardKinematics(model,data,q);pin.updateFramePlacements(model,data)
        result=np.eye(4);result[:3,:3]=data.oMf[eef_id].rotation;result[:3,3]=data.oMf[eef_id].translation
        return result
    memory={};infos={};clock=[0]
    buffer=ObservationBuffer(source_clock_verified=False,clock=lambda:clock[0])
    buffer.instance='recorded-'+hashlib.sha256((args.capture/'capture.json').read_bytes()).hexdigest()[:24]
    # Acquisition belongs to the archived robot, not this replay host.
    buffer.host_boot_id=capture.get('host_boot_id') or 'recorded-native-capture-boot-unavailable'
    def blob(sha):
        raw=memory.get(sha)
        if raw is None:raw=(args.capture/'blobs'/sha).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('capture_blob_changed')
        return raw
    topics={item[stream]['topic']:(item,stream) for item in cfg['observation']['cameras'] for stream in ['rgb','depth']}
    joints=[r for r in capture['records'] if r['kind']=='joint_state']
    grippers=[r for r in capture['records'] if r['kind']=='gripper_state'
        and r.get('names')==['right_gripper_position'] and len(r.get('position',[]))==1]
    pipeline=CalibratedScenePipeline(deepcopy(geometry),provider,blob);results=[];skipped=[];last_right=None;issued_hint=None
    context=dict(owner_instance_id='offline-none',session_id=buffer.instance,task_id=buffer.instance,
        task_hash='offline-no-action',epoch=1,task_generation=1,phase='observe',authorized=False,placement_verified=False)
    try:
        for row in sorted(capture['records'],key=lambda r:r['received_ns']):
            clock[0]=row['received_ns']
            if row['kind']=='camera_info':
                info={k:row[k] for k in ['k','d','r','p','width','height','frame_id','distortion_model']}
                raw=json.dumps(info,sort_keys=True).encode();sha=hashlib.sha256(raw).hexdigest();memory[sha]=raw
                infos[row['topic']]=sha
            if row['kind']!='image' or row['topic'] not in topics:continue
            camera,stream=topics[row['topic']];info_sha=infos.get(camera[stream]['camera_info_topic'])
            if info_sha is None:continue
            expected=geometry if camera['name']=='right' else geometry['hole_camera']
            if info_sha!=expected[stream+'_info_sha256']:raise ValueError('capture_calibration_identity_changed:'+camera['name']+':'+stream)
            frame=Frame(**{k:row[k] for k in ['sequence','received_ns','source_ns','frame_id','width','height','encoding','row_step_bytes','is_bigendian','blob_sha256']},
                camera=camera['name'],stream=stream,clock_domain=camera['source_clock_domain'],
                camera_info_sha256=info_sha,depth_scale_m=camera[stream].get('depth_scale_m'))
            buffer.add(frame);obs=buffer.snapshot()
            right=next((f for f in obs.frames if f.camera=='right' and f.stream=='rgb'),None)
            head_frame=next((f for f in obs.frames if f.camera=='head' and f.stream=='rgb'),None)
            pair=next((p for p in obs.pairings if p.camera=='right'),None)
            if right is None or head_frame is None or pair is None or pair.quality=='invalid' or right.source_ns==last_right:continue
            if len(obs.frames)!=4:continue
            last_right=right.source_ns
            j=min(joints,key=lambda r:abs(r['received_ns']-right.received_ns))
            hj=min(joints,key=lambda r:abs(r['received_ns']-head_frame.received_ns));values=full(j)
            if abs(j['received_ns']-right.received_ns)>cfg['scene']['frame_state_skew_ms']*1e6:
                skipped.append(dict(observation_id=obs.observation_id,reason='recorded_arm_feedback_frame_skew',
                    feedback_delta_ns=j['received_ns']-right.received_ns,configured_frame_state_skew_ms=cfg['scene']['frame_state_skew_ms']))
                continue
            available_grips=[g for g in grippers if 0<=obs.received_ns-g['received_ns']<=cfg['scene']['feedback_age_ms']*1e6]
            grip=max(available_grips,key=lambda g:g['received_ns']) if available_grips else None
            state_time=min(j['received_ns'],grip['received_ns']) if grip else j['received_ns']
            obs=obs.model_copy(update=dict(state16=tuple(values[:14]+[0.,0. if grip is None else grip['position'][0]]),
                state_valid_mask=tuple([True]*14+[False,grip is not None]),state_received_ns=state_time))
            if args.diagnostic_right_box is not None and issued_hint is None:
                mapped=rectified_search_box(dict(camera='right',box_source_px=args.diagnostic_right_box,
                    source_image_size=[right.width,right.height],frame=right.model_dump()),geometry)
                rgb=rectified_view(obs,geometry,blob,{},allow_receipt=True)[0]
                source=dict(source_ns=right.source_ns,stream=dict(acquisition_instance=obs.acquisition_instance,
                    host_boot_id=obs.host_boot_id,camera='right',frame_id=right.frame_id,source_epoch=right.source_epoch,
                    publisher_id=right.publisher_id,width=right.width,height=right.height))
                provider.set_search_hint(view='right',object_id='square-1',box=mapped['box'])
                provider.seed_issued_image(view='right',object_id='square-1',rgb=rgb,source=source)
                issued_hint=dict(source='human_current_image_diagnostic_only',source_ns=right.source_ns,
                    blob_sha256=right.blob_sha256,raw_box=args.diagnostic_right_box,rectified_box=mapped['box'])
            right_pose=pose(values);head_values=full(hj);head_pose=head.pose(head_values)
            head_pair=next((p for p in obs.pairings if p.camera=='head'),None)
            secondary_error=None
            if head_pair is None or head_pair.quality=='invalid':
                secondary_error=SecondaryCameraUnavailable('secondary_camera_pairing_unavailable')
            if abs(hj['received_ns']-head_frame.received_ns)>cfg['scene']['frame_state_skew_ms']*1e6:
                secondary_error=SecondaryCameraUnavailable('frame_feedback_skew:head')
            started=time.perf_counter();scene=None;error=None;error_chain=[]
            try:scene=pipeline.process(obs,base_eef=right_pose,context=context,head_camera_pose=head_pose,
                                      secondary_error=secondary_error)
            except ValueError as exc:
                error=str(exc);cause=exc
                while cause is not None and str(cause) not in error_chain:
                    error_chain.append(str(cause));cause=cause.__cause__ or cause.__context__
            results.append(dict(observation=obs.model_dump(),error=error,error_chain=error_chain,elapsed_ms=1000*(time.perf_counter()-started),
                geometry_camera=None if scene is None else scene.geometry_camera,
                output_observation=None if scene is None else scene.model_dump(),
                objects=[] if scene is None else [x.model_dump() for x in scene.objects],
                right_feedback_delta_ns=j['received_ns']-right.received_ns,
                head_feedback_delta_ns=hj['received_ns']-head_frame.received_ns,
                right_full22=values,head_full22=head_values,
                right_gripper_feedback=grip,
                state_stamps16=[j['received_ns']]*14+[None,None if grip is None else grip['received_ns']],
                base_eef=np.asarray(right_pose).tolist(),head_camera_pose=np.asarray(head_pose).tolist(),
                secondary_input_unavailable=None if secondary_error is None else str(secondary_error),
                diagnostics=pipeline.last_timing))
    finally:
        pipeline.close();provider.close();scene_module.object_from_mask=original_measure
    report=dict(schema='f1-current-stationary-capture-scene-replay-v1',robot_connected=False,physical_motion=False,
        command_publishers=capture['command_publishers'],original_source_clocks_preserved=True,
        joint_window=capture['joint_window'],rows=results,skipped=skipped,
        diagnostic_hint=issued_hint,runtime_root=str(Path(f1_harness.__file__).resolve().parents[2]),
        measurement_inputs=measurement_inputs,
        summary=dict(samples=len(results),measured=sum(any(o.get('pose') is not None for o in r['objects']) for r in results),
            partial=sum((r['output_observation'] or {}).get('geometry_scope')=='coarse_local_environment' for r in results),
            errors={e:sum(r['error']==e for r in results) for e in {r['error'] for r in results if r['error']}},
            p50_ms=float(np.percentile([r['elapsed_ms'] for r in results],50)) if results else None,
            p95_ms=float(np.percentile([r['elapsed_ms'] for r in results],95)) if results else None),
        source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [args.capture/'capture.json',
            args.site/'gateway.local.json',args.site/'masks.local.json',urdf,mapping,Path(__file__),
            *sorted(Path(f1_harness.__file__).resolve().parent.glob('*.py'))]},
        limitations=['Current static capture, not original moving trial; actual joint ranges are reported.',
            'Independent gripper feedback is valid only when an original named same-window native message exists; no grasp/held success is inferred.',
            'Publisher/boot IDs were not retained by the native capture; replay uses an explicitly synthetic acquisition scope.',
            'Human diagnostic hint is explicitly recorded when supplied; it does not validate autonomous zero-image discovery.'])
    manifest=Path(report['runtime_root'])/'manifest.json'
    if manifest.exists():report['runtime_manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest()
    (args.output/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    (args.output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n');print(json.dumps(report['summary']))


if __name__=='__main__':main()

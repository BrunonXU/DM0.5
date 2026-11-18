"""Read-only diagnostic replay of a saved failure; no owner, robot or model API.

Restores local fixed-color prompts. Geometry is a fresh single-frame pipeline,
not a reconstruction of missing temporal state or a motion-admission verdict.
"""
import argparse
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re

import numpy as np

from f1_harness.contracts import Observation,digest
from f1_harness.geometry import transform
from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.scene_views import rectified_view


def read_json(path, limit=256*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise ValueError('diagnostic_input_not_regular_or_too_large')
    return json.loads(path.read_text())


def replay(snapshot_path, mask_config_path):
    snapshot_path=Path(snapshot_path).absolute();mask_config_path=Path(mask_config_path).absolute()
    record=read_json(snapshot_path);config=read_json(mask_config_path)
    if record.get('schema')!='f1-scene-failure-v1' or record.get('diagnostic_only') is not True:
        raise ValueError('scene_failure_snapshot_required')
    if record.get('snapshot_id')!=snapshot_path.stem:
        raise ValueError('snapshot_filename_identity_mismatch')
    context=record.get('context',{})
    if not all(context.get(k) for k in ('owner_instance_id','session_id','task_id','task_hash')):
        raise ValueError('snapshot_task_identity_missing')
    # Reject SAM/checkpoint loads before constructing any segmentation backend.
    views=config.get('views',{'default':config})
    if not views or any(v.get('backend')!='fixed_color' for v in views.values()):
        raise ValueError('offline_replay_requires_local_fixed_color')
    mask_identity=record.get('mask_config_sha256')
    if mask_identity is not None and mask_identity!=digest(config):
        raise ValueError('mask_configuration_identity_changed')
    obs=Observation.model_validate(record['observation'])
    if len(obs.frames)>8:raise ValueError('diagnostic_frame_count_exceeded')
    blobs=snapshot_path.parent/'blobs'
    if blobs.is_symlink():raise ValueError('diagnostic_blob_directory_symlink')
    verified={};missing=[];total=0
    refs={sha for f in obs.frames for sha in (f.blob_sha256,f.camera_info_sha256) if sha}
    for sha in sorted(refs):
        if not re.fullmatch('[a-f0-9]{64}',sha):raise ValueError('invalid_diagnostic_blob_digest')
        path=blobs/sha
        if not path.exists():missing.append(sha);continue
        if path.is_symlink() or not path.is_file():raise ValueError('diagnostic_blob_not_regular')
        total+=path.stat().st_size
        if total>64*1024**2:raise ValueError('diagnostic_input_budget_exceeded')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha:raise ValueError('diagnostic_blob_hash_mismatch')
        verified[sha]=data
    source_files=[snapshot_path,mask_config_path,Path(__file__)]
    import f1_harness
    # Include transitive local preprocessing/contract code as well as the pipeline.
    source_files += sorted(Path(f1_harness.__file__).parent.rglob('*.py'))
    report=dict(schema='f1-scene-failure-replay-v1',diagnostic_only=True,robot_connected=False,
        paid_model_calls=0,motion_admission=False,experience_promotion_allowed=False,
        recorded_reason=record.get('reason'),context=context,
        input_timestamps=dict(observation_received_ns=obs.received_ns,state_received_ns=obs.state_received_ns,
            frames=[dict(camera=f.camera,stream=f.stream,source_ns=f.source_ns,received_ns=f.received_ns) for f in obs.frames]),
        source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
        verified_blob_sha256=sorted(verified),missing_blobs=missing,views={},geometry=None,
        mask_config_identity_verified=mask_identity is not None,
        matched_feedback=record.get('feedback'),
        limitations=([] if mask_identity is not None else ['Old snapshot has no mask configuration digest.'])+[
            'Saved pixel prompts are restored, but temporal histories, caches and prior scope transitions are not.',
            'Historical search hints are reported, not applied without their missing scope/history proof.',
            'Single-frame geometry cannot establish grasp, stationary, placement, freshness or task success.',
            'Missing data and replay success are never promoted to execution evidence.'])
    report['library_versions']={}
    for name in ('numpy','scipy','opencv-python','opencv-python-headless','pydantic'):
        try:report['library_versions'][name]=version(name)
        except PackageNotFoundError:pass
    search=record.get('search_before') or {}
    report['recorded_search_hint']=search.get('hint')
    saved_prompts=search.get('prompts')
    def provider():
        value=ConfiguredMaskProvider(deepcopy(config),fixed_only=True)
        if saved_prompts is not None:
            if set(saved_prompts)!=set(value.states):raise ValueError('saved_mask_view_identity_mismatch')
            for name,state in value.states.items():
                saved=saved_prompts[name]
                if saved.get('backend')!=state['backend'] or set(saved['prompts'])!=set(state['prompts']):
                    raise ValueError('saved_mask_object_identity_mismatch')
                for box in saved['prompts'].values():
                    a=np.asarray(box,float)
                    if a.shape!=(4,) or not np.isfinite(a).all() or np.any(a[:2]>=a[2:]):
                        raise ValueError('saved_mask_box_invalid')
                state['prompts']=deepcopy(saved['prompts'])
        return value
    mask_provider=provider()
    report['prompts_restored']=saved_prompts is not None
    if missing:
        report['status']='input_incomplete'
        return report
    geometry_config=record.get('geometry_config') or {}
    primary=geometry_config.get('camera')
    if not primary:raise ValueError('recorded_geometry_config_missing')
    camera_configs={primary:geometry_config}
    if geometry_config.get('hole_camera'):
        camera_configs[geometry_config['hole_camera']['camera']]=geometry_config['hole_camera']
    for camera,view_config in camera_configs.items():
        selected=camera if camera in mask_provider.states else mask_provider.default
        try:
            rgb,_,_=rectified_view(obs,view_config,verified.__getitem__,{},
                allow_receipt=geometry_config.get('admission_profile')=='development_coarse')
            masks=mask_provider(rgb,view=selected)
            report['views'][camera]=dict(status='masks_observed',pixels={key:int(mask.sum()) for key,mask in masks.items()})
        except (ValueError,KeyError) as exc:
            report['views'][camera]=dict(status='rejected',reason=str(exc))
    feedback=record.get('feedback');eef=record.get('base_eef')
    if feedback is None or eef is None:
        report['geometry']=dict(status='unavailable',reason='matched_feedback_or_eef_missing')
    else:
        stamps=feedback.get('stamps16',[])
        if len(stamps)!=16 or any(s is None for s in stamps[7:14]):
            report['geometry']=dict(status='unavailable',reason='right_feedback_timestamps_missing')
        else:
            # Preserve recorded times; do not re-stamp the old input as fresh.
            matched=obs.model_copy(update=dict(state16=tuple(feedback['measured16']),state_received_ns=min(stamps[7:14])))
            pipeline=CalibratedScenePipeline(deepcopy(geometry_config),provider(),verified.__getitem__)
            try:
                scene=pipeline.process(matched,base_eef=transform(eef),context=context,
                    **({"head_camera_pose":record["head_camera_pose"]} if record.get("head_camera_pose") is not None else {}))
                report['geometry']=dict(status='single_frame_computed',geometry_scope=scene.geometry_scope,
                    objects=[dict(object_id=o.object_id,frame_id=o.frame_id,pose=o.pose) for o in scene.objects])
            except (ValueError,KeyError) as exc:
                report['geometry']=dict(status='rejected',reason=str(exc))
            finally:pipeline.close()
    report['status']='replayed_with_incomplete_temporal_state'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',required=True,type=Path)
    parser.add_argument('--mask-config',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    result=replay(args.snapshot,args.mask_config)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:json.dump(result,stream,indent=2,allow_nan=False);stream.write('\n')
    print(json.dumps(dict(status=result['status'],output=str(args.output),motion_admission=False)))


if __name__=='__main__':main()

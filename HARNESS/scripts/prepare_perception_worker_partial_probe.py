"""Adapt the existing real initial SAM/RGB-D worker receipt for interface isolation.

Only its real initial observation is consumed. Simulated owner follow-up scenes
and motion are excluded. Original image, mask and feedback identities remain.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
from f1_harness.contracts import Observation
from f1_harness.geometry import transform
from f1_harness.head_kinematics import HeadKinematics
from f1_harness.scene import object_from_mask
from f1_harness.scene_views import rectified_view


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('worker','capture','native-feedback','output'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    worker=json.loads((a.worker/'report.json').read_text());capture=json.loads(a.capture.read_text())
    native=json.loads(a.native_feedback.read_text());row=worker['rows'][0]
    assert row['error'] is None and len(worker['rows'])==1
    obs=Observation.model_validate(row['scene']);cfg=deepcopy(capture['configuration']['scene'])
    cfg['geometry']['independent_fixture_localization']=True;geometry=cfg['geometry']
    feedback=native['rows'][obs.observation_id]
    root=Path(__file__).resolve().parents[2]
    head=HeadKinematics(dict(cfg['head_kinematics'],
        urdf_path=str(root/'F1-4242/web/models/f1_wheel/urdf/f1_wheel_0602_new.urdf'),
        mapping_path=str(root/'F1-4242/configs/model_mapping.json')))
    def blob(sha):
        raw=(a.capture.parent/'blobs'/sha).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('original_blob_changed')
        return raw
    _,depth,evidence=rectified_view(obs,geometry,blob,{},allow_receipt=True)
    frame=next(f for f in obs.frames if f.camera=='right' and f.stream=='rgb')
    full=np.zeros((frame.height,frame.width));x0,y0,x1,y1=geometry['roi'];full[y0:y1,x0:x1]=depth
    mask=np.load(a.worker/'right-001.npz')[geometry['object_id']]
    tracking=row['mask_diagnostics']['right'][geometry['object_id']]['tracking']
    assert tracking['source_ns']==frame.source_ns and tracking['identity_status']=='confirmed'
    pose=transform(feedback['base_eef'])@transform(geometry['eef_camera']);diagnostics={}
    params=dict(object_id=geometry['object_id'],shape='square',calibration_error_m=0.,evidence_ids=list(evidence),
        frame_id=geometry['native_frame'],roi=geometry['roi'],surface_selection=geometry['pregrasp_surface_selection'])
    obj=object_from_mask(**params,aligned_depth_m=full,mask=mask,k_rgb=geometry['k_rgb'],base_camera=pose,diagnostics=diagnostics)
    np.savez_compressed(a.output/'measurement-input-0.npz',aligned_depth_m=full,mask=mask,k_rgb=geometry['k_rgb'],base_camera=pose)
    entry=dict(params,file='measurement-input-0.npz',observation_id=obs.observation_id,
        source='original_saved_SAM_mask_and_recomputed_original_calibrated_depth')
    adapted=dict(schema='real-perception-worker-initial-input-adapter-v1',natural_visual_failure=False,
        physical_motion=False,measurement_inputs=[entry],geometry=[dict(observation_id=obs.observation_id,
            error=None,output_observation=obs.model_dump(),segmentation=row['mask_diagnostics'],timing=diagnostics)],
        sources={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in
            [a.worker/'report.json',a.worker/'right-001.npz',a.worker/'head-001.npz',a.capture,a.native_feedback,Path(__file__)]})
    (a.output/'report.json').write_text(json.dumps(adapted,indent=2)+'\n')
    (a.output/'gateway.local.json').write_text(json.dumps(dict(scene=cfg),indent=2)+'\n')
    (a.output/'kinematics.json').write_text(json.dumps(dict(rows=[dict(observation_id=obs.observation_id,
        base_eef=feedback['base_eef'],head_pose=head.pose(feedback['full22']).tolist(),full22=feedback['full22'])]),indent=2)+'\n')
    print(json.dumps(dict(output=str(a.output),observation_id=obs.observation_id,
        original_gripper=obs.state16[15],fixture=obs.fixture is not None,physical_motion=False)))


if __name__=='__main__':main()

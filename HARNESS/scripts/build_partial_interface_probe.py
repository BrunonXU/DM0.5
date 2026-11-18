"""Withhold a successfully measured pose to isolate the local-motion interface.

This is NOT a naturally failed visual measurement or a hardware experiment.
RGB-D, masks, source times, actual feedback and measured fixture are unchanged.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import numpy as np
from f1_harness.contracts import Observation
from f1_harness.geometry import transform
from f1_harness.partial_scene import observed_target_bounds,registered_box_surface_anchor
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.scene_views import rectified_view


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--observation-id',required=True)
    p.add_argument('--run',type=Path,required=True);p.add_argument('--site',type=Path,required=True)
    p.add_argument('--blob-root',type=Path)
    p.add_argument('--kinematics',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    report=json.loads(a.report.read_text());row=next(r for r in report['geometry'] if r['observation_id']==a.observation_id)
    original=Observation.model_validate(row['output_observation'])
    cfg=deepcopy(json.loads((a.site/'gateway.local.json').read_text())['scene']['geometry'])
    target=next(o for o in original.objects if o.object_id==cfg['object_id'])
    assert target.pose is not None and row['error'] is None
    kin=next(r for r in json.loads(a.kinematics.read_text())['rows'] if r['observation_id']==original.observation_id)
    cfg['hole_camera']['base_camera']=kin['head_pose']
    entry=next(r for r in report['measurement_inputs'] if r['observation_id']==original.observation_id and r['evidence_ids']==list(target.evidence_ids))
    archive=np.load(a.report.parent/entry['file']);camera=original.geometry_camera
    view_cfg=cfg if camera=='right' else cfg['hole_camera']
    tracking=row['segmentation'][camera][cfg['object_id']]['tracking']
    frame=next(f for f in original.frames if f.camera==camera and f.stream=='rgb')
    assert tracking['identity_status']=='confirmed' and tracking['source_ns']==frame.source_ns
    bounds=observed_target_bounds(archive['mask'],archive['aligned_depth_m'],view_cfg,
        archive['base_camera'],cfg['search_object_box'])
    # Recompute only the measured plane diagnostics from these exact archived
    # mask/depth pixels using the current producer; never borrow a later pose.
    from f1_harness.geometry import masked_points,crop_intrinsics
    from f1_harness.surface import select_upright_face
    x0,y0,x1,y1=entry['roi']
    points=masked_points(archive['aligned_depth_m'][y0:y1,x0:x1],archive['mask'],
        crop_intrinsics(archive['k_rgb'],entry['roi']))
    _,surface=select_upright_face(points,archive['base_camera'],entry['surface_selection'])
    anchor=registered_box_surface_anchor(surface,cfg['planning_margin_m'])
    if anchor is not None:bounds['surface_anchor']=anchor
    def blob(sha):
        raw=((a.blob_root if a.blob_root is not None else a.run/'planner/blobs')/sha).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('original_rgbd_blob_changed')
        return raw
    pipeline=CalibratedScenePipeline(cfg,object(),blob);pipeline.last_timing={};pipeline.secondary_future=None
    pipeline._partial_targets=[dict(camera=camera,bounds=bounds,reason='precise_pose_withheld_for_interface_isolation',
        identity_provenance=dict(sam_identity_status='confirmed',identity_source=tracking.get('identity_source'),
            source_ns=tracking['source_ns'],physical_object_pose_available=False,
            isolation_source_actual_pose_was_available=True))]
    pipeline._partial_views={camera:(archive['aligned_depth_m'],view_cfg,archive['base_camera'])}
    for other in ('right','head'):
        if other==camera:continue
        other_cfg=cfg if other=='right' else cfg['hole_camera']
        _,depth,_=rectified_view(original,other_cfg,blob,{},allow_receipt=True)
        f=next(f for f in original.frames if f.camera==other and f.stream=='rgb')
        full=np.zeros((f.height,f.width));x0,y0,x1,y1=other_cfg['roi'];full[y0:y1,x0:x1]=depth
        pose=transform(kin['base_eef'])@transform(cfg['eef_camera']) if other=='right' else kin['head_pose']
        pipeline._partial_views[other]=(full,other_cfg,pose)
    try:conditioned=pipeline._partial_observation(original,'precise_pose_withheld_for_interface_isolation')
    finally:pipeline.close()
    assert conditioned is not None and conditioned.objects[0].pose is None
    if original.fixture is not None:
        assert original.fixture.source=='rgbd_anchor_registration'
        assert set(original.fixture.evidence_ids)<={f.blob_sha256 for f in conditioned.frames}
        conditioned=conditioned.model_copy(update=dict(fixture=original.fixture))
    result=dict(schema='f1-partial-interface-isolation-probe-v1',natural_visual_failure=False,
        precise_pose_intentionally_withheld=True,physical_motion=False,command_publishers=0,
        source_report_sha256=hashlib.sha256(a.report.read_bytes()).hexdigest(),
        source_measurement_input_sha256=hashlib.sha256((a.report.parent/entry['file']).read_bytes()).hexdigest(),
        original_observation=original.model_dump(),output_observation=conditioned.model_dump(),
        kinematics=kin,diagnostics=pipeline.last_timing,
        fixture_source='same_current_independently_measured_fixture' if original.fixture is not None else None,
        runtime_source_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in
            sorted(Path(__file__).resolve().parents[1].glob('src/f1_harness/*.py'))},
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (a.output/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    (a.output/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
    print(json.dumps(dict(output=str(a.output/'report.json'),natural_visual_failure=False,
        scope=conditioned.geometry_scope,fixture=conditioned.fixture is not None,
        gripper_measured=conditioned.state_valid_mask[15],gripper=conditioned.state16[15])))


if __name__=='__main__':main()

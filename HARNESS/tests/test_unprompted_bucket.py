"""Current collision-body measurement before grasp and during held transport."""
from types import SimpleNamespace
import cv2
import numpy as np
import pytest

from f1_harness.fixture_tracking import locate_unprompted_bucket


def image_and_reference(two_buckets=False):
    depth=np.ones((210,420),np.float32)
    rgb=np.full((*depth.shape,3),210,np.uint8)
    polygons={
        'bucket-triangle':[[103,54],[87,78],[119,78]],
        'bucket-left-slot':[[45,81],[66,81],[66,109],[45,109]],
        'bucket-u-slot':[[90,125],[101,125],[101,139],[111,139],[111,125],[122,125],[122,154],[90,154]],
    }
    anchors={}
    for name,vertices in polygons.items():
        outline=np.array(vertices,np.int32)
        cv2.fillPoly(rgb,[outline],(0,0,0))
        mask=np.zeros(depth.shape,np.uint8)
        cv2.fillPoly(mask,[outline],1)
        ring=cv2.dilate(mask,np.ones((17,17),np.uint8))>cv2.dilate(mask,np.ones((7,7),np.uint8))
        y,x=np.nonzero(ring)
        anchors[name]=dict(roi_center_px=[x.mean(),y.mean()],depth_m=1.)
    if two_buckets:rgb[:,210:]=rgb[:,:210]
    cfg=dict(k_rgb=[[500.,0,210.],[0,500.,105.],[0,0,1]],base_camera=np.eye(4).tolist(),roi=[0,0,420,210])
    return rgb,depth,cfg,np.eye(4),np.eye(4),anchors


def test_shape_registration_only_measures_bucket():
    args=image_and_reference()
    pose,_,details=locate_unprompted_bucket(*args,identity_tolerance_m=.012)
    assert np.linalg.norm(pose[:3,3])<.01
    assert details['aperture_measured'] is False and details['anchor_count']==3
    assert details['landmark_source']=='current_head_contours'


@pytest.mark.parametrize('defect',['no_depth','missing_notch','different_layout','two_buckets'])
def test_missing_or_ambiguous_current_landmarks_do_not_create_collision_geometry(defect):
    rgb,depth,cfg,camera,reference,anchors=image_and_reference(defect=='two_buckets')
    if defect=='no_depth':depth[:]=0
    if defect=='missing_notch':rgb[117:160,80:135]=210
    if defect=='different_layout':anchors['bucket-u-slot']['roi_center_px'][0]+=60
    with pytest.raises(ValueError,match='ambiguous_bucket' if defect=='two_buckets' else 'registration_unavailable'):
        locate_unprompted_bucket(rgb,depth,cfg,camera,reference,anchors,identity_tolerance_m=.012)


@pytest.mark.parametrize('gripper,defect',[(0,None),(90,None),(90,'wrong_observation'),
                                         (90,'wrong_cache'),(90,'partial_prompt'),(90,'missing_notch')])
def test_current_rgbd_can_supply_only_body_independent_of_fingers(gripper,defect):
    from f1_harness.scene_pipeline import CalibratedScenePipeline
    from test_scene_pipeline import fixture
    rgb,depth,head,camera,reference,anchors=image_and_reference()
    old,observation=fixture()
    try:
        observation=observation.model_copy(update=dict(state16=(*observation.state16[:-1],float(gripper))))
        evidence=tuple(f.blob_sha256 for f in observation.frames)
        masks={} if defect!='partial_prompt' else {'bucket-left-slot':depth>0}
        cfg=dict(hole_camera=dict(head,camera='head'),base_hole=reference,planning_margin_m=.012,
                 measured_head_pose=True,hole_id='square',native_frame='world')
        cache_evidence=('older-rgb',evidence[1]) if defect=='wrong_cache' else evidence
        pipe=SimpleNamespace(config=cfg,fixture_reference_camera=camera,live_hole=None,last_timing={},
            occlusion=dict(fixture_id='bucket',anchors=anchors),secondary_cache=(None,(depth,cache_evidence,masks),rgb))
        if defect=='missing_notch':rgb[117:160,80:135]=210
        if defect=='wrong_observation':
            observation=observation.model_copy(update=dict(frames=tuple(
                f.model_copy(update=dict(blob_sha256='0'*64)) if f.stream=='rgb' else f for f in observation.frames)))
        measure=lambda:CalibratedScenePipeline._locate_bucket_for_object(pipe,observation,'right',(depth,evidence,masks))
        if defect is None:
            body=measure()
            assert body.evidence_ids==evidence and body.measured_at_ns==observation.received_ns
            assert pipe.last_timing['independent_fixture']['aperture_measured'] is False
        else:
            expected={'wrong_observation':'frame_identity_changed','wrong_cache':'frame_identity_changed',
                      'partial_prompt':'anchor_missing','missing_notch':'registration_unavailable'}
            with pytest.raises(ValueError,match=expected[defect]):measure()
    finally:old.close()

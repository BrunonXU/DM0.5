import hashlib
import numpy as np
import pytest
from f1_harness.contracts import Frame,Observation,Pairing
from f1_harness.scene_pipeline import CalibratedScenePipeline


def fixture():
    # Analytic pixel/metric fixture; this is a software test, never a saved real observation.
    k=[[20.,0,10],[0,20.,10],[0,0,1.]]
    hole=np.eye(4);hole[2,3]=1.1
    flip=np.diag([1.,-1.,-1.,1.])
    cfg=dict(calibration_id='test-cal',base_camera=np.eye(4).tolist(),k_rgb=k,d_rgb=[0.]*5,
        k_depth=k,d_depth=[0.]*5,rgb_depth=np.eye(4).tolist(),rgb_info_sha256='b'*64,
        depth_info_sha256='c'*64,depth_scale_m=.001,object_id='block',hole_id='hole',
        base_hole=hole.tolist(),face_object=flip.tolist(),native_frame='test-base',uncertainty_m=.001,
        hole_clearance_m=.01,insertion_depth_m=.02,xy_tolerance_m=.01,yaw_tolerance_rad=.1,
        tilt_tolerance_rad=.1,stationary_window_ns=100_000_000,stationary_delta_deg=.1,
        object_stationary_m=.001,gripper_open_range=[0,5],hole_reprojection_px=2,camera='head',roi=[0,0,20,20])
    blobs={};frames=[]
    for stream,data,encoding,info in [('rgb',np.zeros((20,20,3),np.uint8).tobytes(),'bgr8','b'*64),
                                     ('depth',np.full((20,20),1000,np.uint16).tobytes(),'16UC1','c'*64)]:
        sha=hashlib.sha256(data).hexdigest();blobs[sha]=data
        frames.append(Frame(camera='head',stream=stream,frame_id='optical',sequence=1,source_ns=1,
            received_ns=1,clock_domain='test-clock',width=20,height=20,encoding=encoding,
            blob_sha256=sha,camera_info_sha256=info,depth_scale_m=.001 if stream=='depth' else None))
    mask=np.zeros((20,20),bool);mask[8:13,8:13]=True
    hole_mask=np.zeros((20,20),bool);hole_mask[6:15,6:15]=True;hole_mask[7:14,7:14]=False
    pipeline=CalibratedScenePipeline(cfg,lambda rgb:{'block':mask,'hole':hole_mask},blobs.__getitem__)
    obs=Observation(observation_id='test',acquisition_instance='test',received_ns=1,clock_domain='test',
        frames=tuple(frames),pairings=(Pairing(camera='head',quality='source_time_bounded',rgb_sequence=1,depth_sequence=1,skew_ns=0),),
        state16=(0.,)*16,state_valid_mask=(True,)*16,state_received_ns=1)
    return pipeline,obs


def test_metric_scene_requires_temporal_release_evidence():
    pipeline,obs=fixture()
    first=pipeline.process(obs,base_eef=np.eye(4))
    assert first.objects[0].held is None and not first.stationary
    for index in (1,2):
        stamp=1+index*100_000_000
        frames=tuple(f.model_copy(update={'source_ns':stamp,'received_ns':stamp,'sequence':index+1}) for f in obs.frames)
        pair=obs.pairings[0].model_copy(update={'rgb_sequence':index+1,'depth_sequence':index+1})
        value=pipeline.process(obs.model_copy(update={'received_ns':stamp,'state_received_ns':stamp,
                                                      'frames':frames,'pairings':(pair,)}),base_eef=np.eye(4))
    assert value.stationary
    assert value.objects[0].held is False  # F1 zero-open direction is not inverted.
    assert value.objects[0].inserted and value.objects[0].inserted_hole_id=='hole'


def test_camera_change_invalidates_scene():
    pipeline,obs=fixture()
    frames=list(obs.frames);frames[0]=frames[0].model_copy(update={'camera_info_sha256':'d'*64})
    with pytest.raises(ValueError,match='identity_changed'):
        pipeline.process(obs.model_copy(update={'frames':tuple(frames)}),base_eef=np.eye(4))


def test_repeated_pixels_cannot_be_fresh_temporal_evidence():
    pipeline,obs=fixture()
    pipeline.process(obs,base_eef=np.eye(4))
    with pytest.raises(ValueError,match='new_consistent_source_frames'):
        pipeline.process(obs.model_copy(update={'received_ns':999999999}),base_eef=np.eye(4))

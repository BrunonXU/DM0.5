import numpy as np
import pytest
from f1_harness.fixture_tracking import locate_hole
from f1_harness.conditions import observation_condition


def fixture():
    mask=np.zeros((120,120),bool);mask[45:76,45:76]=True
    depth=np.ones(mask.shape)
    anchors={};masks={}
    for name,(x,y) in zip(('a','b','c'),((25,25),(90,25),(25,90))):
        region=np.zeros_like(mask);region[y-3:y+4,x-3:x+4]=True;masks[name]=region
        anchors[name]=dict(roi_center_px=[x,y],depth_m=1.)
    config=dict(k_rgb=[[100.,0,60],[0,100.,60],[0,0,1]],roi=[0,0,120,120],base_camera=np.eye(4).tolist())
    reference=np.eye(4);reference[2,3]=1
    return mask,depth,masks,config,reference,anchors


def test_current_metric_translation_and_plane_are_measured():
    args=list(fixture());args[0]=np.roll(args[0],5,axis=1)
    args[2]={k:np.roll(v,5,axis=1) for k,v in args[2].items()}
    pose,anchors,details=locate_hole(*args,identity_tolerance_m=.015)
    assert np.allclose(pose[:3,3],[.05,0,1])
    assert np.allclose(pose[:3,2],[0,0,1])
    assert details['anchor_distance_error_m']<1e-10
    assert anchors['a']['roi_center_px']==[30,25]


def test_missing_depth_and_inconsistent_fixture_cannot_update_pose():
    args=list(fixture());args[1][:]=0
    with pytest.raises(ValueError,match='depth_missing'):locate_hole(*args,identity_tolerance_m=.015)
    args=list(fixture());args[2]['a']=np.roll(args[2]['a'],10,axis=0)
    with pytest.raises(ValueError,match='anchor_identity_changed'):locate_hole(*args,identity_tolerance_m=.015)


def test_transient_detection_and_identity_fault_have_different_recovery():
    assert observation_condition('fixture_relocalization_anchor_missing:a')['state']=='waiting'
    assert observation_condition('fixture_relocalization_anchor_identity_changed')['state']=='needs_attention'


def test_live_fixture_is_cleared_with_observation_scope():
    from test_full_development_scene import full_scene
    pipeline,_=full_scene()
    try:
        for reset in (pipeline.invalidate,pipeline.invalidate_secondary):
            pipeline.live_hole=np.eye(4);pipeline.live_fixture_anchors={'a':{}}
            reset()
            assert pipeline.live_hole is None and pipeline.live_fixture_anchors is None
    finally:pipeline.close()


def test_bucket_moves_rigidly_but_table_stays_in_world():
    from dataclasses import asdict
    from test_full_development_skills import scene,pose
    from f1_harness.contracts import Task,Observation,SceneObject
    from f1_harness.skill_services import PlanService
    from f1_harness.frames import FrameChain
    value=scene();service=object.__new__(PlanService)
    service.task=Task(task_id='t',object_id='block',hole_id='hole',calibration_id='cal',admission_profile='development_coarse')
    service.frames=FrameChain(pose(.4),pose(),'native','world')
    bucket=dict(role='bucket',min=[-.1]*3,max=[.1]*3,pose=pose(.4,.1,.2).tolist())
    table=dict(role='table',min=[-1]*3,max=[0]*3,pose=pose().tolist())
    service.fixture=asdict(value)|dict(native_frame='native',fixture_reference_hole=value.base_hole.tolist(),obstacles=[bucket,table])
    moved=value.base_hole.copy();moved[:3,3]+=[.03,0,.01]
    obs=Observation(observation_id='o',acquisition_instance='a',received_ns=100,clock_domain='clock',
        objects=(SceneObject(object_id='block',shape='square',pose=tuple(value.base_object.ravel()),frame_id='native',held=True),
                 SceneObject(object_id='hole',shape='square',pose=tuple(moved.ravel()),frame_id='native')))
    result=service._scene(obs,[0.]*22,[0.]*16,base_eef=pose())
    assert np.allclose(np.asarray(result.obstacles[0]['pose'])[:3,3],[.83,.1,.21])
    assert np.allclose(np.asarray(result.obstacles[1]['pose'])[:3,3],[.4,0,0])


def test_fixture_group_reacquires_and_ring_search_boxes_do_not_grow():
    from f1_harness.mask_service import ConfiguredMaskProvider
    cfg=dict(backend='fixed_color',prompts={'hole':[40,40,60,60],'anchor':[70,40,90,60]},
        image_shape=[120,120,3],minimum_score=.9,minimum_area_px=10,prompt_padding_px=1,
        fixed_color_tracking_padding_px=20,maximum_overlap_fraction=.1,
        fixture_tracking_group={'primary':'hole','members':['hole','anchor']},
        reacquisition_boxes={'hole':[40,20,60,40]},mask_regions={'anchor':{'kind':'outer_ring','inner_px':1,'outer_px':4}},
        color_profiles={k:dict(hsv_lower=[0,200,200],hsv_upper=[5,255,255],minimum_area_px=20,maximum_area_px=100) for k in ('hole','anchor')})
    pixels=np.zeros((120,120,3),np.uint8);pixels[26:35,46:55]=[255,0,0];pixels[26:35,76:85]=[255,0,0]
    provider=ConfiguredMaskProvider(cfg,fixed_only=True)
    for _ in range(5):
        masks=provider(pixels)
        assert set(masks)=={'hole','anchor'}
        box=provider.states['default']['prompts']['anchor']
        assert box[2]-box[0]==20 and box[3]-box[1]==20

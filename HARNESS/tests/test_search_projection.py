import numpy as np
import pytest
from f1_harness.search_projection import object_search_box
from f1_harness.mask_service import ConfiguredMaskProvider


def test_projection_follows_camera_motion_and_roi_without_producing_a_pose():
    obj=np.eye(4);obj[2,3]=1
    camera=np.eye(4);camera[0,3]=.2
    config=dict(k_rgb=[[100,0,60],[0,100,60],[0,0,1]],roi=[10,20,120,120])
    box={'min':[-.05,-.05,-.05],'max':[.05,.05,.05]}
    hint=object_search_box(obj,camera,box,config,margin_m=.01)
    assert hint[0]<30<hint[2] and hint[1]<40<hint[3]
    assert hint[2]<50  # shifted left because the camera moved right
    behind=obj.copy();behind[2,3]=-1
    assert object_search_box(behind,camera,box,config,margin_m=.01) is None


def test_lost_tracking_box_can_be_reacquired_but_empty_or_ambiguous_hint_is_not_evidence():
    cfg=dict(backend='fixed_color',prompts={'block':[10,10,40,40]},image_shape=[100,100,3],
        minimum_score=.9,minimum_area_px=20,prompt_padding_px=3,maximum_overlap_fraction=.1,
        fixed_color_tracking_padding_px=10,color_profiles={'block':dict(hsv_lower=[0,200,200],
        hsv_upper=[5,255,255],minimum_area_px=20,maximum_area_px=200)})
    rgb=np.zeros((100,100,3),np.uint8);rgb[60:70,60:70]=[255,0,0]
    provider=ConfiguredMaskProvider(cfg,fixed_only=True)
    with pytest.raises(ValueError,match='mask_uncertain'):provider(rgb)
    masks=provider.with_search_boxes(rgb,view='default',boxes={'block':[50,50,90,90]})
    assert masks['block'].sum()==100
    assert provider(rgb)['block'].sum()==100
    with pytest.raises(ValueError,match='mask_uncertain'):
        provider.with_search_boxes(np.zeros_like(rgb),view='default',boxes={'block':[50,50,90,90]})
    provider=ConfiguredMaskProvider(cfg,fixed_only=True);rgb[77:85,77:85]=[255,0,0]
    with pytest.raises(ValueError,match='mask_uncertain'):
        provider.with_search_boxes(rgb,view='default',boxes={'block':[50,50,90,90]})


def test_search_hint_does_not_cross_authority_scope():
    from test_full_development_scene import full_scene
    pipeline,_=full_scene()
    try:
        context=dict(epoch=1,task_id='one',authorized=True)
        pipeline._context(context,1)
        pipeline.search_hint={'pose':np.eye(4),'received_ns':1}
        pipeline.invalidate()  # only a search hint survives rejected image evidence
        assert not pipeline.tracking_history and pipeline.in_hand_reference is None
        pipeline._context(context|{'epoch':2},2)
        assert pipeline.search_hint is None
    finally:pipeline.close()


def test_closed_fingers_project_candidate_attachment_but_do_not_create_held_evidence():
    from types import SimpleNamespace
    from test_full_development_scene import full_scene
    pipeline,_=full_scene()
    class Search:
        def with_search_boxes(self,*a,**kw):raise AssertionError('projection must not measure')
    pipeline.segment=Search()
    try:
        pipeline.config.update(object_id='block',gripper_open_range=[17.5,18.5],
            search_object_box={'min':[-.05,-.05,-.05],'max':[.05,.05,.05]},planning_margin_m=.01)
        obj=np.eye(4);obj[2,3]=1
        pipeline.search_hint=dict(pose=obj.copy(),eef_object=obj.copy(),received_ns=1)
        config=dict(k_rgb=[[100,0,60],[0,100,60],[0,0,1]],roi=[0,0,120,120])
        moved=np.eye(4);moved[0,3]=.3
        closed=SimpleNamespace(received_ns=10_000_000_000,state16=[0.]*15+[100.],state_valid_mask=[True]*16)
        carried=pipeline._object_search_hints(closed,config,moved,enabled=True,base_eef=moved)['block']
        opened=SimpleNamespace(**vars(closed));opened.state16=[0.]*15+[18.]
        stationary=pipeline._object_search_hints(opened,config,moved,enabled=True,base_eef=moved)['block']
        assert carried[0]<60<carried[2] and stationary[2]<carried[0]
        assert pipeline.in_hand_reference is None and not pipeline.tracking_history
        assert np.array_equal(pipeline.search_hint['pose'],obj)
    finally:pipeline.close()

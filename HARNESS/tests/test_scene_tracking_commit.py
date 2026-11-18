"""Geometry rejection must not commit a misleading pixel tracking origin."""
from copy import deepcopy
import hashlib

import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.scene_pipeline import CalibratedScenePipeline,SecondaryCameraUnavailable
from test_scene_pipeline import fixture


def moving_fixture(padding=1):
    original,observation=fixture();blobs={}
    config=dict(backend='fixed_color',prompts={'block':[3,4,17,16],'hole':[4,4,17,17]},
        image_shape=[20,20,3],minimum_score=.9,minimum_area_px=4,prompt_padding_px=1,
        fixed_color_tracking_padding_px=padding,maximum_overlap_fraction=.1,
        color_profiles=dict(block=dict(hsv_lower=[0,200,200],hsv_upper=[5,255,255],minimum_area_px=4,maximum_area_px=100),
            hole=dict(hsv_lower=[55,200,200],hsv_upper=[65,255,255],minimum_area_px=4,maximum_area_px=100)))
    provider=ConfiguredMaskProvider(config,fixed_only=True)
    pipeline=CalibratedScenePipeline(original.config,provider,blobs.__getitem__)
    def sample(index,*,x,depth=True):
        rgb=np.zeros((20,20,3),np.uint8)
        rgb[6:15,6:15]=[0,255,0];rgb[7:14,7:14]=0;rgb[8:13,x:x+5]=[255,0,0]
        frames=[]
        stamp=1+index*100_000_000
        for f in observation.frames:
            data=rgb.tobytes() if f.stream=='rgb' else np.full((20,20),1000 if depth else 0,np.uint16).tobytes()
            sha=hashlib.sha256(data).hexdigest();blobs[sha]=data
            frames.append(f.model_copy(update=dict(blob_sha256=sha,encoding='rgb8' if f.stream=='rgb' else f.encoding,
                sequence=index+1,received_ns=stamp,source_ns=stamp)))
        pair=observation.pairings[0].model_copy(update=dict(rgb_sequence=index+1,depth_sequence=index+1))
        return observation.model_copy(update=dict(observation_id='fixture:'+str(index),frames=tuple(frames),
            pairings=(pair,),received_ns=stamp,state_received_ns=stamp))
    return pipeline,provider,sample


def test_depth_rejection_does_not_move_search_and_next_good_frame_recovers():
    pipeline,provider,sample=moving_fixture()
    before=provider.tracking_snapshot()
    with pytest.raises(ValueError):pipeline.process(sample(0,x=4,depth=False),base_eef=np.eye(4))
    assert provider.tracking_snapshot()==before and provider.pending_tracking is None
    scene=pipeline.process(sample(1,x=11),base_eef=np.eye(4))
    assert scene.objects[0].visible and not scene.stationary and scene.objects[0].held is None
    assert provider.tracking_snapshot()!=before


def test_old_eager_commit_reproduces_persistent_mask_loss():
    pipeline,provider,sample=moving_fixture()
    # Model exactly the prior interface: mask calls mutate immediately, with no
    # enclosing geometry transaction. It remains a local analytic counterexample.
    pipeline.segment=lambda rgb:provider(rgb)
    before=provider.tracking_snapshot()
    with pytest.raises(ValueError):pipeline.process(sample(0,x=4,depth=False),base_eef=np.eye(4))
    assert provider.tracking_snapshot()!=before
    with pytest.raises(ValueError,match='mask_uncertain.*block'):
        pipeline.process(sample(1,x=11),base_eef=np.eye(4))


def test_repeated_geometric_failure_never_narrows_next_search():
    pipeline,provider,sample=moving_fixture();before=provider.tracking_snapshot()
    for index in range(3):
        with pytest.raises(ValueError):pipeline.process(sample(index,x=4,depth=False),base_eef=np.eye(4))
        assert provider.tracking_snapshot()==before
    assert pipeline.process(sample(3,x=11),base_eef=np.eye(4)).objects[0].visible


def test_one_pixel_padding_keeps_exclusive_crop_border_on_both_sides():
    pipeline,provider,sample=moving_fixture()
    for index in range(4):
        scene=pipeline.process(sample(index,x=11),base_eef=np.eye(4))
        assert scene.objects[0].visible
        assert provider.states['default']['prompts']['block']==[10,7,17,14]
    assert scene.stationary and scene.objects[0].held is False


def test_late_secondary_failure_only_commits_valid_primary_view():
    _,base,sample=moving_fixture()
    provider=ConfiguredMaskProvider(dict(views={view:deepcopy(base.states['default']['config']) for view in ('right','head')},default_view='right'),fixed_only=True)
    pipeline,_,_=moving_fixture();pipeline.segment=provider;pipeline.config['camera']='right'
    before=provider.tracking_snapshot()
    def process(*a,**kw):
        # Real mask computation in both views, then a failed fixture geometry.
        rgb=np.zeros((20,20,3),np.uint8);rgb[6:15,6:15]=[0,255,0];rgb[7:14,7:14]=0;rgb[8:13,4:9]=[255,0,0]
        provider(rgb,view='right');provider(rgb,view='head')
        raise SecondaryCameraUnavailable('secondary_camera_pairing_unavailable')
    pipeline._process=process
    with pytest.raises(SecondaryCameraUnavailable):pipeline.process(sample(0,x=4),base_eef=np.eye(4))
    after=provider.tracking_snapshot()
    assert after['right']!=before['right'] and after['head']==before['head']


def test_discarded_primary_view_cannot_commit_when_fallback_view_succeeds():
    _,base,_=moving_fixture()
    provider=ConfiguredMaskProvider(dict(views={view:deepcopy(base.states['default']['config']) for view in ('right','head')},default_view='right'),fixed_only=True)
    before=provider.tracking_snapshot();token=provider.begin_tracking_frame()
    for view in ('right','head'):provider.pending_tracking[1][view]['prompts']['block']=[1,2,6,7]
    provider.discard_tracking_view('right')
    provider.finish_tracking_frame(token,accepted_views=None)
    assert provider.tracking_snapshot()['right']==before['right']
    assert provider.tracking_snapshot()['head']!=before['head']


def test_finish_waits_for_background_view_before_rollback():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    pipeline,provider,sample=moving_fixture();before=provider.tracking_snapshot()
    entered=threading.Event();release=threading.Event()
    def work():
        entered.set();assert release.wait(2)
        provider.pending_tracking[1]['default']['prompts']['block']=[1,2,6,7]
        pipeline.secondary_cache=('uncommitted-mask-state',None)
    with ThreadPoolExecutor(1) as executor:
        def process(*a,**kw):
            pipeline.secondary_future=executor.submit(work)
            assert entered.wait(1)
            threading.Timer(.02,release.set).start()
            raise ValueError('primary_geometry_rejected')
        pipeline._process=process
        with pytest.raises(ValueError,match='primary_geometry_rejected'):
            pipeline.process(sample(0,x=4),base_eef=np.eye(4))
    assert provider.pending_tracking is None and provider.tracking_snapshot()==before
    assert pipeline.secondary_cache is None

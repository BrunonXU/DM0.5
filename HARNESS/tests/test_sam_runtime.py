"""SAM owns target masks; fixture geometry and task authority stay separate."""
from copy import deepcopy
import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider
from test_color_prompt_recovery import config, image


def sam_config():
    cfg=config();cfg.update(backend='sam2_remote',sam_objects=['block'],color_profiles={},
                           require_explicit_target_prompt=False)
    return {'views':{'right':cfg}}


class Sam:
    def __init__(self):self.calls=[]
    def segment(self,rgb,*,box):
        self.calls.append(np.asarray(box).tolist())
        mask=np.zeros(rgb.shape[:2],bool);mask[25:35,25:35]=True
        return [(mask,.99)]


def test_sam_segment_is_used_even_when_color_target_is_absent():
    sam=Sam();provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':sam})
    masks=provider(np.zeros((80,80,3),np.uint8),view='right')
    assert masks['block'].sum()==100 and len(sam.calls)==1


def test_sam_error_never_falls_back_to_target_color_detection():
    class Down:
        def segment(self,*args,**kw):raise ValueError('sam_inference_failed:503')
    provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':Down()})
    with pytest.raises(ValueError,match='sam_inference_failed'):
        provider(image((25,25,35,35)),view='right')


def test_runtime_target_requires_current_prompt_not_old_color_search_region():
    cfg=sam_config();cfg['views']['right']['require_explicit_target_prompt']=True
    sam=Sam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    with pytest.raises(ValueError,match='requires_new_prompt'):
        provider(image((25,25,35,35)),view='right')
    assert not sam.calls
    provider.set_search_hint(view='right',object_id='block',box=[20,20,40,40])
    assert provider(image((25,25,35,35)),view='right')['block'].sum()==100


def test_visual_search_survives_geometry_rejection_without_accepting_geometry():
    sam=Sam();provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':sam})
    provider.set_search_hint(view='right',object_id='block',box=[10,10,40,40])
    before=deepcopy(provider.tracking_snapshot())
    token=provider.begin_tracking_frame()
    provider(np.zeros((80,80,3),np.uint8),view='right')
    provider.finish_tracking_frame(token,accepted_views=[])
    after=provider.tracking_snapshot()['right']
    assert after['prompts']==before['right']['prompts']
    assert after['visual_prompts']['block']==[17,17,43,43]
    assert after['visual_state_is_motion_evidence'] is False
    token=provider.begin_tracking_frame()
    provider(np.zeros((80,80,3),np.uint8),view='right')
    assert sam.calls[-1]==[17,17,43,43]
    provider.finish_tracking_frame(token,accepted_views=['right'])
    assert provider.tracking_snapshot()!=before


def test_fixture_color_masks_do_not_replace_sam_target():
    cfg=sam_config();view=cfg['views']['right']
    view['prompts']['hole']=[45,45,70,70]
    view['color_profiles']['hole']=config()['color_profiles']['block']
    sam=Sam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    masks=provider(image((50,50,60,60)),view='right')
    assert set(masks)=={'block','hole'} and masks['block'].sum()==100 and len(sam.calls)==1


def test_projected_low_score_mask_is_advisory_without_duplicate_inference():
    cfg=sam_config();cfg['views']['right'].update(require_explicit_target_prompt=True,
        optional_ids=['block'],reacquisition_boxes={'block':[20,20,40,40]})
    class LowScore(Sam):
        def segment(self,*args,**kwargs):
            mask,_=super().segment(*args,**kwargs)[0]
            return [(mask,.78)]
    sam=LowScore();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    assert provider.with_search_boxes(np.zeros((80,80,3),np.uint8),view='right',
        boxes={'block':[20,20,40,40]})['block'].sum()==100
    assert sam.calls==[[20.,20.,40.,40.]]
    assert provider.states['right']['sam_seeded']=={'block'}
    assert provider.diagnostic_snapshot()['right']['block']['quality_warning']=='mask_quality_below_reference'


def test_distinct_reacquisition_box_is_still_tried():
    cfg=sam_config();cfg['views']['right'].update(require_explicit_target_prompt=True,
        reacquisition_boxes={'block':[15,15,45,45]})
    class Recovered(Sam):
        def segment(self,*args,**kwargs):
            mask,_=super().segment(*args,**kwargs)[0]
            return [(np.zeros_like(mask) if len(self.calls)==1 else mask,.99)]
    sam=Recovered();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    assert provider.with_search_boxes(np.zeros((80,80,3),np.uint8),view='right',
        boxes={'block':[20,20,40,40]})['block'].sum()==100
    assert sam.calls==[[20.,20.,40.,40.],[15.,15.,45.,45.]]


def test_independent_views_infer_concurrently_and_reject_together():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    cfg=sam_config();cfg['views']['head']=deepcopy(cfg['views']['right'])
    release=Event()
    class BlockingSam(Sam):
        def __init__(self):super().__init__();self.entered=Event()
        def segment(self,*a,**kw):
            self.entered.set()
            assert release.wait(2), 'view was serialized behind another view'
            return super().segment(*a,**kw)
    workers={view:BlockingSam() for view in cfg['views']}
    provider=ConfiguredMaskProvider(cfg,segmenter=workers)
    before=deepcopy(provider.tracking_snapshot());token=provider.begin_tracking_frame()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=[pool.submit(provider,np.zeros((80,80,3),np.uint8),view=view) for view in workers]
        try:assert all(worker.entered.wait(1) for worker in workers.values())
        finally:release.set()
        assert all(job.result()['block'].sum()==100 for job in jobs)
    provider.finish_tracking_frame(token,accepted_views=[])
    for view in workers:
        assert provider.tracking_snapshot()[view]['prompts']==before[view]['prompts']
        assert provider.tracking_snapshot()[view]['visual_prompts']['block']==[17,17,43,43]


def test_transaction_commit_waits_for_inflight_view():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered=Event();release=Event();finishing=Event()
    class BlockingSam(Sam):
        def segment(self,*a,**kw):
            entered.set();assert release.wait(2)
            return super().segment(*a,**kw)
    provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':BlockingSam()})
    token=provider.begin_tracking_frame()
    def finish():
        finishing.set();provider.finish_tracking_frame(token,accepted_views=['right'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        job=pool.submit(provider,np.zeros((80,80,3),np.uint8),view='right')
        try:
            assert entered.wait(1)
            commit=pool.submit(finish);assert finishing.wait(1)
            assert not commit.done()
        finally:release.set()
        job.result();commit.result()
    assert provider.states['right']['sam_seeded']=={'block'}
    assert provider.states['right']['prompts']['block']==[17,17,43,43]


def test_streaming_binds_source_and_keeps_presence_separate_from_mask_quality():
    cfg=sam_config();cfg['views']['right']['sam_streaming']=True
    class Video(Sam):
        def __init__(self):super().__init__();self.requests=[]
        def track(self,rgb,**kw):
            self.requests.append(kw)
            mask,_=super().segment(rgb,box=kw['box'])[0]
            return (mask,.75),dict(score_kind='object_presence_not_mask_iou',
                source_ns=kw['source_ns'],motion_evidence=False)
    sam=Video();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    source=dict(source_ns=50,stream={'acquisition':'a','camera':'right','epoch':1})
    rgb=np.zeros((80,80,3),np.uint8)
    masks=provider.segment_frame(rgb,view='right',source=source)
    assert masks['block'].sum()==100  # .75 presence is not .75 mask IoU
    initial=sam.requests[-1]['stream_id']
    note=provider.diagnostic_snapshot()['right']['block']
    assert note['score_kind']=='object_presence_not_mask_iou'
    assert not note['motion_evidence']
    provider.segment_frame(rgb,view='right',source=dict(source,source_ns=51))
    assert sam.requests[-1]['stream_id']==initial
    provider.segment_frame(rgb,view='right',source=dict(source,stream={'acquisition':'b'}))
    assert sam.requests[-1]['stream_id']!=initial
    provider.set_search_hint(view='right',object_id='block',box=[15,15,45,45])
    provider.segment_frame(rgb,view='right',source=source)
    assert sam.requests[-1]['stream_id']!=initial
    hinted=sam.requests[-1]['stream_id']
    provider.reset_visual_tracking()
    provider.segment_frame(rgb,view='right',source=source)
    assert sam.requests[-1]['stream_id']!=hinted


def test_streaming_empty_mask_is_not_accepted_as_high_presence():
    cfg=sam_config();cfg['views']['right']['sam_streaming']=True
    class Empty(Sam):
        def track(self,rgb,**kw):
            return (np.zeros(rgb.shape[:2],bool),1.),{'score_kind':'object_presence_not_mask_iou'}
    provider=ConfiguredMaskProvider(cfg,segmenter={'right':Empty()})
    with pytest.raises(ValueError,match='mask_uncertain'):
        provider.segment_frame(np.zeros((80,80,3),np.uint8),view='right',
            source={'source_ns':50,'stream':{'camera':'right'}})


def test_large_collision_projection_does_not_replace_tight_visual_prompt():
    sam=Sam();provider=ConfiguredMaskProvider(sam_config(),segmenter={'right':sam})
    rgb=np.zeros((80,80,3),np.uint8)
    provider(rgb,view='right')
    provider.with_search_boxes(rgb,view='right',boxes={'block':[1,1,70,70]})
    assert sam.calls[-1]==[17,17,43,43]
    # A moved projection that no longer contains the old visible object uses
    # the predicted location instead of staying locked to the old pixel box.
    provider.with_search_boxes(rgb,view='right',boxes={'block':[45,45,75,75]})
    assert sam.calls[-1]==[45,45,75,75]


def test_scope_reset_inside_geometry_transaction_clears_old_prompt_authority():
    cfg=sam_config();cfg['views']['right']['require_explicit_target_prompt']=True
    sam=Sam();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    provider.set_search_hint(view='right',object_id='block',box=[20,20,40,40])
    token=provider.begin_tracking_frame()
    provider.reset_visual_tracking()
    with pytest.raises(ValueError,match='requires_new_prompt'):
        provider(np.zeros((80,80,3),np.uint8),view='right')
    provider.finish_tracking_frame(token,accepted_views=['right'])
    assert not provider.states['right']['sam_seeded'] and not sam.calls

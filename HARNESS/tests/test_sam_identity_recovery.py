"""A hand in the old block box must not acquire the block's identity."""
import numpy as np
import pytest

from f1_harness.mask_service import ConfiguredMaskProvider
from f1_harness.sam_service import track_frame
from test_sam_runtime import Sam, sam_config


class Semantic:
    prompts={'block':'wooden block'}
    def __init__(self):self.visible=True;self.calls=[]
    def seed(self,rgb,**kw):
        self.calls.append(kw)
        if not self.visible:
            return None,dict(seed_backend='sam3',seed_reason='semantic_scope_ambiguous_or_empty',
                             semantic_candidates=0)
        mask=np.zeros(rgb.shape[:2],bool);mask[25:35,25:35]=True
        return (mask,.4),dict(seed_backend='sam3',seed_reason='unique_in_current_hint')


class Video:
    def __init__(self):self.tracks={};self.calls=[];self.visible=True
    def track(self,rgb,**kw):
        self.calls.append(kw);key=(kw['stream_id'],kw['object_id'])
        old=self.tracks.get(key)
        mask=kw.get('seed_mask')
        if mask is None:mask=old['result']['mask']
        if not self.visible:mask=np.zeros_like(mask)
        result=dict(mask=mask,object_presence_score=.999,source_ns=kw['source_ns'],
            stream_id=kw['stream_id'],object_id=kw['object_id'],motion_evidence=False,
            score_kind='object_presence_not_mask_iou',duplicate_source=False)
        self.tracks[key]=dict(result=result,memory=old['memory'] if old and not kw['reset'] else object())
        return result


def test_occluded_semantic_seed_never_falls_back_to_hand_and_can_reacquire():
    rgb=np.zeros((80,80,3),np.uint8);semantic=Semantic();video=Video();sam=Sam()
    def frame(n):
        return track_frame(video,sam,semantic,rgb,box=[20,20,40,40],
                           stream_id='head',object_id='block',source_ns=n)
    (mask,score),initial=frame(1)
    assert mask.any() and initial['identity_status']=='confirmed'
    # Preserve the original upstream memory even though current pixels have no
    # target. A high-quality SAM2 rectangle of the hand is never requested.
    memory=video.tracks[('head','block')]['memory']
    semantic.visible=False;video.visible=False
    (mask,score),missing=frame(2)
    assert not mask.any() and missing['identity_status']=='unresolved'
    assert missing['tracking_memory_preserved'] and not missing['reseeded']
    assert video.tracks[('head','block')]['memory'] is memory and not sam.calls
    (mask,score),still_missing=frame(3)
    assert not mask.any() and still_missing['identity_status']=='unresolved'
    assert len(video.calls)==2 and not sam.calls
    semantic.visible=True;video.visible=True
    (mask,score),recovered=frame(4)
    assert mask.any() and recovered['identity_status']=='confirmed' and recovered['reseeded']
    assert video.calls[-1]['reset'] and not sam.calls


def test_explicit_point_seed_and_unconfigured_prompt_still_use_sam2():
    rgb=np.zeros((80,80,3),np.uint8);sam=Sam()
    class Unconfigured:
        prompts={}
        def seed(self,*a,**kw):
            return None,dict(seed_backend='sam2',seed_reason='explicit_points_or_unconfigured_entity')
    (mask,_),metadata=track_frame(Video(),sam,Unconfigured(),rgb,box=[20,20,40,40],
        stream_id='right',object_id='other',source_ns=2)
    assert mask.any() and metadata['identity_source']=='explicit_prompt' and sam.calls
    class Points(Unconfigured):prompts={'block':'wooden block'}
    class PointSam(Sam):
        def segment(self,rgb,*,box,points,labels):
            self.points=points;return super().segment(rgb,box=box)
    sam=PointSam()
    (mask,_),metadata=track_frame(Video(),sam,Points(),rgb,box=[20,20,40,40],
        stream_id='right',object_id='block',source_ns=3,
        point_args=dict(points=np.asarray([[30,30]]),labels=np.asarray([1])))
    assert mask.any() and sam.points.tolist()==[[30,30]] and metadata['identity_source']=='explicit_prompt'


def test_discovery_never_uses_a_box_only_fallback_for_unknown_semantics():
    sam=Sam();rgb=np.zeros((80,80,3),np.uint8)
    (mask,_),metadata=track_frame(Video(),sam,None,rgb,box=[0,0,80,80],
        stream_id='right',object_id='block',source_ns=1,discover=True)
    assert not mask.any() and not sam.calls
    assert metadata['seed_scope']=='full_image_semantics'
    assert metadata['seed_reason']=='semantic_entity_unconfigured'


def test_unseeded_current_frame_discovers_without_consuming_the_old_roi():
    cfg=sam_config();view=cfg['views']['right']
    view.update(require_explicit_target_prompt=True,sam_streaming=True)
    class Discover(Sam):
        def discover(self,rgb,**kw):
            self.discovery=kw
            mask,_=super().segment(rgb,box=[0,0,80,80])[0]
            return (mask,.4),dict(kw,identity_status='confirmed',identity_source='semantic',
                score_kind='object_presence_not_mask_iou',reseeded=True,seed_scope='full_image_semantics')
    sam=Discover();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    masks=provider.segment_frame(np.zeros((80,80,3),np.uint8),view='right',
        source=dict(source_ns=4,stream={'camera':'right'}))
    assert masks['block'].sum()==100 and sam.calls==[[0,0,80,80]]
    assert provider.states['right']['sam_seeded']=={'block'}
    note=provider.diagnostic_snapshot()['right']['block']
    assert note['tracking']['seed_provenance']['seed_scope']=='full_image_semantics'
    assert note['attempted_boxes']==[[0,0,80,80]]


def test_unresolved_mask_cannot_replace_previous_search_identity_even_if_nonempty():
    cfg=sam_config();cfg['views']['right']['sam_streaming']=True
    class Unresolved(Sam):
        def track(self,rgb,**kw):
            mask,_=super().segment(rgb,box=kw['box'])[0]
            return (mask,1.),dict(identity_status='unresolved',identity_source='semantic',
                stream_id=kw['stream_id'],source_ns=kw['source_ns'],
                score_kind='object_presence_not_mask_iou')
    provider=ConfiguredMaskProvider(cfg,segmenter={'right':Unresolved()})
    with pytest.raises(ValueError,match='mask_uncertain'):
        provider.segment_frame(np.zeros((80,80,3),np.uint8),view='right',
            source=dict(source_ns=4,stream={'camera':'right'}))
    assert provider.states['right']['visual_prompts']=={}


def test_reappearing_object_uses_semantic_seed_extent_after_partial_mask_shrank():
    cfg=sam_config();cfg['views']['right'].update(sam_streaming=True,
        sam_tracking_padding_px=0,minimum_area_px=1)
    class Partial(Sam):
        def track(self,rgb,**kw):
            self.calls.append(kw)
            mask=np.zeros(rgb.shape[:2],bool)
            metadata=dict(stream_id=kw['stream_id'],source_ns=kw['source_ns'],reseeded=False,
                score_kind='object_presence_not_mask_iou',identity_status='confirmed',
                seed_inference_count=0,tracking_inference_count=1)
            if kw['source_ns']==1 or kw['reseed']:
                mask[25:45,25:45]=True
                metadata.update(reseeded=True,seed_backend='sam3',semantic_selected_index=0,
                    semantic_candidate_summary=[dict(index=0,box=[25,25,45,45])],seed_inference_count=1)
            elif kw['source_ns']==2:mask[25:45,25:30]=True
            else:metadata.update(identity_status='unresolved',semantic_candidates=1,
                                 semantic_scoped_candidates=0,seed_inference_count=1)
            return (mask,1.),metadata
    sam=Partial();provider=ConfiguredMaskProvider(cfg,segmenter={'right':sam})
    for ns in [1,2,3]:
        result=provider.segment_frame(np.zeros((80,80,3),np.uint8),view='right',
            source=dict(source_ns=ns,stream={'camera':'right'}))
    assert result['block'].sum()==400
    assert np.array_equal(sam.calls[-2]['box'],[25,25,30,45])
    assert np.array_equal(sam.calls[-1]['box'],[25,25,45,45])
    note=provider.diagnostic_snapshot()['right']['block']['tracking']
    assert note['recovery_search_source']=='previous_semantic_seed_bounds'
    assert note['recovery_seed_source_ns']==1 and note['seed_inference_count']==2

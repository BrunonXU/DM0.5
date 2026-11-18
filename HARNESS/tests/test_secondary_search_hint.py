"""Reacquisition searches both calibrated views without reusing old poses."""
import numpy as np
import pytest

from test_full_development_scene import full_scene,sample
from test_scene_pipeline import fixture


def setup():
    pipeline,obs=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head',
        # 20px analytic camera at 1m: match its visible multi-pixel target.
        search_object_box={'min':[-.12,-.12,-.01],'max':[.12,.12,.01]},
        search_hint_window_ns=2_000_000_000)
    class Masks:
        lost=False
        calls=[]
        def __call__(self,rgb,view):
            if view=='right' and self.lost:
                raise ValueError('mask_uncertain_requires_new_prompt:object=block')
            all_masks=original.segment(rgb)
            return {'hole':all_masks['hole']} if view=='head' else {'block':all_masks['block']}
        def with_search_boxes(self,rgb,*,view,boxes):
            self.calls.append((view,boxes))
            return original.segment(rgb) if view=='head' else self(rgb,view)
    masks=Masks();pipeline.segment=masks
    return pipeline,obs,masks


def test_lost_primary_reacquires_from_current_secondary_depth():
    pipeline,obs,masks=setup()
    try:
        first=pipeline.process(sample(obs,0),base_eef=np.eye(4))
        masks.lost=True
        recovered=pipeline.process(sample(obs,1),base_eef=np.eye(4))
        assert recovered.geometry_camera=='head' and recovered.objects[0].visible
        assert recovered.objects[0].held is not True and not recovered.stationary
        assert any(v=='head' and 'block' in boxes for v,boxes in masks.calls)
        note=pipeline.last_timing['head_object_search_hint']
        assert note['measured_at_ns']==first.received_ns and not note['pose_used_as_evidence']
        assert recovered.objects[0].evidence_ids
    finally:pipeline.close()


def test_hint_not_used_after_scope_changes():
    pipeline,obs,masks=setup()
    try:
        context={'phase':'observe','epoch':1}
        pipeline.process(sample(obs,0),base_eef=np.eye(4),context=context)
        masks.lost=True
        next_obs=sample(obs,1)
        context['epoch']=2
        with pytest.raises(ValueError,match='mask_uncertain_requires_new_prompt:object=block'):
            pipeline.process(next_obs,base_eef=np.eye(4),context=context)
        assert pipeline.last_timing['object_geometry_attempts'][-1]['reason']=='object_mask_missing:camera=head:object=block'
        assert not any(v=='head' for v,_ in masks.calls)
    finally:pipeline.close()


@pytest.mark.parametrize('closed',[False,True])
def test_search_prediction_survives_gap_but_only_current_depth_supplies_pose(closed):
    pipeline,obs,masks=setup()
    try:
        context={'phase':'observe','epoch':1}
        first=pipeline.process(sample(obs,0),base_eef=np.eye(4),context=context)
        masks.lost=True
        current=sample(obs,30)
        if closed:
            context['phase']='grasp'
            state=list(current.state16);state[15]=60.
            current=current.model_copy(update={'state16':tuple(state)})
        recovered=pipeline.process(current,base_eef=np.eye(4),context=context)
        assert any(v=='head' for v,_ in masks.calls)
        assert recovered.objects[0].held is not True
        assert recovered.received_ns==current.received_ns>first.received_ns
        assert not pipeline.last_timing['head_object_search_hint']['pose_used_as_evidence']
    finally:pipeline.close()


def test_hint_cannot_bypass_bad_secondary_depth(monkeypatch):
    import f1_harness.scene_views as views
    pipeline,obs,masks=setup()
    try:
        pipeline.process(sample(obs,0),base_eef=np.eye(4));masks.lost=True
        original=views.rectified_view
        def no_depth(*a,**kw):
            rgb,depth,evidence=original(*a,**kw)
            return rgb,np.zeros_like(depth),evidence
        monkeypatch.setattr(views,'rectified_view',no_depth)
        with pytest.raises(ValueError,match='mask_uncertain_requires_new_prompt:object=block'):
            pipeline.process(sample(obs,1),base_eef=np.eye(4))
        assert pipeline.last_timing['object_geometry_attempts'][-1]['reason']=='insufficient valid depth'
        assert not pipeline.stationary_history and pipeline.in_hand_reference is None
    finally:pipeline.close()


def test_cached_secondary_pixels_bind_search_hint_but_do_not_refresh_frame():
    pipeline,obs,masks=setup();frame=sample(obs,0);cfg=pipeline.config['hole_camera']
    try:
        no_hint=pipeline._secondary_view(frame,cfg)
        assert 'block' not in no_hint[2]
        with_hint=pipeline._secondary_view(frame,cfg,search_boxes={'block':[4,4,16,16]})
        assert 'block' in with_hint[2] and not with_hint[3]['head_cache_hit']
        again=pipeline._secondary_view(frame,cfg,search_boxes={'block':[4,4,16,16]})
        assert again[3]['head_cache_hit'] and again[1]==with_hint[1]
        assert len(masks.calls)==1
        pipeline._secondary_view(frame,cfg,search_boxes={'block':[3,3,17,17]})
        assert len(masks.calls)==2
    finally:pipeline.close()


def test_reprojected_hint_cannot_make_repeated_head_frame_new_evidence():
    pipeline,obs,masks=setup()
    try:
        pipeline.process(sample(obs,0),base_eef=np.eye(4));masks.lost=True
        head_seen=sample(obs,1)
        pipeline.process(head_seen,base_eef=np.eye(4))
        later=sample(obs,2)
        later=later.model_copy(update=dict(
            frames=tuple(f for f in later.frames if f.camera=='right')+
                   tuple(f for f in head_seen.frames if f.camera=='head'),
            pairings=tuple(p for p in later.pairings if p.camera=='right')+
                     tuple(p for p in head_seen.pairings if p.camera=='head')))
        with pytest.raises(ValueError,match='new_consistent_source_frames_required:camera=head'):
            pipeline.process(later,base_eef=np.eye(4))
    finally:pipeline.close()

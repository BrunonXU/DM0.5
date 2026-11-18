import threading

import numpy as np
import pytest

from test_full_development_scene import full_scene, sample


def test_secondary_computation_overlaps_primary_without_sharing_scene_history():
    pipeline,obs=full_scene();segment=pipeline.segment
    head_entered=threading.Event();right_entered=threading.Event()
    def masks(rgb,view):
        if view=='head':
            head_entered.set();assert right_entered.wait(1)
        else:
            right_entered.set();assert head_entered.wait(1)
        return segment(rgb,view=view)
    pipeline.segment=masks
    try:
        result=pipeline.process(sample(obs,0),base_eef=np.eye(4))
        assert len(result.objects)==2 and len(pipeline.stationary_history)==1
        assert pipeline.secondary_future is None
    finally:pipeline.close()


def test_identical_secondary_frame_reuses_only_pixels_and_original_identity():
    pipeline,obs=full_scene();segment=pipeline.segment;counts={'head':0,'right':0}
    def masks(rgb,view):counts[view]+=1;return segment(rgb,view=view)
    pipeline.segment=masks
    first=sample(obs,0);second=sample(obs,1)
    second=second.model_copy(update=dict(frames=tuple(f for f in first.frames if f.camera=='head')+
        tuple(f for f in second.frames if f.camera=='right'),pairings=(first.pairings[0],second.pairings[1])))
    try:
        pipeline.process(first,base_eef=np.eye(4))
        result=pipeline.process(second,base_eef=np.eye(4))
        assert counts=={'head':1,'right':2} and pipeline.last_timing['head_cache_hit']
        assert [f.received_ns for f in result.frames if f.camera=='head']==[1,1]
        third=sample(obs,2)
        pipeline.process(third,base_eef=np.eye(4))
        assert counts=={'head':2,'right':3} and not pipeline.last_timing['head_cache_hit']
    finally:pipeline.close()


def test_primary_failure_still_joins_secondary_before_next_sample():
    pipeline,obs=full_scene();segment=pipeline.segment;completed=threading.Event()
    def masks(rgb,view):
        if view=='right':raise ValueError('primary rejected')
        value=segment(rgb,view=view);completed.set();return value
    pipeline.segment=masks
    try:
        with pytest.raises(ValueError,match='primary rejected'):
            pipeline.process(sample(obs,0),base_eef=np.eye(4))
        assert completed.is_set() and pipeline.secondary_future is None
    finally:pipeline.close()


@pytest.mark.parametrize('change',[{'quality':'invalid'},{'rgb_sequence':99}])
def test_cached_pixels_cannot_bypass_changed_pairing_identity(change):
    pipeline,obs=full_scene();first=sample(obs,0);second=sample(obs,1)
    second=second.model_copy(update=dict(frames=tuple(f for f in first.frames if f.camera=='head')+
        tuple(f for f in second.frames if f.camera=='right'),
        pairings=(first.pairings[0].model_copy(update=change),second.pairings[1])))
    try:
        pipeline.process(first,base_eef=np.eye(4))
        with pytest.raises(ValueError,match='secondary_camera'):
            pipeline.process(second,base_eef=np.eye(4))
    finally:pipeline.close()

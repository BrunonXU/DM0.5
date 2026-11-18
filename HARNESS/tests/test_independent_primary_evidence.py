"""Rejected fixture pixels do not erase independently measured wrist evidence."""
import numpy as np
import pytest

from f1_harness.contracts import SceneObject
from f1_harness.scene_pipeline import SecondaryCameraUnavailable
from test_full_development_scene import full_scene, sample
from test_phase_perception import context


def bad_head(obs):
    return obs.model_copy(update=dict(faults=('duplicate_source_stamp:head',),
        pairings=tuple(p.model_copy(update={'quality':'invalid'}) if p.camera=='head' else p for p in obs.pairings)))


def test_rejected_head_updates_fresh_wrist_history_but_returns_no_motion_scene():
    pipe,obs=full_scene()
    try:
        for i in range(3):
            limited=pipe.process(bad_head(sample(obs,i)),base_eef=np.eye(4),context=context())
            assert limited.geometry_scope=='coarse_object_verification' and len(limited.objects)==1
            assert len(pipe.stationary_history)==i+1
            assert pipe.last_received_ns==1+i*100_000_000
            assert pipe.last_stamps==(1+i*100_000_000,)*2
            assert pipe.hole_last_visible_ns is None
        recovered=pipe.process(sample(obs,3),base_eef=np.eye(4),context=context())
        assert recovered.stationary and not recovered.faults
    finally:pipe.close()


@pytest.mark.parametrize('case',['epoch','gap','same_capture_new_epoch'])
def test_primary_evidence_never_crosses_scope_or_acquisition_gap(case):
    pipe,obs=full_scene()
    try:
        for i in range(3):
            limited=pipe.process(bad_head(sample(obs,i)),base_eef=np.eye(4),context=context())
            assert limited.geometry_scope=='coarse_object_verification'
        if case=='same_capture_new_epoch':
            with pytest.raises(ValueError,match='new_consistent_source_frames'):
                pipe.process(sample(obs,2),base_eef=np.eye(4),context=context(epoch=2))
            assert not pipe.stationary_history and pipe.in_hand_reference is None
        else:
            # Even a snapshot labelled recent cannot hide the original 600ms
            # wrist acquisition gap; camera timestamps are the evidence clock.
            next_obs=sample(obs,8 if case=='gap' else 3)
            result=pipe.process(next_obs,base_eef=np.eye(4),context=context(epoch=2 if case=='epoch' else 1))
            if case=='gap':
                assert 'measurement_gap' in pipe.last_timing['quality_warnings']
                assert result.objects[0].held is not True
            else:assert not result.stationary and len(pipe.stationary_history)==1
    finally:pipe.close()


@pytest.mark.parametrize('also_duplicate',[False,True])
def test_head_calibration_change_is_not_a_transient_miss(also_duplicate):
    pipe,obs=full_scene()
    try:
        for i in range(3):pipe.process(sample(obs,i),base_eef=np.eye(4),context=context())
        broken=sample(obs,3)
        broken=broken.model_copy(update=dict(frames=tuple(f.model_copy(update={'camera_info_sha256':'f'*64})
            if f.camera=='head' else f for f in broken.frames)))
        if also_duplicate:broken=bad_head(broken)
        with pytest.raises(ValueError,match='secondary_camera_identity_changed') as error:
            pipe.process(broken,base_eef=np.eye(4),context=context())
        assert not isinstance(error.value,SecondaryCameraUnavailable)
        assert not pipe.stationary_history
    finally:pipe.close()


def test_bad_primary_also_clears_history_even_with_bad_head():
    pipe,obs=full_scene()
    try:
        for i in range(3):pipe.process(sample(obs,i),base_eef=np.eye(4),context=context())
        broken=bad_head(sample(obs,3)).model_copy(update={'faults':('duplicate_source_stamp:head','duplicate_source_stamp:right')})
        with pytest.raises(ValueError) as error:pipe.process(broken,base_eef=np.eye(4),context=context())
        assert not isinstance(error.value,SecondaryCameraUnavailable)
        assert not pipe.stationary_history
    finally:pipe.close()


def test_measured_lift_while_head_missing_can_establish_hold_for_later_full_scene(monkeypatch):
    pipe,obs=full_scene();pose=np.eye(4)
    def measured_object(**kwargs):
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
            frame_id='test-base',uncertainty_m=.001,evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',measured_object)
    pipe.config['face_object']=np.eye(4).tolist()
    try:
        # Coarse positional evidence uses the declared 15 mm uncertainty:
        # a 20 mm lift cannot independently distinguish attachment from noise.
        for i,z in enumerate([0.,.02,.04]):
            pose[2,3]=z;eef=np.eye(4);eef[2,3]=z
            value=sample(obs,i);state=list(value.state16);state[15]=50;state[7]=z*100
            value=value.model_copy(update={'state16':tuple(state)})
            partial=pipe.process(bad_head(value),base_eef=eef,context=context('grasp'))
            assert partial.geometry_scope=='coarse_object_verification'
        assert pipe.in_hand_reference is not None and len(pipe.tracking_history)==3
        value=sample(obs,3);state=list(value.state16);state[15]=50;state[7]=4
        result=pipe.process(value.model_copy(update={'state16':tuple(state)}),base_eef=eef,context=context('verify_grasp'))
        assert result.objects[0].held is True
        assert len(pipe.tracking_history)==4
    finally:pipe.close()


def test_missing_hole_mask_preserves_current_object_evidence():
    pipe,obs=full_scene();original=pipe._secondary_view
    def missing_hole(*args,**kwargs):
        depth,evidence,masks,timing=original(*args,**kwargs)
        return depth,evidence,{k:v for k,v in masks.items() if k!=pipe.config['hole_id']},timing
    pipe._secondary_view=missing_hole
    try:
        for i in range(3):
            value=sample(obs,i);state=list(value.state16);state[15]=50
            result=pipe.process(value.model_copy(update={'state16':tuple(state)}),base_eef=np.eye(4),context=context('verify_grasp'))
            assert result.geometry_scope=='coarse_object_verification'
            assert len(result.objects)==1 and result.objects[0].object_id==pipe.config['object_id']
            assert len(pipe.tracking_history)==i+1
        assert any('fixture_unavailable:' in w for w in result.quality_warnings)
    finally:pipe.close()

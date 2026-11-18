import numpy as np
import pytest
from f1_harness.observation import require_geometry
from test_full_development_scene import full_scene,sample


def prepared(monkeypatch):
    pipeline,obs=full_scene()
    pipeline.occlusion=dict(anchors={'fixture':{}},max_occlusion_ns=2_000_000_000)
    monkeypatch.setattr('f1_harness.fixture_tracking.locate_hole',
        lambda *args,**kwargs:(np.array(pipeline.config['base_hole']),{'fixture':{}},{}))
    pipeline._fixture_visible=lambda masks,depth:True
    old=pipeline.segment
    def segment(rgb,view):
        if view=='right':raise AssertionError('return must not require wrist target segmentation')
        return old(rgb,view)
    pipeline.segment=segment
    return pipeline,obs


def test_return_scene_contains_only_current_fixture_not_fabricated_dropped_object(monkeypatch):
    p,obs=prepared(monkeypatch)
    context=dict(authorized=True,placement_verified=True,phase='withdraw_reset',epoch=1)
    try:
        for i in range(3):result=p.process(sample(obs,i),base_eef=np.eye(4),context=context)
        assert result.stationary and result.geometry_scope=='coarse_fixture_return'
        assert [o.object_id for o in result.objects]==['hole']
        assert {f.camera for f in result.frames}=={'head'}
        require_geometry(result,now_ns=result.received_ns,max_age_ns=500_000_000,
                         goal='placed',admission_profile='development_coarse',skill='withdraw_reset')
        for skill in ('grasp','insert','transfer_above_hole','verify_placed'):
            with pytest.raises(ValueError,match='coarse_geometry'):
                require_geometry(result,now_ns=result.received_ns,max_age_ns=500_000_000,
                                 goal='placed',admission_profile='development_coarse',skill=skill)
        with pytest.raises(ValueError,match='new_return_frames'):
            p.process(sample(obs,2),base_eef=np.eye(4),context=context)
        assert not p.return_history
    finally:p.close()


def test_closed_gripper_or_lost_authority_cannot_enter_return_mode(monkeypatch):
    p,obs=prepared(monkeypatch)
    context=dict(authorized=True,placement_verified=True,phase='withdraw_reset',epoch=1)
    try:
        value=sample(obs,0);q=list(value.state16);q[15]=100.
        with pytest.raises(ValueError,match='gripper_not_open'):
            p.process(value.model_copy(update={'state16':tuple(q)}),base_eef=np.eye(4),context=context)
        with pytest.raises(ValueError,match='verified_placement'):
            p.process(value,base_eef=np.eye(4),context=context|{'authorized':False})
    finally:p.close()


def test_unused_wrist_sample_fault_does_not_block_return_but_head_or_identity_does(monkeypatch):
    p,obs=prepared(monkeypatch)
    context=dict(authorized=True,placement_verified=True,phase='withdraw_reset',epoch=1)
    try:
        for i in range(3):
            result=p.process(sample(obs,i).model_copy(update={'faults':('duplicate_source_stamp:right',)}),
                             base_eef=np.eye(4),context=context)
        assert result.stationary and not result.faults
        for fault in ('duplicate_source_stamp:head','camera_identity_changed:right'):
            with pytest.raises(ValueError,match='return_capture_faults'):
                p.process(sample(obs,4).model_copy(update={'faults':(fault,)}),base_eef=np.eye(4),context=context)
    finally:p.close()

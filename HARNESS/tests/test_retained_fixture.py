"""Static scene memory survives rejected samples, never a contradictory observation."""
import numpy as np
import pytest
from f1_harness.contracts import FixturePose
from f1_harness.observation import require_geometry
from test_full_development_scene import full_scene, sample
from test_independent_primary_evidence import bad_head
from test_phase_perception import context


def seed(pipe,obs):
    for i in range(3):pipe.process(sample(obs,i),base_eef=np.eye(4),context=context())
    value=FixturePose(fixture_id='bucket',pose=tuple(np.eye(4).ravel()),
        reference_pose=tuple(np.eye(4).ravel()),frame_id='test-base',
        evidence_ids=('original-head-rgb','original-head-depth'),measured_at_ns=200_000_001)
    pipe.measured_fixture=value
    return value


def test_rejected_head_keeps_original_world_measurement_not_aperture():
    pipe,obs=full_scene()
    try:
        original=seed(pipe,obs)
        scene=pipe.process(bad_head(sample(obs,3)),base_eef=np.eye(4),context=context())
        assert scene.geometry_scope=='coarse_static_task'
        assert scene.fixture.source=='retained_static_fixture'
        assert scene.fixture.pose==original.pose
        assert scene.fixture.measured_at_ns==original.measured_at_ns
        assert scene.fixture.evidence_ids==original.evidence_ids
        assert [o.object_id for o in scene.objects]==['block']
        require_geometry(scene,now_ns=scene.received_ns,max_age_ns=500_000_000,
                         goal='placed',admission_profile='development_coarse',skill='move_tcp')
        assert any('static_fixture_prediction' in w for w in scene.quality_warnings)
    finally:pipe.close()


@pytest.mark.parametrize('reset',['epoch','calibration','explicit'])
def test_scene_memory_does_not_cross_resets(reset):
    pipe,obs=full_scene()
    try:
        seed(pipe,obs)
        value=bad_head(sample(obs,3));ctx=context()
        if reset=='epoch':ctx=context(epoch=2)
        if reset=='calibration':
            value=value.model_copy(update={'frames':tuple(f.model_copy(update={'camera_info_sha256':'f'*64})
                if f.camera=='head' else f for f in value.frames)})
            with pytest.raises(ValueError):pipe.process(value,base_eef=np.eye(4),context=ctx)
            assert pipe.measured_fixture is None
            return
        if reset=='explicit':pipe.invalidate()
        scene=pipe.process(value,base_eef=np.eye(4),context=ctx)
        assert scene.fixture is None and scene.geometry_scope=='coarse_object_verification'
    finally:pipe.close()


def test_new_visual_failure_cannot_keep_old_fixture():
    pipe,obs=full_scene()
    original_view=pipe._secondary_view
    def missing(*args,**kwargs):
        depth,evidence,masks,timing=original_view(*args,**kwargs)
        return depth,evidence,{},timing
    try:
        seed(pipe,obs)
        pipe._secondary_view=missing
        value=pipe.process(sample(obs,3),base_eef=np.eye(4),context=context())
        assert value.fixture is None and pipe.measured_fixture is None
    finally:pipe.close()

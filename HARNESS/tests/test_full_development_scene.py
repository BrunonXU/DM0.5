"""Full coarse task uses two real-view contracts without claiming precision sync."""
import numpy as np
import pytest

from f1_harness.observation import require_geometry
from f1_harness.scene_pipeline import CalibratedScenePipeline
from test_scene_pipeline import fixture


def full_scene():
    original,obs=fixture()
    head=original.config.copy()
    config=original.config|dict(admission_profile='development_coarse',goal='placed',camera='right',
        base_camera=None,eef_camera=np.eye(4).tolist(),hole_camera=head,uncertainty_m=None,
        planning_margin_m=.015,tracking_tolerance_m=.005,gripper_stationary_delta=.5,
        stationary_window_ns=200_000_000,precision_certified=False)
    def masks(rgb,view):
        all_masks=original.segment(rgb)
        return {'block':all_masks['block']} if view=='right' else {'hole':all_masks['hole']}
    pipeline=CalibratedScenePipeline(config,masks,original.blob)
    return pipeline,obs


def sample(obs,index):
    stamp=1+index*100_000_000
    frames=tuple(f.model_copy(update=dict(camera=c,source_ns=stamp,received_ns=stamp,sequence=index+1))
                 for c in ('head','right') for f in obs.frames)
    pairs=tuple(obs.pairings[0].model_copy(update=dict(camera=c,quality='receipt_only',
                 rgb_sequence=index+1,depth_sequence=index+1)) for c in ('head','right'))
    return obs.model_copy(update=dict(received_ns=stamp,state_received_ns=stamp,frames=frames,pairings=pairs))


def test_two_view_full_development_keeps_sync_quality_and_full_task_scope():
    pipeline,obs=full_scene()
    for index in range(3):value=pipeline.process(sample(obs,index),base_eef=np.eye(4))
    assert value.stationary and len(value.objects)==2
    assert value.geometry_scope=='coarse_static_task'
    assert all(p.quality=='receipt_only' for p in value.pairings)
    assert all(o.uncertainty_m is None and o.planning_margin_m==.015 for o in value.objects)
    require_geometry(value,now_ns=value.received_ns,max_age_ns=500_000_000,
                     goal='placed',admission_profile='development_coarse')
    with pytest.raises(ValueError,match='coarse_geometry'):
        require_geometry(value,now_ns=value.received_ns,max_age_ns=500_000_000)


def test_missing_head_cannot_use_right_object_as_hole_evidence():
    pipeline,obs=full_scene();value=sample(obs,0)
    value=value.model_copy(update=dict(pairings=tuple(p for p in value.pairings if p.camera=='right')))
    with pytest.raises(ValueError,match='secondary_camera'):
        pipeline.process(value,base_eef=np.eye(4))


def test_current_fixture_with_missing_hole_exposes_reason_to_next_skill():
    from test_phase_perception import anchored_fixture
    registered,_,masks=anchored_fixture()
    pipeline,obs=full_scene()
    try:
        pipeline.fixture_reference_camera=np.eye(4)
        pipeline.occlusion=registered.occlusion
        pipeline.segment=lambda rgb,view: ({'block':masks['block']} if view=='right' else
            {key:masks[key] for key in registered.occlusion['anchors']})
        value=pipeline.process(sample(obs,0),base_eef=np.eye(4))
        assert value.fixture is not None and value.geometry_scope=='coarse_static_task'
        assert [obj.object_id for obj in value.objects]==['block']
        assert 'aperture_unavailable:aperture_not_directly_visible' in value.quality_warnings
    finally:
        pipeline.close()
        registered.close()


def test_full_task_static_evidence_includes_gripper_feedback():
    pipeline,obs=full_scene()
    for index in range(3):
        value=sample(obs,index);state=list(value.state16);state[15]=index
        result=pipeline.process(value.model_copy(update={'state16':tuple(state)}),base_eef=np.eye(4))
    assert not result.stationary


def test_lost_wrist_prompt_reacquires_current_head_geometry_without_fake_held_state():
    pipeline,obs=full_scene()
    original,_=fixture()
    def masks(rgb,view):
        if view=='right':raise ValueError('mask_uncertain_requires_new_prompt:object=block')
        return original.segment(rgb)
    pipeline.segment=masks
    pipeline.config['object_reacquisition_camera']='head'
    try:
        for index in range(3):value=pipeline.process(sample(obs,index),base_eef=np.eye(4))
        assert value.geometry_camera=='head' and value.stationary
        assert value.objects[0].held is not True
        assert pipeline.last_timing['object_reacquisition']['camera']=='head'
    finally:pipeline.close()


def test_reacquisition_cannot_mask_camera_identity_failure():
    pipeline,obs=full_scene();pipeline.config['object_reacquisition_camera']='head'
    value=sample(obs,0)
    value=value.model_copy(update=dict(frames=tuple(f.model_copy(update=dict(camera_info_sha256='f'*64))
         if f.camera=='right' else f for f in value.frames)))
    try:
        with pytest.raises(ValueError,match='camera_calibration_identity_changed'):
            pipeline.process(value,base_eef=np.eye(4))
    finally:pipeline.close()


@pytest.mark.parametrize('phase', ['observe', 'approach_grasp', 'grasp', 'inspect_in_hand',
    'transfer_above_hole','reobserve','regrasp','move_tcp','release_supported'])
def test_registered_pregrasp_view_follows_measured_open_gripper_not_requested_phase(phase):
    # Same physical state under different pending requests uses the same view.
    pipeline,obs=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head',pregrasp_object_camera='head')
    pipeline.segment=lambda rgb,view:original.segment(rgb)
    try:
        value=pipeline.process(sample(obs,0),base_eef=np.eye(4),context={'phase':phase})
        assert value.geometry_camera=='head'
        assert bool(pipeline.last_timing.get('right_pixel_work_skipped'))==(value.geometry_camera=='head')
    finally:pipeline.close()


def test_preferred_camera_duplicate_waits_for_new_evidence_instead_of_ending_task():
    from f1_harness.conditions import observation_condition
    pipeline,obs=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head',pregrasp_object_camera='head')
    pipeline.segment=lambda rgb,view:original.segment(rgb)
    first=sample(obs,0);later=sample(obs,1)
    later=later.model_copy(update={'frames':tuple(f if f.camera=='right' else next(old for old in first.frames if old.camera==f.camera and old.stream==f.stream) for f in later.frames)})
    later=later.model_copy(update={'pairings':tuple(p if p.camera=='right' else first.pairings[0] for p in later.pairings)})
    try:
        pipeline.process(first,base_eef=np.eye(4),context={'phase':'observe'})
        with pytest.raises(ValueError) as error:pipeline.process(later,base_eef=np.eye(4),context={'phase':'observe'})
        assert observation_condition(str(error.value))['state']=='waiting'
        value=pipeline.process(sample(obs,2),base_eef=np.eye(4),context={'phase':'observe'})
        assert value.geometry_camera=='head'
    finally:pipeline.close()


def test_grasp_camera_prior_ends_at_measured_closure_not_submission():
    pipeline,obs=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head',pregrasp_object_camera='head')
    pipeline.segment=lambda rgb,view:original.segment(rgb)
    try:
        value=pipeline.process(sample(obs,0),base_eef=np.eye(4),context={'phase':'grasp'})
        assert value.geometry_camera=='head'
        closed=sample(obs,1);state=list(closed.state16);state[15]=60.
        value=pipeline.process(closed.model_copy(update={'state16':tuple(state)}),base_eef=np.eye(4),context={'phase':'grasp'})
        assert value.geometry_camera=='right'
        assert value.objects[0].held is not True
    finally:pipeline.close()


def test_registered_wrist_pregrasp_preserves_head_fixture_and_ends_prior_at_closure(monkeypatch):
    import f1_harness.scene_pipeline as module
    pipeline,obs=full_scene();original,_=fixture()
    cfg=pipeline.config|dict(object_reacquisition_camera='head',pregrasp_object_camera='right',
        pregrasp_surface_selection={'pose_normal_prior':'registered_upright'})
    pipeline.close()
    pipeline=CalibratedScenePipeline(cfg,lambda rgb,view:original.segment(rgb),original.blob)
    measured=module.object_from_mask;surfaces=[]
    def record(**kwargs):
        surfaces.append(kwargs.pop('surface_selection',None))
        return measured(**kwargs)
    monkeypatch.setattr(module,'object_from_mask',record)
    try:
        value=pipeline.process(sample(obs,0),base_eef=np.eye(4),context={'phase':'observe'})
        assert value.geometry_camera=='right' and len(value.objects)==2
        assert surfaces[-1]['pose_normal_prior']=='registered_upright'
        assert not pipeline.last_timing.get('right_pixel_work_skipped')
        closed=sample(obs,1);state=list(closed.state16);state[15]=60.
        value=pipeline.process(closed.model_copy(update={'state16':tuple(state)}),base_eef=np.eye(4),context={'phase':'grasp'})
        assert value.geometry_camera=='right' and surfaces[-1] is None
        assert value.objects[0].held is not True
    finally:pipeline.close()


def test_unregistered_pregrasp_view_is_not_allowed():
    pipeline,_=full_scene()
    try:
        with pytest.raises(ValueError,match='pregrasp_view_requires_registered_coarse_camera'):
            CalibratedScenePipeline(pipeline.config|dict(pregrasp_object_camera='left'),pipeline.segment,pipeline.blob)
    finally:pipeline.close()


@pytest.mark.parametrize('reason',['surface is not a reliable planar block face',
    'insufficient_upright_face_support','mask_view=right:mask_uncertain_requires_new_prompt:object=block'])
def test_rejected_quality_sample_waits_but_identity_fault_still_stops(reason):
    from f1_harness.conditions import observation_condition
    assert observation_condition(reason)['state']=='waiting'
    assert observation_condition('camera_calibration_identity_changed')['state']=='needs_attention'


def test_coarse_scene_uses_explicit_depth_band_without_altering_default(monkeypatch):
    import f1_harness.scene_pipeline as module
    original=module.insertion_depth_reached;calls=[]
    def check(z,depth,**kw):
        calls.append((z,depth,kw));return original(z,depth,**kw)
    monkeypatch.setattr(module,'insertion_depth_reached',check)
    pipeline,obs=full_scene();pipeline.config['insertion_depth_tolerance_m']=.002
    pipeline.process(sample(obs,0),base_eef=np.eye(4))
    assert calls and calls[-1][2]==dict(tolerance_m=.002)
    strict,raw=fixture();strict.config['insertion_depth_tolerance_m']=.002
    strict.process(raw,base_eef=np.eye(4))
    assert calls[-1][2]==dict(tolerance_m=0.)

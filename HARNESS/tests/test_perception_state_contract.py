"""Physical measurement contracts across requests, view changes and occlusion.

RGB-D, masks and feedback are analytic fixtures; the pipeline and verification
are the production implementations. These tests publish no robot commands.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from f1_harness.contracts import SKILLS
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.verification import verify
from test_full_development_scene import full_scene, sample
from test_phase_perception import anchored_fixture, context
from test_scene_pipeline import fixture


def closed(raw,index,gripper=50.):
    obs=sample(raw,index);state=list(obs.state16);state[15]=gripper
    return obs.model_copy(update={'state16':tuple(state)})


@pytest.mark.parametrize('gripper,expected_prior',[(0.,True),(17.5,True),(18.5,True),(100.,False)])
def test_identical_physical_state_uses_identical_estimator_for_all_pending_skills(monkeypatch,gripper,expected_prior):
    import f1_harness.scene_pipeline as module
    pipeline,raw=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head',pregrasp_object_camera='right',
        pregrasp_surface_selection={'pose_normal_prior':'registered_upright'},gripper_open_range=[17.5,18.5])
    pipeline.segment=lambda rgb,view:original.segment(rgb)
    measure=module.object_from_mask;surfaces=[];poses=[]
    def capture(**kwargs):
        surfaces.append(kwargs.pop('surface_selection',None))
        return measure(**kwargs)
    monkeypatch.setattr(module,'object_from_mask',capture)
    try:
        for index,skill in enumerate(SKILLS):
            value=pipeline.process(closed(raw,index,gripper),base_eef=np.eye(4),context=context(skill))
            assert bool(surfaces[-1] and surfaces[-1].get('pose_normal_prior')) is expected_prior
            poses.append(value.objects[0].pose)
        np.testing.assert_allclose(poses,np.broadcast_to(poses[0],np.array(poses).shape))
    finally:pipeline.close();original.close()


def held_pipeline():
    pipeline,raw=full_scene();original,_=fixture()
    pipeline.config.update(object_reacquisition_camera='head')
    switched=[False]
    def masks(rgb,view):
        if switched[0] and view=='right':
            raise ValueError('mask_uncertain_requires_new_prompt:object=block')
        return original.segment(rgb)
    pipeline.segment=masks
    ctx=context('verify_grasp')
    first=pipeline.process(closed(raw,0),base_eef=np.eye(4),context=ctx)
    reference=np.array(first.objects[0].pose).reshape(4,4)
    # Actual numerical following over independent simulated motion samples;
    # no caller-provided held=True is injected into the scene.
    for index,x in enumerate((-.04,-.02,0.),1):
        eef=np.eye(4);eef[0,3]=x
        *_,held=pipeline._primary_evidence(closed(raw,index),eef@reference,eef,
            (1+index*100_000_000,)*2,camera='right')
    assert held is True
    original.close()
    return pipeline,raw,switched,reference,ctx


@pytest.mark.parametrize('skill',['grasp','regrasp','reobserve','move_tcp','release_supported'])
def test_new_request_preserves_proven_attachment_until_measured_opening(skill):
    pipeline,raw,_,_,ctx=held_pipeline()
    try:
        ctx=dict(ctx,phase=skill,request_id='new-unpublished-request')
        value=pipeline.process(closed(raw,4),base_eef=np.eye(4),context=ctx)
        assert value.objects[0].held is True
        opened=pipeline.process(closed(raw,5,0.),base_eef=np.eye(4),context=ctx)
        assert opened.objects[0].held is not True and pipeline.in_hand_reference is None
        closed_again=pipeline.process(closed(raw,6),base_eef=np.eye(4),context=ctx)
        assert closed_again.objects[0].held is None
    finally:pipeline.close()


def test_associated_camera_handoff_revalidates_history_without_new_motion():
    pipeline,raw,switched,reference,ctx=held_pipeline();switched[0]=True
    try:
        rows=[]
        for index in (4,5,6):
            obs=closed(raw,index)
            # Head and wrist source clocks are not ordered against each other.
            obs=obs.model_copy(update={'frames':tuple(f.model_copy(update={'source_ns':index})
                if f.camera=='head' else f for f in obs.frames)})
            value=pipeline.process(obs,base_eef=np.eye(4),context=ctx)
            assert value.geometry_camera=='head'
            np.testing.assert_allclose(np.array(value.objects[0].pose).reshape(4,4),reference)
            rows.append(value.objects[0].held)
        assert rows==[None,None,True]
        assert pipeline.in_hand_quarantine is None
        assert pipeline.last_timing['primary_evidence']['reason']=='measured_following_revalidated'
    finally:pipeline.close()


@pytest.mark.parametrize('change',['open','epoch','feedback','pose'])
def test_camera_handoff_cannot_restore_changed_physical_or_authority_state(change):
    pipeline,raw,switched,_,ctx=held_pipeline();switched[0]=True
    try:
        for index in (4,5,6,7):
            obs=closed(raw,index,0. if change=='open' else 50.)
            if change=='feedback':
                valid=list(obs.state_valid_mask);valid[15]=False
                obs=obs.model_copy(update={'state_valid_mask':tuple(valid)})
            if change=='pose':
                camera=np.eye(4);camera[0,3]=.03
                pipeline.config['hole_camera']=dict(pipeline.config['hole_camera'],base_camera=camera.tolist())
            value=pipeline.process(obs,base_eef=np.eye(4),context=ctx|({'epoch':2} if change=='epoch' else {}))
            assert value.objects[0].held is not True
    finally:pipeline.close()


@pytest.mark.parametrize('independent',[False,True])
@pytest.mark.parametrize('relocalize',[False,True])
def test_current_anchors_preserve_recent_aperture_for_placement_verification(independent,relocalize):
    pipeline,raw=full_scene();anchor,_,masks=anchored_fixture()
    config=dict(pipeline.config,fixture_occlusion=anchor.config['fixture_occlusion'],
        independent_fixture_localization=independent,relocalize_visible_fixture=relocalize)
    if relocalize:
        # The measured lid is at z=1 in these analytic pixels. The registered
        # object's origin lies below its observed face in the inserted fixture.
        face=np.array(config['face_object']);face[2,3]=.1;config['face_object']=face.tolist()
    pipeline.close();pipeline=CalibratedScenePipeline(config,lambda rgb,view:masks,anchor.blob)
    task=SimpleNamespace(object_id='block',shape='square',hole_id='hole')
    try:
        first=pipeline.process(sample(raw,0),base_eef=np.eye(4),context=context('insert'))
        assert first.objects[1].visible
        masks['hole']=masks['block'].copy()
        for index,skill in enumerate(('release','reobserve','verify_placed'),1):
            value=pipeline.process(sample(raw,index),base_eef=np.eye(4),context=context(skill))
            assert value.objects[0].inserted and not value.objects[1].visible
            assert pipeline.last_timing['aperture_tracking']['last_visible_ns']==1
        assert verify('verify_placed',task,value,'check-only').outcome=='success'
        expired=pipeline.process(sample(raw,6),base_eef=np.eye(4),context=context('verify_placed'))
        assert expired.objects[0].inserted is None
        assert verify('verify_placed',task,expired,'check-only').outcome=='pending_review'
    finally:pipeline.close();anchor.close()


@pytest.mark.parametrize('change',['never_observed','anchor_moved','unauthorized'])
def test_bucket_pose_alone_never_fabricates_occluded_aperture(change):
    pipeline,raw=full_scene();anchor,_,masks=anchored_fixture()
    config=dict(pipeline.config,fixture_occlusion=anchor.config['fixture_occlusion'],
        independent_fixture_localization=True)
    pipeline.close();pipeline=CalibratedScenePipeline(config,lambda rgb,view:masks,anchor.blob)
    try:
        if change!='never_observed':
            pipeline.process(sample(raw,0),base_eef=np.eye(4),context=context('insert'))
        masks['hole']=masks['block'].copy()
        if change=='anchor_moved':masks['a']=np.roll(masks['a'],2,axis=1)
        value=pipeline.process(sample(raw,1),base_eef=np.eye(4),
            context=context('verify_placed',authorized=change!='unauthorized'))
        assert value.objects[0].inserted is None
        assert len(value.objects)==1
    finally:pipeline.close();anchor.close()

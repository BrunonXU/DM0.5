import numpy as np
import pytest

from f1_harness.contracts import SceneObject
from f1_harness.scene_pipeline import CalibratedScenePipeline
from f1_harness.perception import FixedColorSegmenter
from test_scene_pipeline import fixture


def frame_at(obs,index,*,grip=0.,joint=0.):
    stamp=1+index*100_000_000
    frames=tuple(f.model_copy(update={'source_ns':stamp,'received_ns':stamp,'sequence':index+1}) for f in obs.frames)
    pair=obs.pairings[0].model_copy(update={'rgb_sequence':index+1,'depth_sequence':index+1})
    state=[0.]*16;state[7]=joint;state[15]=grip
    return obs.model_copy(update={'received_ns':stamp,'state_received_ns':stamp,'state16':tuple(state),
                                 'frames':frames,'pairings':(pair,)})


def context(phase='align',*,epoch=1,authorized=True,generation=1):
    return dict(owner_instance_id='owner',session_id='s',task_id='t',task_hash='hash',
                task_generation=generation,epoch=epoch,phase=phase,authorized=authorized,request_id=phase)


def anchored_fixture():
    old,obs=fixture();cfg=dict(old.config);masks=old.segment(np.zeros((20,20,3),np.uint8))
    anchors={}
    for key,(x,y) in {'a':(1,1),'b':(14,1),'c':(1,14)}.items():
        mask=np.zeros((20,20),bool);mask[y:y+5,x:x+5]=True;masks[key]=mask
        anchors[key]=dict(roi_center_px=[x+2,y+2],depth_m=1.)
    cfg['fixture_occlusion']=dict(validated=True,evidence_ids=['test-only-heldout'],calibration_id='test-cal',
        fixture_id='fixture',anchors=anchors,max_occlusion_ns=500_000_000,pixel_tolerance=.5,depth_tolerance_m=.001)
    return CalibratedScenePipeline(cfg,lambda rgb:masks,old.blob),obs,masks


def test_expected_overlap_uses_fresh_independent_fixture_and_expires():
    pipe,obs,masks=anchored_fixture()
    assert pipe.process(frame_at(obs,0),base_eef=np.eye(4),context=context()).objects[1].visible
    masks['hole']=masks['block'].copy()
    for i in range(1,6):
        value=pipe.process(frame_at(obs,i),base_eef=np.eye(4),context=context())
        assert not value.objects[1].visible and value.objects[1].pose is not None
    with pytest.raises(ValueError,match='hole_occlusion_without_current_fixture_evidence'):
        pipe.process(frame_at(obs,6),base_eef=np.eye(4),context=context())
    assert pipe.hole_last_visible_ns is None


@pytest.mark.parametrize('change',['anchor_missing','anchor_moved','epoch','generation','unauthorized'])
def test_occlusion_cannot_hide_fixture_or_owner_change(change):
    pipe,obs,masks=anchored_fixture()
    pipe.process(frame_at(obs,0),base_eef=np.eye(4),context=context())
    masks.pop('hole')
    ctx=context()
    if change=='anchor_missing':masks.pop('a')
    if change=='anchor_moved':masks['a']=np.roll(masks['a'],2,axis=1)
    if change=='epoch':ctx=context(epoch=2)
    if change=='generation':ctx=context(generation=2)
    if change=='unauthorized':ctx=context(authorized=False)
    with pytest.raises(ValueError,match='hole_occlusion'):
        pipe.process(frame_at(obs,1),base_eef=np.eye(4),context=ctx)


def test_overlap_without_anchor_contract_fails_closed():
    pipe,obs=fixture();masks=pipe.segment(None);masks['hole']=masks['block'].copy();pipe.segment=lambda rgb:masks
    with pytest.raises(ValueError,match='hole_occlusion'):
        pipe.process(obs,base_eef=np.eye(4),context=context())


def test_motion_establishes_grasp_then_stationary_window_retains_it(monkeypatch):
    pipe,obs=fixture();pose=np.eye(4)
    def measured_object(**kwargs):
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
                           frame_id='test-base',uncertainty_m=.001,evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',measured_object)
    pipe.config['face_object']=np.eye(4).tolist()
    for i,z in enumerate([0.,.01,.02,.02,.02]):
        pose[2,3]=z; eef=np.eye(4);eef[2,3]=z
        result=pipe.process(frame_at(obs,i,grip=50,joint=z*100),base_eef=eef,context=context('grasp' if i<3 else 'verify_grasp'))
    assert result.stationary and result.objects[0].held is True
    assert len(pipe.stationary_history)==3 and len(pipe.tracking_history)==5
    # A new owner epoch cannot inherit the grasp claim from a stationary image.
    result=pipe.process(frame_at(obs,5,grip=50,joint=2),base_eef=eef,context=context('verify_grasp',epoch=2))
    assert result.objects[0].held is None


def test_visible_grasp_miss_survives_stationary_verification_but_not_new_scope(monkeypatch):
    pipe,obs=fixture();pose=np.eye(4)
    pipe.config['gripper_stationary_delta']=.5
    def object_on_table(**kwargs):
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
                           frame_id='test-base',uncertainty_m=.001,evidence_ids=('test-measurement',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',object_on_table)
    pipe.config['face_object']=np.eye(4).tolist()
    for i in range(28):
        z=min(i*.02,.04);eef=np.eye(4);eef[2,3]=z
        result=pipe.process(frame_at(obs,i,grip=50,joint=z*100),base_eef=eef,
            context=context('grasp' if i<3 else 'verify_grasp'))
        if i>=2:assert result.objects[0].held is False
    assert pipe.last_timing['primary_evidence']['reason']=='measured_object_did_not_follow_hand'
    result=pipe.process(frame_at(obs,28,grip=50,joint=4),base_eef=eef,
        context=context('verify_grasp',epoch=2))
    assert result.objects[0].held is None


def test_completed_grasp_idle_gap_then_stationary_verify_retains_measured_hold(monkeypatch):
    pipe,obs=fixture();pose=np.eye(4)
    def measured_object(**kwargs):
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
                           frame_id='test-base',uncertainty_m=.001,evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',measured_object)
    pipe.config['face_object']=np.eye(4).tolist()
    active={**context('grasp'),'request_id':'grasp-request'}
    # Actual lifecycle: moving grasp(r) -> completed grasp(None) -> stationary verify(v).
    for i,z in enumerate([0.,.01,.02]):
        pose[2,3]=z;eef=np.eye(4);eef[2,3]=z
        result=pipe.process(frame_at(obs,i,grip=50,joint=z*100),base_eef=eef,context=active)
    assert result.objects[0].held is True
    completed={**active,'request_id':None}
    result=pipe.process(frame_at(obs,3,grip=50,joint=2),base_eef=eef,context=completed)
    assert result.objects[0].held is True
    verifying={**active,'phase':'verify_grasp','request_id':'verify-request'}
    result=pipe.process(frame_at(obs,4,grip=50,joint=2),base_eef=eef,context=verifying)
    assert result.stationary and result.objects[0].held is True
    # A submitted request is not a new physical grasp. Measured opening or
    # inconsistent following still invalidates the historical attachment.
    result=pipe.process(frame_at(obs,5,grip=50,joint=2),base_eef=eef,
                        context={**active,'request_id':'new-grasp-request'})
    assert result.objects[0].held is True


def test_gripper_opening_and_visual_slip_invalidate_held(monkeypatch):
    pipe,obs=fixture();pose=np.eye(4)
    def measured_object(**kwargs):
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
                           frame_id='test-base',uncertainty_m=.001,evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',measured_object)
    pipe.config['face_object']=np.eye(4).tolist()
    for i,z in enumerate([0.,.01,.02]):
        pose[2,3]=z;eef=np.eye(4);eef[2,3]=z
        value=pipe.process(frame_at(obs,i,grip=50),base_eef=eef,context=context('grasp'))
    assert value.objects[0].held
    pose[0,3]=.01
    value=pipe.process(frame_at(obs,3,grip=50),base_eef=eef,context=context('inspect_in_hand'))
    assert value.objects[0].held is None and pipe.in_hand_reference is None
    # Open command alone is insufficient: object must also be observed stationary.
    pose[0,3]=.02
    value=pipe.process(frame_at(obs,4,grip=0),base_eef=eef,context=context('release'))
    assert value.objects[0].held is None
    for i in (5,6):value=pipe.process(frame_at(obs,i,grip=0),base_eef=eef,context=context('verify_placed'))
    assert value.objects[0].held is False


def test_fixed_color_unique_component_and_no_arbitrary_selection():
    detector=FixedColorSegmenter({'block':dict(hsv_lower=[0,200,200],hsv_upper=[5,255,255],
                                              minimum_area_px=20,maximum_area_px=50)})
    image=np.zeros((30,30,3),np.uint8);image[3:8,4:9]=[255,0,0]
    mask,score=detector.segment(image,box=[1,1,29,29],object_id='block')[0]
    assert score==1 and mask.sum()==25
    image[20:25,20:25]=[255,0,0]
    assert detector.segment(image,box=[1,1,29,29],object_id='block')[0][1]==0
    image[:]=0;image[1:6,4:9]=[255,0,0]
    assert detector.segment(image,box=[1,1,29,29],object_id='block')[0][1]==0


def test_unknown_anchors_are_not_configured_as_validated():
    pipe,obs,masks=anchored_fixture()
    pipe.config['fixture_occlusion']['validated']=False
    with pytest.raises(ValueError,match='independently_validated'):
        CalibratedScenePipeline(pipe.config,lambda rgb:masks,pipe.blob)


def test_fixture_diagnostic_reports_common_depth_shift_without_relaxing_admission():
    pipe,obs,masks=anchored_fixture()
    depth=np.ones((20,20))-.01
    assert not pipe._fixture_visible(masks,depth)
    report=pipe.last_timing['fixture_check']
    assert not report['valid'] and set(report['anchors'])=={'a','b','c'}
    for row in report['anchors'].values():
        assert row['reason']=='depth_displacement'
        assert row['depth_error_m']==pytest.approx(-.01)
        assert row['pixel_error']==0 and row['depth_support']==25
    # New actual evidence, not a logged diagnosis, is needed to restore validity.
    assert pipe._fixture_visible(masks,np.ones((20,20)))
    assert pipe.last_timing['fixture_check']['valid']


def test_fixture_diagnostic_keeps_missing_and_empty_depth_distinct():
    pipe,obs,masks=anchored_fixture()
    masks.pop('a');depth=np.ones((20,20));depth[masks['b']]=np.nan
    assert not pipe._fixture_visible(masks,depth)
    rows=pipe.last_timing['fixture_check']['anchors']
    assert rows['a']['reason']=='missing_anchor'
    assert rows['b']['reason']=='insufficient_depth' and rows['b']['measured_depth_m'] is None
    assert rows['c']['valid']
    import json
    json.dumps(pipe.last_timing,allow_nan=False)


def test_explicit_coarse_policy_warns_within_existing_planning_margin_only():
    pipe,obs,masks=anchored_fixture()
    pipe.coarse=True
    pipe.config['planning_margin_m']=.015
    pipe.occlusion['depth_policy']='coarse_margin_warning'
    assert pipe._fixture_visible(masks,np.ones((20,20))-.01)
    assert all(r['severity']=='warning' for r in pipe.last_timing['fixture_check']['anchors'].values())
    assert not pipe._fixture_visible(masks,np.ones((20,20))-.02)
    # Insufficient independent evidence is not a precision discrepancy.
    assert not pipe._fixture_visible(masks,np.zeros((20,20)))
    pipe.coarse=False
    assert not pipe._fixture_visible(masks,np.ones((20,20))-.01)


def test_precision_mode_rejects_coarse_warning_policy():
    pipe,obs,masks=anchored_fixture()
    pipe.occlusion['depth_policy']='coarse_margin_warning'
    with pytest.raises(ValueError,match='fixture_depth_warning_requires_coarse_development'):
        CalibratedScenePipeline(pipe.config,lambda rgb:masks,pipe.blob)


@pytest.mark.parametrize('change',['held','quarantined','open','changed_grip','new_epoch'])
def test_surface_direction_follows_only_applicable_measured_attachment(monkeypatch,change):
    pipe,obs=fixture();pose=np.eye(4);seen=[]
    pipe.config.update(face_object=np.eye(4).tolist(),gripper_stationary_delta=.5,
        surface_selection=dict(normal_world=[0,0,1],pose_normal_prior='registered_upright'))
    def measured_object(**kwargs):
        seen.append(kwargs['surface_selection'])
        return SceneObject(object_id='block',shape='square',pose=tuple(pose.ravel()),
            frame_id='test-base',uncertainty_m=.001,evidence_ids=('measured-test',))
    monkeypatch.setattr('f1_harness.scene_pipeline.object_from_mask',measured_object)
    for i,z in enumerate([0.,.01,.02]):
        pose[2,3]=z;eef=np.eye(4);eef[2,3]=z
        pipe.process(frame_at(obs,i,grip=50,joint=z*100),base_eef=eef,context=context('grasp'))
    assert pipe.in_hand_reference is not None
    if change=='quarantined':pipe.reject_measurement('measurement_gap')
    angle=np.deg2rad(20);eef[:3,:3]=[[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]]
    pipe.process(frame_at(obs,3,grip=0 if change=='open' else 60 if change=='changed_grip' else 50),
        base_eef=eef,context=context('transfer_above_hole',epoch=2 if change=='new_epoch' else 1))
    expected=eef[:3,2] if change in ('held','quarantined') else [0,0,1]
    assert np.allclose(seen[-1]['normal_world'],expected)
    assert ('pose_normal_prior' in seen[-1])==(change=='open')

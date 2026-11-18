"""Post-motion acquisition waits for complete fresh frames and settled evidence."""
import threading
import time

import pytest

from f1_harness.contracts import SceneObject,SkillIntent,Task,digest
from f1_harness.skill_services import VerifyService
from test_scene_pipeline import fixture


def test_verifier_waits_through_moving_and_partly_old_frames(monkeypatch):
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='test-cal')
    intent=SkillIntent(request_id='verify',session_id='session',owner_instance_id='owner',
        task_id=task.task_id,task_hash=digest(task),epoch=1,observation_id='before',
        scene_version='scene',calibration_id=task.calibration_id,skill='approach_grasp')
    service=VerifyService(task)
    checked=threading.Event()
    def geometry(obs,**kwargs):
        checked.set()
        if not obs.stationary:raise ValueError('robot_not_stationary')
    monkeypatch.setattr('f1_harness.skill_services.require_geometry',geometry)
    _,base=fixture()
    after=100
    obj=SceneObject(object_id='block',shape='square',visible=True,evidence_ids=('a'*64,))
    def scene(stamp,stationary,old=False):
        frames=tuple(f.model_copy(update={'received_ns':50 if old and i==0 else stamp})
                     for i,f in enumerate(base.frames))
        return base.model_copy(update=dict(received_ns=stamp,frames=frames,stationary=stationary,
            calibration_id=task.calibration_id,objects=(obj,)))
    service.dispatch('update_scene',{'observation':scene(200,False).model_dump()})
    result=[]
    thread=threading.Thread(target=lambda:result.append(service.dispatch('verify_skill',
        {'intent':intent.model_dump(),'after_ns':after})))
    thread.start()
    try:
        assert checked.wait(1) and thread.is_alive()
        service.dispatch('update_scene',{'observation':scene(250,True,old=True).model_dump()})
        time.sleep(.02)
        assert not result
        service.dispatch('update_scene',{'observation':scene(300,True).model_dump()})
        thread.join(timeout=1)
        assert result[0]['outcome']=='success'
        assert result[0]['evidence_ids']==('a'*64,)
    finally:
        thread.join(timeout=4)


def test_verifier_rejects_different_task_before_waiting():
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='cal')
    intent=SkillIntent(request_id='verify',session_id='session',owner_instance_id='owner',
        task_id=task.task_id,task_hash='wrong',epoch=1,observation_id='before',
        scene_version='scene',calibration_id=task.calibration_id,skill='verify_placed')
    with pytest.raises(RuntimeError,match='verifier_scope_mismatch'):
        VerifyService(task).dispatch('verify_skill',{'intent':intent.model_dump(),'after_ns':0})


def test_post_motion_gap_waits_for_new_evidence_without_aging_old_frames(monkeypatch):
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='test-cal')
    intent=SkillIntent(request_id='verify',session_id='session',owner_instance_id='owner',
        task_id=task.task_id,task_hash=digest(task),epoch=1,observation_id='before',
        scene_version='scene',calibration_id=task.calibration_id,skill='approach_grasp')
    elapsed=[0.]
    monkeypatch.setattr('f1_harness.skill_services.time.monotonic',lambda:elapsed[0])
    service=VerifyService(task,clock=lambda:int(elapsed[0]*1e9))
    _,base=fixture()
    obj=SceneObject(object_id='block',shape='square',visible=True,evidence_ids=('b'*64,))
    fresh=base.model_copy(update=dict(received_ns=1_500_000_000,
        frames=tuple(f.model_copy(update=dict(received_ns=1_500_000_000)) for f in base.frames),
        stationary=True,calibration_id=task.calibration_id,objects=(obj,)))
    def geometry(obs,**kwargs):
        assert obs.received_ns==1_500_000_000
        assert all(f.received_ns==1_500_000_000 for f in obs.frames)
    monkeypatch.setattr('f1_harness.skill_services.require_geometry',geometry)
    class Arrival:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def wait(self,remaining):
            assert 1.5 < remaining <= 2.0
            elapsed[0]=1.5
            service.latest=fresh
    service.condition=Arrival()
    result=service.dispatch('verify_skill',{'intent':intent.model_dump(),'after_ns':100})
    assert result['outcome']=='success' and elapsed[0]==1.5


def test_each_skill_can_use_remaining_bounded_task_budget():
    fields=dict(request_id='motion',session_id='session',owner_instance_id='owner',
        task_id='task',task_hash='hash',epoch=1,observation_id='obs',
        scene_version='scene',calibration_id='cal')
    assert SkillIntent(**fields,skill='transfer_above_hole',budget_ms=90000).budget_ms==90000
    assert SkillIntent(**fields,skill='grasp',budget_ms=300000).budget_ms==300000
    with pytest.raises(ValueError):
        SkillIntent(**fields,skill='transfer_above_hole',budget_ms=300001)


@pytest.mark.parametrize('change,outcome,reason',[
    ('held','success','owner_transport_endpoint_and_held_target'),
    ('unheld','failure','transport_target_observed_unheld'),
    ('unknown','pending_review','grasp_held_evidence_missing'),
    ('missing_transform','pending_review','in_hand_transform_missing'),
    ('stale_source','pending_review','transport_attachment_frame_missing'),
    ('absent_frame','pending_review','transport_attachment_frame_missing'),
    ('old_arm','pending_review','pre_motion_frame'),
])
def test_transfer_endpoint_verifies_attachment_from_bound_rgbd_only(monkeypatch,change,outcome,reason):
    from f1_harness.observation import require_geometry
    from test_independent_fixture import coarse_case
    plan,_,intent,_=coarse_case()
    _,observed=fixture()
    ids=tuple(f.blob_sha256 for f in observed.frames)
    obj=SceneObject(object_id=plan.task.object_id,shape='square',held=True,
        eef_object_pose=(1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.),evidence_ids=ids)
    obs=observed.model_copy(update=dict(geometry_scope='coarse_object_verification',geometry_camera='head',
        objects=(obj,),stationary=True,calibration_id=plan.task.calibration_id,
        quality_warnings=('fixture_unavailable:fixture_relocalization_anchor_identity_changed',)))
    args=dict(goal=plan.task.goal,admission_profile=plan.task.admission_profile,
        hole_id=plan.task.hole_id,now_ns=2,max_age_ns=500_000_000)
    with pytest.raises(ValueError,match='coarse_geometry_cannot_authorize_precision_task'):
        require_geometry(obs,skill='transfer_above_hole',**args)
    for skill in ('align','insert','release','verify_placed'):
        with pytest.raises(ValueError,match='aperture_measurement_required:.*:fixture_relocalization_anchor_identity_changed'):
            require_geometry(obs,skill=skill,**args)
    if change=='unheld':obj=obj.model_copy(update=dict(held=False,eef_object_pose=None))
    if change=='unknown':obj=obj.model_copy(update=dict(held=None,eef_object_pose=None))
    if change=='missing_transform':obj=obj.model_copy(update=dict(eef_object_pose=None))
    if change=='stale_source':obj=obj.model_copy(update=dict(evidence_ids=(ids[0],'1'*64)))
    if change=='absent_frame':obs=obs.model_copy(update=dict(frames=obs.frames[:1]))
    if change=='old_arm':obs=obs.model_copy(update=dict(state_received_ns=0))
    service=VerifyService(plan.task,clock=lambda:2)
    service.dispatch('update_scene',dict(observation=obs.model_copy(update=dict(objects=(obj,))).model_dump()))
    bounds=iter((0.,3.))
    monkeypatch.setattr('f1_harness.skill_services.time.monotonic',lambda:next(bounds))
    result=service.dispatch('verify_skill',dict(intent=intent.model_copy(update=dict(skill='transfer_above_hole')).model_dump(),after_ns=0))
    assert (result['outcome'],result['reason'])==(outcome,reason)

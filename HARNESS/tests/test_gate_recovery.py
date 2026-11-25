"""The failed capture -> scene -> task sequence, without hardware or relaxed limits."""
import numpy as np
import pytest

from f1_harness.observation import ObservationBuffer, require_geometry
from f1_harness.contracts import HarnessError, Task, SkillIntent, digest
from f1_harness.skill_services import PlanService
from test_observation import frame
from test_remote_readiness import backend
from test_full_development_scene import full_scene, sample
from test_phase_perception import context
from test_plan_revalidation import service, observation


def test_late_source_frame_never_enters_pair_or_poisons_future_frames():
    b=ObservationBuffer(clock=lambda:1000)
    for seq,stamp in enumerate((100,90,110,120),1):
        for stream in ('rgb','depth'): b.add(frame(stream,sequence=seq,source_ns=stamp,received_ns=seq*100))
        obs=b.snapshot()
        if stamp==90:
            assert obs.faults and all(f.source_ns==100 for f in obs.frames)
        else: assert not obs.faults and obs.pairings[0].quality=='receipt_only'
    events=b.diagnostic_events()
    assert {e['frame']['stream'] for e in events}=={'rgb','depth'}
    assert all(e['previous']['source_ns']==100 and e['delta_ns']==-10 for e in events)


def test_source_restart_has_new_identity_and_cannot_clear_hard_fault():
    b=ObservationBuffer();b.add(frame('rgb'));b.add(frame('depth'));old=b.snapshot()
    b.faults.add('resolution_changed:head')
    with pytest.raises(ValueError):b.rebuild_source(reason='timestamp_lower',evidence={'delta':-10})
    b.rebuild_source(reason='publisher_changed',evidence={'previous':'old','current':'new'})
    assert not b.snapshot().frames and b.snapshot().acquisition_instance!=old.acquisition_instance
    b.add(frame('rgb',source_ns=1));b.add(frame('depth',source_ns=1))
    assert all(f.source_epoch==1 for f in b.snapshot().frames)
    assert b.snapshot().faults==('resolution_changed:head',)


def test_missing_observation_returns_before_long_task_budget(monkeypatch):
    b=backend(monkeypatch,[False]*310+[True]);b.task_deadline=60.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion'] and not b.submissions
    assert b.clock.value==pytest.approx(15.) and b.task_deadline==60.


def test_total_budget_exhaustion_is_review_without_submission(monkeypatch):
    b=backend(monkeypatch,[False]);b.task_deadline=45.
    result=b.execute_tool('execute_skill',{'name':'approach_grasp'})
    assert result['outcome']=='pending_review' and result['no_motion'] and not b.submissions
    assert b.clock.value==pytest.approx(15.) and b.task_deadline==45.


def test_head_sampling_fault_cannot_admit_motion_but_allows_wrist_verification():
    pipe,obs=full_scene()
    try:
        for i in range(4):
            value=sample(obs,i).model_copy(update={'faults':('source_time_reversed:head',)})
            result=pipe.process(value,base_eef=np.eye(4),context=context())
        assert result.stationary and result.geometry_scope=='coarse_object_verification'
        assert {f.camera for f in result.frames}=={'right'} and len(result.objects)==1
        kw=dict(now_ns=result.received_ns,max_age_ns=500_000_000,admission_profile='development_coarse')
        require_geometry(result,skill='verify_grasp',**kw)
        for motion in ('approach_grasp','grasp','transfer_above_hole','align','insert','release'):
            with pytest.raises(ValueError):require_geometry(result,skill=motion,**kw)
        require_geometry(result,skill='grasp',verification=True,**kw)
    finally:pipe.close()


def test_registered_numeric_alternative_is_revalidated_with_its_own_geometry():
    clock=[1_000_000_000]
    task=Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',admission_profile='development_coarse')
    s=service(clock,task);obs=observation(clock[0]);original=s.numerics.call;count=0
    def call(method,**kwargs):
        nonlocal count
        if method=='solve_path':
            count+=1
            if count<=2:raise RuntimeError('bounded_IK_did_not_reach_goal')
        return original(method,**kwargs)
    s.numerics.call=call
    intent=SkillIntent(request_id='req',session_id='s',owner_instance_id='o',task_id=task.task_id,
        task_hash=digest(task),epoch=1,observation_id=obs.observation_id,scene_version=obs.scene_version,
        calibration_id=task.calibration_id,skill='approach_grasp')
    held=list(obs.state16);full=[0.]*22
    candidate=s.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=obs.model_dump(),full22=full,held16=held))
    assert candidate['candidate_variant']==1 and count>=3
    after=clock[0];clock[0]+=100_000_000;latest=observation(clock[0],name='fresh')
    proof=s.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=candidate,
        original_observation=obs.model_dump(),observation=latest.model_dump(),full22=full,held16=held,after_ns=after))
    assert proof['geometry_checks']['swept_segments']>0
    forged=dict(candidate,candidate_variant=0)
    with pytest.raises(HarnessError,match='stage_binding'):
        s.dispatch('revalidate_plan',dict(intent=intent.model_dump(),proposal=forged,
            original_observation=obs.model_dump(),observation=latest.model_dump(),full22=full,held16=held,after_ns=after))


@pytest.mark.parametrize('cancel',[False,True])
def test_post_motion_evidence_wait_never_replays_movement(tmp_path,cancel):
    from f1_harness.contracts import SkillResult
    from test_unpublished_plan import make_bridge
    e,b,clock,state,poll,_=make_bridge(tmp_path)
    try:
        assert poll() is None
        b.future.cancel();b.future=None
        b.future_kind='verify';b.verification_after_ns=clock[0]
        b.future=b.executor.submit(b.verifier,e.running,b.verification_after_ns)
        b.future.set_result(SkillResult(request_id=e.running.request_id,lifecycle='completed',
            outcome='pending_review',reason='fresh_post_motion_scene_missing'))
        assert poll() is None
        future,func,args=b.executor.jobs[-1]
        assert func==b.verifier and args[1]==b.verification_after_ns
        assert e.running.request_id=='one-request' and not e.request_publications
        if cancel:
            future.set_running_or_notify_cancel();b.revoke('user_stop')
        future.set_result(SkillResult(request_id='one-request',lifecycle='completed',outcome='success',
            evidence_ids=('test-post-motion-scene',)))
        assert poll() is None and b.proposal is None
        assert e.results['one-request'].outcome==('interrupted' if cancel else 'success')
        assert not e.request_publications
    finally:b.close()


def test_missing_post_motion_scene_escalates_with_actual_reason_without_replaying(tmp_path):
    from f1_harness.contracts import SkillResult
    from test_unpublished_plan import make_bridge
    e,b,clock,state,poll,_=make_bridge(tmp_path)
    try:
        poll();b.future.cancel();b.future=None
        b.future_kind='verify';b.verification_after_ns=clock[0];b.verification_wait_attempts=0
        e.live_observation_invalid_reason='mask_view=right:mask_uncertain_requires_new_prompt:object=square-1'
        b.future=b.executor.submit(b.verifier,e.running,b.verification_after_ns)
        for _ in range(2):
            b.future.set_result(SkillResult(request_id=e.running.request_id,lifecycle='completed',
                outcome='pending_review',reason='fresh_post_motion_scene_missing'))
            assert poll() is None
        assert e.running is None and not e.request_publications
        result=e.results['one-request']
        assert result.outcome=='pending_review'
        assert result.reason=='post_motion_verification_unavailable:'+e.live_observation_invalid_reason
        assert 'approach_grasp' not in e.completed_skills
        assert b.proposal is None
    finally:b.close()

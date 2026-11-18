"""Coarse quality diagnostics do not invent post-motion evidence or freshness."""
import json
import numpy as np
import pytest

from f1_harness.contracts import HarnessError, SkillIntent, Task, digest
from f1_harness.engine import TaskEngine
from f1_harness.evidence import EvidenceStore
from f1_harness.observation import require_geometry, geometry_quality_warnings, geometry_age_limit_ns
from f1_harness.revalidation import require_planning_geometry
from f1_harness.skill_services import VerifyService
from test_full_development_scene import full_scene, sample
from test_plan_revalidation import observation


def task():
    return Task(task_id='task',object_id='block',hole_id='hole',calibration_id='coarse',
                admission_profile='development_coarse')


def intent(t,obs,**kw):
    return SkillIntent(request_id='quality',session_id='session',owner_instance_id='owner',
        task_id=t.task_id,task_hash=digest(t),epoch=1,observation_id=obs.observation_id,
        scene_version=obs.scene_version,calibration_id=t.calibration_id,skill='approach_grasp',**kw)


def test_slow_processing_and_incomplete_history_keep_actual_times_and_verdicts():
    p,raw=full_scene()
    try:
        first=p.process(sample(raw,0),base_eef=np.eye(4))
        # Six raw 100ms intervals represent a slow SAM cycle, not six observations.
        second=p.process(sample(raw,6),base_eef=np.eye(4))
        for obs in (first,second):
            before=obs.model_dump()
            assert not obs.stationary and obs.objects[0].held is not True
            assert 'stationary_history_incomplete' in obs.quality_warnings
            require_geometry(obs,now_ns=obs.received_ns+800_000_000,max_age_ns=500_000_000,
                admission_profile='development_coarse')
            assert 'visual_measurements_above_age_budget' in geometry_quality_warnings(obs,
                now_ns=obs.received_ns+800_000_000,max_age_ns=500_000_000,admission_profile='development_coarse')
            assert obs.model_dump()==before
        assert 'measurement_gap' in second.quality_warnings
    finally:p.close()


def test_measured_motion_is_distinct_from_incomplete_history():
    p,raw=full_scene()
    try:
        p.process(sample(raw,0),base_eef=np.eye(4))
        nxt=sample(raw,1);state=list(nxt.state16);state[7]+=1.
        obs=p.process(nxt.model_copy(update={'state16':tuple(state)}),base_eef=np.eye(4))
        assert not obs.stationary and 'observed_motion' in obs.quality_warnings
        with pytest.raises(ValueError,match='stationary calibrated'):
            require_geometry(obs,now_ns=obs.received_ns,max_age_ns=500_000_000,
                             admission_profile='development_coarse')
    finally:p.close()


@pytest.mark.parametrize('failure',['clock','fault','state','identity'])
def test_quality_warning_never_overrides_real_input_failures(failure):
    obs=observation(1_000_000_000).model_copy(update=dict(stationary=False,
        quality_warnings=('stationary_history_incomplete',)))
    if failure=='clock':obs=obs.model_copy(update={'state_received_ns':3_000_000_000})
    elif failure=='fault':obs=obs.model_copy(update={'faults':('source_time_reversed:right',)})
    elif failure=='state':obs=obs.model_copy(update={'state_valid_mask':(False,)*16})
    else:obs=obs.model_copy(update={'calibration_id':None})
    with pytest.raises(ValueError):require_geometry(obs,now_ns=2_000_000_000,max_age_ns=500_000_000,
                                                   admission_profile='development_coarse')


def test_precision_and_single_approach_age_limits_unchanged():
    assert geometry_age_limit_ns(500_000_000)==500_000_000
    assert geometry_age_limit_ns(500_000_000,admission_profile='development_coarse',
                                 geometry_scope='coarse_static_approach')==500_000_000
    obs=observation(1_000_000_000).model_copy(update={'geometry_scope':'precision',
        'pairings':tuple(p.model_copy(update={'quality':'source_time_bounded'}) for p in observation(1).pairings)})
    with pytest.raises(ValueError,match='stale observation'):
        require_geometry(obs,now_ns=2_000_000_000,max_age_ns=500_000_000)


def test_engine_and_numerical_ingress_share_quality_policy(tmp_path):
    now=2_000_000_000;t=task();e=TaskEngine(EvidenceStore(tmp_path),mode='hardware',clock=lambda:now)
    session=e.open_session()['session_id'];e.prepare_task(session,t.model_dump())
    e.hardware_ready=True;e.local_start(physical_event=True,feedback_healthy=True)
    obs=observation(1_000_000_000,boot=e.host_boot_id).model_copy(update=dict(
        stationary=False,quality_warnings=('stationary_history_incomplete',)))
    e.update_observation(obs)
    req=intent(t,obs).model_copy(update=dict(session_id=session,owner_instance_id=e.instance,epoch=e.epoch))
    require_planning_geometry(obs,intent=req,task=t,now_ns=now,max_age_ns=500_000_000)
    assert e.submit_skill(req.model_dump())['lifecycle']=='accepted'
    events=[json.loads(line) for line in (tmp_path/'trace.jsonl').read_text().splitlines()]
    assert any(row['kind']=='observation_quality_warning' for row in events)
    assert not e.request_publications


def test_age_advisory_does_not_turn_pre_motion_frame_into_success(monkeypatch):
    t=task();obs=observation(1_000_000_000).model_copy(update=dict(received_ns=2_000_000_000,
        stationary=False,quality_warnings=('stationary_history_incomplete',)))
    service=VerifyService(t,clock=lambda:3_000_000_000)
    service.dispatch('update_scene',dict(observation=obs.model_dump()))
    times=iter((0.,3.));monkeypatch.setattr('f1_harness.skill_services.time.monotonic',lambda:next(times))
    result=service.dispatch('verify_skill',dict(intent=intent(t,obs).model_dump(),after_ns=1_500_000_000))
    assert result['outcome']=='pending_review' and result['reason']=='pre_motion_frame'

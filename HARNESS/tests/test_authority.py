from rejection_assertions import assert_rejected
import pytest
from f1_harness.host import fake_host
from f1_harness.contracts import SkillIntent, digest, HarnessError, SkillResult


def request(engine, plant, **updates):
    obs = plant.observe()
    value = dict(request_id="request-1", session_id=engine.session, owner_instance_id=engine.instance,
                 task_id=engine.task.task_id, task_hash=digest(engine.task), epoch=engine.epoch,
                 observation_id=obs.observation_id, scene_version=obs.scene_version,
                 calibration_id=engine.task.calibration_id, skill="approach_grasp")
    return SkillIntent(**(value | updates)).model_dump()


def test_preparation_does_not_authorize(tmp_path):
    e, p, _, _ = fake_host(tmp_path)
    e.authorized = False
    assert_rejected(e.submit_skill(request(e, p)), "not_authorized")


def test_idempotent_request_and_payload_conflict(tmp_path):
    e, p, _, store = fake_host(tmp_path)
    r = request(e, p)
    first = e.submit_skill(r)
    assert e.submit_skill(r) == first
    p.advance()
    assert e.submit_skill(r)["outcome"] == "success"
    assert (store.root / "trace.jsonl").read_text().count('"kind": "synthetic_plant_action"') == 1
    with pytest.raises(HarnessError, match="content_conflict"):
        e.submit_skill(r | {"skill": "grasp"})


@pytest.mark.parametrize("field,value,reason", [
    ("epoch", 0, "epoch"), ("owner_instance_id", "old", "owner"),
    ("task_hash", "other", "task_scope"), ("calibration_id", "old", "calibration"),
    ("observation_id", "old", "observation"), ("scene_version", "old", "scene"),
    ("skill", "raw_joint", "skill_not_authorized")])
def test_scope_rejected(tmp_path, field, value, reason):
    e, p, _, _ = fake_host(tmp_path)
    if field in ('observation_id','scene_version','skill'):
        assert_rejected(e.submit_skill(request(e,p,**{field:value})),reason)
    else:
        with pytest.raises(HarnessError, match=reason):
            e.submit_skill(request(e,p,**{field:value}))


def test_cancel_stages_and_late_completion(tmp_path):
    e, p, _, _ = fake_host(tmp_path)
    r = request(e, p)
    e.submit_skill(r); e.take_pending()
    e.cancel_skill(e.session, r["request_id"])
    assert e.skill_result(e.session, r["request_id"])["cancel_stage"] == "cancel_requested"
    with pytest.raises(HarnessError, match="late_or_revoked"):
        e.complete(r["request_id"], SkillResult(request_id=r["request_id"], lifecycle="completed", outcome="success"))
    e.acknowledge_hold(publication_id="owner-42", measured_stationary=False)
    assert e.hold_requested
    e.acknowledge_hold(publication_id="owner-45", measured_stationary=True)
    assert not e.hold_requested
    assert e.results[r["request_id"]].cancel_stage == "measured_stationary"


def test_deadline_and_heartbeat_cannot_resurrect(tmp_path):
    e, p, _, _ = fake_host(tmp_path)
    now = e.clock()
    e.clock = lambda: now
    e.submit_skill(request(e, p, budget_ms=1))
    now += 2_000_000
    e.tick()
    assert e.results["request-1"].reason == "skill_deadline"
    e.heartbeat(e.session)
    assert not e.authorized


def test_regrasp_budget(tmp_path):
    e, p, _, _ = fake_host(tmp_path)
    p.approached = True
    e.completed_skills.add("approach_grasp")
    e.submit_skill(request(e, p, skill="regrasp")); p.advance()
    assert_rejected(e.submit_skill(request(e, p, skill="regrasp", request_id="second")), "regrasp_budget")


def test_new_request_id_cannot_repeat_successful_motion(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    e.submit_skill(request(e,p));p.advance()
    assert_rejected(e.submit_skill(request(e,p,request_id='different-id')), 'motion_already_attempted')


def test_failed_insert_cannot_bypass_correction_budget(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    e.completed_skills.add('align');e.needs_correction=True
    assert_rejected(e.submit_skill(request(e,p,skill='insert')), 'requires_retreat')


def test_assistance_survives_restart_and_cannot_be_autonomous(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    e.revoke('manual_takeover',preempted=True)
    e.acknowledge_hold(publication_id='stopped',measured_stationary=True)
    e.local_start(physical_event=True,feedback_healthy=True)
    e.completed_skills.add('release')
    e.submit_skill(request(e,p,skill='verify_placed'))
    e.take_pending()
    e.complete('request-1',SkillResult(request_id='request-1',lifecycle='completed',outcome='success',synthetic=True))
    assert e.results['request-1'].assisted
    result=e.finish_task(e.session)
    assert result['outcome']=='success' and result['assisted'] and not result['autonomous_success']


def test_idle_motion_wait_has_terminal_unadmitted_receipt_and_no_budget_use(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    intent=request(e,p);scope=e.begin_idle_motion_wait(e.clock())
    epoch=e.epoch
    result=e.submit_skill(intent)
    assert result['lifecycle']=='rejected' and result['no_motion'] and not result['retryable']
    assert result['reason']=='owner_motion_wait'
    assert e.submit_skill(intent)==result and e.skill_result(e.session,intent['request_id'])==result
    assert e.task_requests==0 and e.pending is None and e.running is None and not e.attempted_motion
    assert e.take_pending() is None and e.authorized and e.epoch==epoch
    assert e.end_idle_motion_wait(scope,e.clock())
    assert e.observation is None and e.live_observation is None


@pytest.mark.parametrize('field,value,reason',[
    ('epoch',0,'owner_or_epoch_mismatch'),('task_hash','other','task_scope_mismatch'),
    ('calibration_id','old','calibration_mismatch')])
def test_idle_wait_never_turns_identity_mismatch_into_recoverable_receipt(tmp_path,field,value,reason):
    e,p,_,_=fake_host(tmp_path);intent=request(e,p)
    e.begin_idle_motion_wait(e.clock())
    with pytest.raises(HarnessError,match=reason):e.submit_skill(intent|{field:value})
    assert intent['request_id'] not in e.results and not e.pending


def test_idle_wait_recovery_requires_observation_and_measurements_after_window(tmp_path):
    e,p,_,_=fake_host(tmp_path);now=e.clock();e.clock=lambda:now
    scope=e.begin_idle_motion_wait(now);now+=200_000_000
    assert e.end_idle_motion_wait(scope,now)
    intent=request(e,p)
    old=e.observation.model_copy(update={'received_ns':now,'state_received_ns':now})
    e.update_observation(old)
    assert_rejected(e.submit_skill(intent), 'observation_before_motion_wait_recovery')
    now+=1
    e.update_observation(old.model_copy(update={'received_ns':now,'state_received_ns':now,
        'frames':tuple(f.model_copy(update={'received_ns':now}) for f in old.frames)}))
    assert e.submit_skill(intent)['lifecycle']=='rejected'
    assert e.submit_skill(intent | {'request_id':'fresh-request'})['lifecycle']=='accepted'


def test_idle_wait_lease_expiry_revokes_and_cannot_resume_old_scope(tmp_path):
    e,p,_,_=fake_host(tmp_path);now=e.clock();e.clock=lambda:now
    scope=e.begin_idle_motion_wait(now);epoch=e.epoch
    now+=e.lease_ns+1;e.tick()
    assert not e.authorized and e.epoch>epoch and e.motion_wait is None
    assert not e.end_idle_motion_wait(scope,now)


def test_scene_fetched_before_completed_idle_wait_has_exact_queryable_zero_motion_receipt(tmp_path):
    e,p,_,_=fake_host(tmp_path);now=e.clock();e.clock=lambda:now
    intent=request(e,p);scope=e.begin_idle_motion_wait(now)
    now+=200_000_000;assert e.end_idle_motion_wait(scope,now)
    result=e.submit_skill(intent)
    assert result['lifecycle']=='rejected' and result['no_motion']
    assert result['reason']=='observation_retired_by_motion_wait' and not result['retryable']
    assert e.skill_result(e.session,intent['request_id'])==result and e.submit_skill(intent)==result
    assert e.task_requests==0 and not e.pending and not e.running and e.authorized


@pytest.mark.parametrize('change,reason',[
    ({'observation_id':'never-seen'},'observation_or_scene_mismatch'),
    ({'scene_version':'not-retired'},'observation_or_scene_mismatch'),
    ({'calibration_id':'another-calibration'},'calibration_mismatch'),
    ({'epoch':999},'owner_or_epoch_mismatch'),
    ({'task_hash':'another-task'},'task_scope_mismatch')])
def test_retired_scene_receipt_never_generalizes_mismatch_or_scope(tmp_path,change,reason):
    e,p,_,_=fake_host(tmp_path);intent=request(e,p)
    scope=e.begin_idle_motion_wait(e.clock());assert e.end_idle_motion_wait(scope,e.clock())
    if reason=='observation_or_scene_mismatch':
        result=assert_rejected(e.submit_skill(intent|change),reason)
        assert not result['retryable'] and not e.pending
    else:
        with pytest.raises(HarnessError,match=reason):e.submit_skill(intent|change)
        assert intent['request_id'] not in e.results


def test_owner_journal_export_retains_ids_and_cursor(tmp_path):
    e,p,_,_=fake_host(tmp_path)
    e.submit_skill(request(e,p));p.advance()
    first=e.execution_events(e.session)
    assert any(r['kind']=='skill_result' for r in first['events'])
    assert len({r['event_id'] for r in first['events']})==len(first['events'])
    assert e.execution_events(e.session,first['next'])['events']==[]
    with pytest.raises(HarnessError,match='session_mismatch'):e.execution_events('other')

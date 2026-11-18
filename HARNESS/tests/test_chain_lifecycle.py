"""One lifecycle: stale plan -> changed scene -> fresh candidate -> stop/finish race."""
import json

import numpy as np
import pytest

from f1_harness.contracts import HarnessError
from f1_harness.geometry import transform
from f1_harness.revalidation import align_observation
from test_plan_revalidation import make_owner, observation


def test_expiry_scene_change_replan_and_stop_share_one_authority(tmp_path):
    engine,bridge,_,clock,state,poll,original=make_owner(tmp_path)
    engine.web_control_enabled=True;engine.recording_identity={'owner_run_id':'run'}
    bridge.web_was_authorized=True
    first=engine.pending;epoch=engine.epoch;session=engine.session

    def finish_job():
        future,function,args=bridge.executor.jobs[-1]
        try:value=function(*args)
        except Exception as exc:future.set_exception(exc)
        else:future.set_result(value)

    def fresh(name,shift=0.):
        clock[0]+=20_000_000
        obs=observation(clock[0],boot=engine.host_boot_id,name=name)
        obj=obs.objects[0];pose=transform(obj.pose);pose[0,3]+=shift
        return obs.model_copy(update={'objects':(obj.model_copy(update={'pose':tuple(pose.ravel())}),obs.objects[1])})

    def submit(obs,name):
        engine.update_observation(obs)
        intent=first.model_copy(update={'request_id':name,'observation_id':obs.observation_id,
                                        'scene_version':obs.scene_version})
        engine.submit_skill(intent.model_dump());poll()
        return intent

    try:
        poll();clock[0]+=600_000_000;finish_job();poll()
        # Coarse frame age is advisory: keep the unpublished candidate and
        # revalidate a genuinely fresh scene instead of rejecting on age.
        assert engine.results[first.request_id].lifecycle=='running' and engine.authorized
        clock[0]+=4_841_571_000;poll()
        latest=fresh('changed',.02);engine.update_live_observation(latest,engine.status()['perception_context'])
        poll();finish_job();poll()
        result=engine.results[first.request_id]
        assert result.retryable and result.no_motion and 'goal_changed' in result.reason
        assert engine.epoch==epoch and not engine.request_publications
        assert engine.submit_skill(first.model_dump())==engine.results[first.request_id].model_dump()
        assert engine.pending is None  # An old ID is only a receipt, never another action.
        submit(fresh('third',.02),'third-request');finish_job();clock[0]+=100_000_000;poll()
        latest=fresh('equivalent',.02)
        obj=latest.objects[0];s=np.eye(4);s[:2,:2]=[[0.,-1.],[1.,0.]]
        latest=latest.model_copy(update={'objects':(obj.model_copy(update={
            'pose':tuple((transform(obj.pose)@s).ravel())}),latest.objects[1])})
        engine.update_live_observation(latest,engine.status()['perception_context'])
        poll();finish_job()
        assert poll() is not None and engine.authorized and not engine.request_publications
        payload=dict(request_id='stop',run_id='run',owner_instance_id=engine.instance,
            session_id=session,task_hash=first.task_hash,epoch=epoch)
        bridge.revoke('operator_stop');engine.finish_task(session)
        bridge.queue_web_control('web_stop',payload)
        engine.close_session(session)  # Deliberately reproduce the historical race.
        bridge.process_web_controls(feedback_healthy=True)
        assert bridge.web_control_status('stop')['stage']=='cancel_requested'
        for index in range(3):
            clock[0]+=100_000_000
            bridge.update_feedback(full22=state['full22'],measured16=state['measured16'],stamps16=(clock[0],)*16)
            bridge.after_publication(publication_id=f'hold-{index}',measured16=state['measured16'],
                                     publication_complete=True,now_ns=clock[0])
        assert bridge.web_control_status('stop')['stage']=='measured_stationary'
        bridge.web_close_session(payload)
        assert not engine.authorized and not engine.request_publications
        rows=[json.loads(line) for line in (engine.store.root/'trace.jsonl').read_text().splitlines()]
        started=[row for row in rows if row['kind']=='harness_revalidation_started']
        assert [row['observation']['observation_id'] for row in started]==['changed','equivalent']
        assert all(row['publication_count']==0 for row in started)
    finally:bridge.close()


def test_square_attachment_coordinates_change_together():
    before=observation(1_000_000_000);obj=before.objects[0]
    attachment=np.eye(4);attachment[:3,3]=[.03,.01,.1]
    before=before.model_copy(update={'objects':(obj.model_copy(update={
        'held':True,'eef_object_pose':tuple(attachment.ravel())}),before.objects[1])})
    s=np.eye(4);s[:2,:2]=[[0.,-1.],[1.,0.]]
    obj=before.objects[0]
    after=before.model_copy(update={'objects':(obj.model_copy(update={
        'pose':tuple((transform(obj.pose)@s).ravel()),
        'eef_object_pose':tuple((attachment@s).ravel())}),before.objects[1])})
    aligned=align_observation(before,after)
    assert np.allclose(aligned.objects[0].pose,before.objects[0].pose)
    assert np.allclose(aligned.objects[0].eef_object_pose,before.objects[0].eef_object_pose)
    assert after.objects[0].pose!=before.objects[0].pose  # Raw evidence stays unchanged.


def test_coarse_stale_dependency_is_advisory_and_request_is_idempotent(tmp_path):
    engine,bridge,_,clock,_,_,original=make_owner(tmp_path)
    try:
        first=engine.pending;engine.pending=None;engine.results.clear();engine.request_hashes.clear()
        frame=original.frames[0].model_copy(update={'received_ns':clock[0]-542_408_000})
        old=original.model_copy(update={'frames':(frame,*original.frames[1:])})
        engine.update_observation(old)
        result=engine.submit_skill(first.model_dump())
        assert result['lifecycle']=='accepted'
        assert engine.pending.request_id==first.request_id and engine.authorized
        assert engine.observation.frames[0].received_ns==clock[0]-542_408_000
        assert engine.skill_result(engine.session,first.request_id)==result
        assert engine.submit_skill(first.model_dump())==result
        changed=first.model_copy(update={'skill':'grasp'})
        with pytest.raises(HarnessError,match='content_conflict'):engine.submit_skill(changed.model_dump())
    finally:bridge.close()

import pytest
from f1_harness.contracts import SceneObject
from f1_harness.grasp_execution import lift_evidence
from f1_harness.owner_bridge import JointProposal
from f1_harness.trajectory import schedule
from test_unpublished_plan import make_bridge


def observed(obs, stamp, held):
    return obs.model_copy(update=dict(observation_id=str(stamp),received_ns=stamp,state_received_ns=stamp,
        state16=(0.,)*15+(100.,),frames=tuple(f.model_copy(update=dict(received_ns=stamp,source_ns=stamp)) for f in obs.frames),
        objects=(SceneObject(object_id='block',shape='square',held=held,evidence_ids=('test',)),)))


@pytest.mark.parametrize('skill,stage',[('grasp','grasp_lift'),('recover_grasp','grasp_observation')])
@pytest.mark.parametrize('held',[True,False,None])
def test_owner_reads_evidence_during_streaming_lift_not_only_at_final_arrival(tmp_path,held,skill,stage):
    engine,bridge,clock,state,_,obs=make_bridge(tmp_path)
    try:
        engine.running=engine.pending.model_copy(update={'skill':skill});engine.pending=None
        target=(0.,)*15+(100.,)
        trace=(dict(semantic_stage=stage,start_index=0,end_index=2),)
        plan=JointProposal(engine.running.request_id,engine.epoch,'scene','real',(0.,)*22,
            (target,target),None,100_000_000,.5,geometry_trace=trace,
            trajectory=schedule((target,target),target,trace,20.))
        bridge.proposal=plan;bridge.current_target=target
        bridge.trajectory_at_boundary=False
        def tick():
            bridge.update_feedback(full22=(0.,)*22,measured16=target,stamps16=(clock[0],)*16)
            bridge.after_publication(publication_id=str(clock[0]),measured16=target,
                publication_complete=True,now_ns=clock[0])
        tick()
        clock[0]+=20_000_000
        engine.live_observation=observed(obs,clock[0],held)
        tick()
        if held is False:
            result=engine.results[plan.request_id]
            assert result.reason=='grasp_not_attached' and result.cancel_stage=='cancel_requested'
            assert result.resume_request_id is None and not result.no_motion
            assert bridge.proposal is None and engine.hold_requested and engine.authorized
            engine.acknowledge_hold(publication_id='hold',measured_stationary=True)
            assert engine.results[plan.request_id].cancel_stage=='measured_stationary'
        else:
            assert bridge.proposal is plan and engine.running is not None
            assert bridge.grasp_lift_monitor['confirmed'] is (held is True)
    finally:bridge.close()


@pytest.mark.parametrize('change',['old_image','old_feedback','wrong_grip','wrong_calibration','fault','invisible'])
def test_old_or_unrelated_observation_cannot_stop_lift_as_grasp_miss(tmp_path,change):
    engine,bridge,clock,_,_,obs=make_bridge(tmp_path)
    try:
        value=observed(obs,clock[0]+1,False)
        if change=='old_image':value=value.model_copy(update={'frames':obs.frames})
        if change=='old_feedback':value=value.model_copy(update={'state_received_ns':clock[0]})
        if change=='wrong_grip':value=value.model_copy(update={'state16':obs.state16})
        if change=='wrong_calibration':value=value.model_copy(update={'calibration_id':'other'})
        if change=='fault':value=value.model_copy(update={'faults':('feedback_expired',)})
        if change=='invisible':value=value.model_copy(update={'objects':(value.objects[0].model_copy(update={'visible':False}),)})
        assert lift_evidence(value,object_id='block',calibration_id='real',after_ns=clock[0],
            gripper=100.,gripper_tolerance=.5) is None
    finally:bridge.close()

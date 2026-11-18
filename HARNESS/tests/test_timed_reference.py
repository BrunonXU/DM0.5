from dataclasses import replace
import numpy as np
import pytest
from f1_harness.trajectory import schedule, sample, validate_schedule
from f1_harness.owner_bridge import JointProposal, valid_arrival_contract
from test_unpublished_plan import make_bridge


def test_reference_streams_through_samples_and_stops_before_closing():
    anchor=np.zeros(16);targets=np.zeros((4,16));targets[:,7]=[1,2,2,3];targets[2:,15]=100
    value=schedule(targets,anchor,[dict(end_index=4)],10.)
    assert value['stops']==[1,2,3]
    ref,index,stop,_=sample(value,targets,start_index=0,elapsed_s=.15)
    assert ref[7]==pytest.approx(1.5) and ref[15]==0 and not stop
    ref,index,stop,end=sample(value,targets,start_index=0,elapsed_s=50)
    assert stop and index==end==1 and ref[15]==0
    ref,index,stop,end=sample(value,targets,start_index=2,elapsed_s=0)
    assert index==2 and ref[15]==0 and not stop
    ref,index,stop,end=sample(value,targets,start_index=2,elapsed_s=50)
    assert index==2 and ref[15]==100 and stop
    assert validate_schedule(value,targets)
    assert not validate_schedule(value|{'stops':[3]},targets)
    assert not validate_schedule(value|{'times_s':[.01,.02,.03,.04]},targets)


@pytest.mark.parametrize('continuous',[False,True])
def test_accepted_task_space_proof_survives_between_ticks_with_point_four_five_degree_bias(tmp_path,continuous):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        e.running=e.pending;e.pending=None
        target=tuple(state['held16']);measured=list(target);measured[12]+=.45
        closing=list(target);closing[15]+=10
        timing=schedule([target,closing],target,[dict(end_index=1),dict(end_index=2)],20.) if continuous else None
        plan=JointProposal(e.running.request_id,e.epoch,'scene','real',(0.,)*22,(target,tuple(closing)),
            None if continuous else .3,300_000_000,.5,free_space_tracking_tolerance_deg=1.,trajectory=timing)
        b.proposal=plan;b.current_target=target;b.plan_observation=obs;b.plan_held_reference=target
        b.trajectory_at_boundary=True
        b.waypoint_verifier=lambda *a:dict(accepted=True)
        b.waypoint_published_ns=clock[0]-1
        def tick():
            b.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(clock[0],)*16)
            b.after_publication(publication_id=str(clock[0]),measured16=measured,publication_complete=True,now_ns=clock[0])
        tick();future,fn,args=b.executor.jobs[-1];future.set_result(fn(*args))
        for _ in range(15):
            clock[0]+=20_000_000;tick();assert b.index==0
        clock[0]+=20_000_000;tick()
        assert b.index==1 and len(b.executor.jobs)==1
    finally:b.close()


def test_continuous_arrival_contract_requires_all_semantic_boundaries():
    target=tuple([0.]*16)
    timing=schedule([target]*3,target,[dict(end_index=2)],20.)
    plan=JointProposal('r',1,'s','c',(0.,)*22,(target,)*3,None,300,.5,
                       geometry_trace=(dict(end_index=2),),trajectory=timing)
    assert valid_arrival_contract(plan)
    assert not valid_arrival_contract(replace(plan,trajectory=timing|{'stops':[2]}))
    assert not valid_arrival_contract(replace(plan,tolerance_deg=.3))


def test_owner_streams_without_waiting_for_each_joint_sample_and_cancel_clears_clock(tmp_path):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        assert poll() is None
        future,_,(intent,original,full,held)=b.executor.jobs[-1]
        points=np.tile(held,(3,1));points[:,7]=[1,2,3]
        timing=schedule(points,held,[dict(end_index=3)],10.)
        future.set_result(JointProposal(intent.request_id,intent.epoch,intent.scene_version,intent.calibration_id,
            full,tuple(map(tuple,points)),None,300_000_000,.5,trajectory=timing))
        assert poll()[7]==0
        b.after_publication(publication_id='0',measured16=state['measured16'],publication_complete=True,now_ns=clock[0])
        clock[0]+=20_000_000;poll()
        b.after_publication(publication_id='1',measured16=state['measured16'],publication_complete=True,now_ns=clock[0])
        clock[0]+=150_000_000
        target=poll()
        assert target[7]==pytest.approx(1.5) and b.index==1 and not b.trajectory_at_boundary
        # No measured arrival at q=1 was needed; fresh feedback still required.
        b.revoke('manual_takeover',preempted=True)
        assert b.trajectory_start_ns is None and b.arrival_evidence is None and poll() is None
    finally:b.close()

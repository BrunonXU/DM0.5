from dataclasses import replace
import pytest
from f1_harness.owner_bridge import JointProposal
from f1_harness.host import fake_host
from test_owner_bridge import install_running
from test_unpublished_plan import make_bridge
from test_plan_revalidation import scenario


def test_event_export_finishes_at_fixed_watermark_despite_new_hold_events(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    for i in range(150): e.store.event('sample',index=i)
    page=e.execution_events(e.session);until=page['high_watermark'];seen=list(page['events'])
    while not page['complete']:
        for i in range(60):e.store.event('later',index=i)
        page=e.execution_events(e.session,after=page['next'],until=until);seen+=page['events']
    assert len(seen)==until and not any(x['kind']=='later' for x in seen)
    e.store.event('large',payload='x'*50000)
    cursor=until;found=False
    while True:
        page=e.execution_events(e.session,after=cursor);cursor=page['next']
        found|=any(x['kind']=='owner_event_reference' for x in page['events'])
        if page['complete']:break
    assert found


def test_hold_journal_records_transitions_but_status_keeps_latest_publication(tmp_path):
    e,_,_,_=fake_host(tmp_path)
    for i in range(100):e.acknowledge_hold(publication_id=str(i),measured_stationary=False)
    e.acknowledge_hold(publication_id='stopped',measured_stationary=True)
    events=e.execution_events(e.session)['events']
    assert len([x for x in events if x['kind']=='hold_acknowledged'])==2
    assert e.status()['hold_confirmation']['publication_id']=='stopped'


@pytest.mark.parametrize('mode',['accept','reject','stale','slow_unchanged','moved','cancel'])
def test_tracking_proof_is_fresh_bound_and_does_not_block_owner(tmp_path,mode):
    e,b,clock,state,poll,obs=make_bridge(tmp_path)
    try:
        e.running=e.pending;e.pending=None
        target=tuple(state['held16']);measured=list(target);measured[12]+=1.44935
        plan=JointProposal(e.running.request_id,e.epoch,'scene','real',(0.,)*22,(target,target),.3,0,.5,
                           free_space_tracking_tolerance_deg=1.)
        b.proposal=plan;b.current_target=target;b.plan_observation=obs;b.plan_held_reference=target
        b.waypoint_verifier=lambda *a:dict(accepted=mode!='reject')
        b.waypoint_published_ns=clock[0]-1
        b.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(clock[0],)*16)
        b.after_publication(publication_id='first',measured16=measured,publication_complete=True,now_ns=clock[0])
        assert b.index==0
        future,fn,args=b.executor.jobs[-1];future.set_result(fn(*args))
        before=clock[0]
        clock[0]+=300_000_000 if mode in ('stale','slow_unchanged') else 10_000_000
        if mode=='moved':measured[12]+=.1
        if mode=='cancel':b.revoke('cancel')
        b.update_feedback(full22=(0.,)*22,measured16=measured,stamps16=(before if mode=='stale' else clock[0],)*16)
        b.after_publication(publication_id='second',measured16=measured,publication_complete=True,now_ns=clock[0])
        assert b.index==(1 if mode in ('accept','slow_unchanged') else 0)
    finally:b.close()


def test_tracking_task_space_and_sweep_reject_large_real_deviation(scenario):
    serv,args,clock,original,latest,intent=scenario
    serv.fixture['free_space_tracking_tolerance_deg']=1.
    candidate=args['proposal']|{'free_space_tracking_tolerance_deg':1.}
    target=candidate['targets'][0];measured=list(target);measured[12]+=1.44935
    kw=dict(intent=intent.model_dump(),proposal=candidate,observation=original.model_dump(),index=0,
            full22=args['full22'],measured16=measured)
    assert serv.check_waypoint(**kw)['accepted']
    real=serv.numerics.call
    def shifted(method,**values):
        result=real(method,**values)
        if method=='forward_path':result[1][0][3]+=.1
        return result
    serv.numerics.call=shifted
    assert serv.check_waypoint(**kw)['reason']=='tracking_task_space_error'
    with pytest.raises(Exception,match='scope_mismatch'):
        serv.check_waypoint(**(kw|{'intent':intent.model_copy(update={'skill':'insert'}).model_dump()}))


def test_coarse_planning_age_preserves_evidence_without_rejecting_geometry(scenario):
    from f1_harness.revalidation import revalidate_candidate
    from f1_harness.contracts import digest
    serv,args,clock,original,latest,intent=scenario
    serv.fixture['allow_stale_planning_candidate']=True
    proposal=serv.dispatch('plan_skill',dict(intent=intent.model_dump(),observation=original.model_dump(),
                                           full22=args['full22'],held16=args['held16']))
    clock[0]+=600_000_000
    proof=revalidate_candidate(serv,**(args|{'proposal':proposal}))
    assert proof['observation_sha256']==digest(latest)
    assert proof['original_observation_sha256']==digest(original)
    assert latest.received_ns < clock[0]-500_000_000

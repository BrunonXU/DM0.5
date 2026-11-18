"""Known partial progress is held and replanned, never retried as zero motion."""
from dataclasses import replace
import pytest
from f1_harness.contracts import HarnessError
from test_unpublished_plan import make_bridge, finish_plan
from test_unpublished_retry import fresh_observation


def stalled(tmp_path):
    engine,bridge,clock,state,poll,obs=make_bridge(tmp_path)
    old=engine.pending
    poll();finish_plan(bridge);poll()
    target=list(bridge.current_target);target[7]=2.
    bridge.proposal=replace(bridge.proposal,targets=(tuple(target),))
    bridge.current_target=target
    for dt in (0,20_000_000,2_020_000_000):
        stamp=10_000_000_000+dt;clock[0]=stamp
        bridge.update_feedback(**{k:state[k] for k in ('full22','measured16')},stamps16=(stamp,)*16)
        bridge.after_publication(publication_id=str(stamp),measured16=state['measured16'],
            publication_complete=True,now_ns=stamp)
    return engine,bridge,clock,state,obs,old


def test_stall_retains_authority_but_requires_hold_and_fresh_replan(tmp_path):
    engine,bridge,clock,state,obs,old=stalled(tmp_path)
    try:
        result=engine.results[old.request_id]
        assert result.reason=='motion_stalled' and result.resume_request_id==old.request_id
        assert not result.no_motion and not result.retryable
        assert engine.authorized and engine.hold_requested and engine.running is None
        assert bridge.current_target is None and bridge.proposal is None
        # Complete measured stop through the same publication path.
        for dt in (20_000_000,320_000_000):
            stamp=clock[0]+dt;clock[0]=stamp
            bridge.update_feedback(full22=state['full22'],measured16=state['measured16'],stamps16=(stamp,)*16)
            bridge.after_publication(publication_id='hold-'+str(stamp),measured16=state['measured16'],
                publication_complete=True,now_ns=stamp)
        assert not engine.hold_requested
        latest=fresh_observation(obs,clock[0]);engine.update_observation(latest)
        new=old.model_copy(update=dict(request_id='resume',resume_request_id=old.request_id,
                                      observation_id=latest.observation_id,scene_version=latest.scene_version))
        assert engine.submit_skill(new.model_dump())['lifecycle']=='accepted'
        assert engine.submit_skill(new.model_dump())['lifecycle']=='accepted'  # idempotent
        assert engine.motion_recoveries==1
        engine.take_pending()
        engine.reject_unpublished_for_replan('resume',reason='planning_observation_expired_before_publication')
        assert engine.motion_recoveries==0
        assert engine.motion_checkpoints[old.request_id]['claimed_by'] is None
        engine.revoke('web_stop_requested')
        assert not engine.motion_checkpoints
    finally:bridge.close()

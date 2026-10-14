from concurrent.futures import Future
import time
from test_f1_policy import engine
from dagger_core.hil_supervisor import HILState

def test_stale_timeout_never_interrupts_manual():
    e,_=engine()
    try:
        e.started=True;e.supervisor.state=HILState.HIL_LATCHED;e.supervisor.policy_epoch=7
        f=Future();f.set_exception(TimeoutError('obsolete request'))
        e.real_future=f;e.real_future_epoch=6;e.real_future_request_id=12
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.HIL_LATCHED
        assert e.real_future is None and e.chunk is None and e.ready is None
    finally:e.close()

def test_current_policy_failure_still_protects():
    e,_=engine()
    try:
        e.started=True;e.supervisor.state=HILState.POLICY;e.supervisor.policy_epoch=7
        f=Future();f.set_exception(TimeoutError('current request'))
        e.real_future=f;e.real_future_epoch=7
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:e.close()

def test_policy_shadow_runs_inference_without_preparing_or_sending_outputs():
    from test_f1_output import make_owner
    from test_real_observation_shadow import packet
    owner,transport=make_owner(False)
    e,j=engine()
    try:
        e.started=True
        deadline=time.monotonic()+1;seq=0;inferred=False
        while time.monotonic()<deadline:
            now=time.monotonic_ns();e.operator(packet(seq,0),now,now);seq+=1;e.tick(now)
            if e.ready is not None:inferred=True
            target,source=e.sample(now,owner.last_submitted)
            owner.prepare();owner.approve(target,now/1e9);owner.publish(now/1e9)
            if inferred and source=='policy':break
            time.sleep(.005)
        assert inferred and j.chunks
        assert not transport.prepared and not transport.sent
    finally:e.close();owner.close()


def test_status_mailbox_writes_structured_identity_and_closes(tmp_path):
    import json
    from f1dagger.status import StatusPublisher
    from dagger_core.recording_sinks import NullSink
    e,_=engine()
    pub=StatusPublisher(tmp_path/'status.json',e,NullSink(),dict(execution_mode='shadow',purpose='input_check'),'synthetic')
    try:
        for _ in range(100):pub.update(None,'limit')
        deadline=time.monotonic()+1
        while not (tmp_path/'status.json').exists() and time.monotonic()<deadline:time.sleep(.01)
        snapshot=json.loads((tmp_path/'status.json').read_text())
        assert snapshot['schema']=='f1_hil_status_v1' and snapshot['run_id']=='synthetic'
        assert snapshot['status']['purpose']=='input_check' and snapshot['binding']['execution_mode']=='shadow'
    finally:pub.close();e.close()
    assert json.loads((tmp_path/'status.json').read_text())['status']['state']=='stopping'

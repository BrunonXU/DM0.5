from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace
import threading
import time
import numpy as np
import pytest
from PIL import Image
from f1dagger.contract import CONTRACT,CAMERAS
from f1dagger.observation import Observation,input_issue
from f1dagger.policy import HttpPolicy,F1PolicyEngine
from g2dagger.recording_observation import TimestampedFrame,DeviceTimestamp
from g2dagger.recording_sinks import NullSink
from g2dagger.hil_supervisor import HILState
from test_real_observation_shadow import packet


def observation(oid=0, start=1_000_000_000):
    io=BytesIO();Image.new('RGB',(16,16)).save(io,format='JPEG')
    frames=tuple(TimestampedFrame(c,oid,DeviceTimestamp(start,'ns','ros'),start+1,io.getvalue(),'image/jpeg',16,16) for c in CAMERAS)
    return Observation(oid,start,start+10,frames,(0.,)*16,(start+2,)*16,(0.,)*22)

class Source:
    def __init__(self):self.stop_event=threading.Event();self.i=0
    def capture(self,started):
        self.i+=1
        return observation(self.i,started)

class Journal:
    writer_error=None
    def __init__(self):self.chunks=[];self.events=[];self.observations=[]
    def record_chunk(self,*args):self.chunks.append(args);return True
    def record_timestamped_observation(self,*args,**kw):self.observations.append(args);return True
    def record_event(self,*args,**kw):self.events.append((args,kw));return True
    def control(self,**kw):return True

CONFIG={'capture_hz':30.,'policy':{'chunk_steps':3,'frequency_hz':10.}}

def engine(policy=None,teleop=False):
    journal=Journal()
    policy=policy or SimpleNamespace(model_id='fake',infer=lambda obs,rid,epoch:np.ones((3,16)))
    result=F1PolicyEngine(lambda *a,**k:None,Source(),policy,journal,NullSink(),CONFIG,teleop_only=teleop)
    return result,journal

def test_f1_open_grippers_are_valid_zero_feedback():
    assert input_issue(observation()) is None

def test_f1_policy_contract_identity_expiry_and_bounds():
    obs=observation()
    cfg=dict(endpoint='http://127.0.0.1:1',timeout_s=1.,chunk_steps=3,frequency_hz=10.,max_age_s=2.)
    client=HttpPolicy(cfg,'task',lambda a:None,clock=lambda:obs.available_monotonic_ns)
    client.model_id='model-a'
    meta=dict(contract=CONTRACT,request_id=7,observation_id=0,policy_epoch=3,
              observation_monotonic_ns=obs.capture_started_monotonic_ns,frequency_hz=10.)
    response=dict(meta,model_id='model-a',actions=np.zeros((3,16)).tolist())
    assert client.validate(response,meta,obs).shape==(3,16)
    for key,value in [('contract','g2'),('request_id',8),('policy_epoch',2),('model_id','changed'),('actions',[[0]*34]*3)]:
        with pytest.raises(ValueError):client.validate(dict(response,**{key:value}),meta,obs)
    client.clock=lambda:obs.capture_started_monotonic_ns+2_000_000_001
    with pytest.raises(ValueError,match='expired'):client.validate(response,meta,obs)

def test_f1_scheduler_uses_10hz_and_configured_horizon():
    e,j=engine()
    try:
        e.started=True;e.ready=np.arange(3)[:,None]*np.ones((1,16))
        e.sample(1_000_000_000,-np.ones(16))
        np.testing.assert_allclose(e.sample(1_150_000_000,np.zeros(16))[0],.5)
        assert e.policy_sampling['source_hz']==10. and 'raw_waypoint_34d' not in e.policy_sampling
        np.testing.assert_allclose(e.sample(1_300_000_000,np.zeros(16))[0],2.)
        assert e.chunk is None
    finally:e.close()

def test_first_start_teleop_pause_and_watchdog():
    e,j=engine(teleop=True)
    try:
        now=time.monotonic_ns()
        e.operator(packet(0,0),now,now)
        assert not e.started
        e.operator(replace(packet(1,1),hil_button_pressed=True),now+1,now+1)
        assert e.started and e.supervisor.state is HILState.HIL_LATCHED
        e.operator(packet(2,2),now+2,now+2)
        assert e.supervisor.state is HILState.HANDOFF_HOLD
        e.tick(now+3)
        assert e.real_future is None  # Manual mode never requests a model.
        e.operator(packet(3,3),now+4,now+4)
        assert e.supervisor.state is HILState.HIL_LATCHED
        e.tick(now+201_000_005)
        assert e.supervisor.state is HILState.SAFE_HOLD
        e.operator(packet(4,3),now+201_000_006,now+201_000_006)
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:e.close()

def test_late_f1_response_does_not_resume_human():
    entered,release=threading.Event(),threading.Event()
    def infer(obs,rid,epoch):
        entered.set();release.wait(2);return np.zeros((3,16))
    e,j=engine(SimpleNamespace(model_id='late',infer=infer))
    try:
        e.started=True
        seq=0;deadline=time.monotonic()+2
        while not entered.is_set() and time.monotonic()<deadline:
            now=time.monotonic_ns();e.operator(packet(seq,0),now,now);e.tick(now);seq+=1;time.sleep(.005)
        assert entered.is_set()
        now=time.monotonic_ns();e.operator(packet(seq,1),now,now)
        release.set()
        deadline=time.monotonic()+1
        while e.real_future is not None and time.monotonic()<deadline:
            now=time.monotonic_ns();e.tick(now);time.sleep(.005)
        assert e.ready is None and e.supervisor.state is HILState.HIL_LATCHED
    finally:release.set();e.close()


@pytest.mark.parametrize('stage', ['first_start', 'handoff'])
@pytest.mark.parametrize('stale_index', [0, 7, 14, 15], ids=['left-arm', 'right-arm', 'left-gripper', 'right-gripper'])
def test_policy_waits_for_post_boundary_state_even_when_images_are_new(stage, stale_index):
    class ControlledSource:
        def __init__(self): self.stop_event = threading.Event()
        def capture(self, started):
            # Test supplies snapshots explicitly; no concurrent replacement.
            self.stop_event.wait(3)
            return observation(999, started)
    requests, events = [], []
    policy = SimpleNamespace(model_id='fresh-state-test', infer=lambda obs, rid, epoch:
                             requests.append((obs, rid, epoch)) or np.zeros((3, 16)))
    e = F1PolicyEngine(lambda name, **fields: events.append((name, fields)),
                      ControlledSource(), policy, Journal(), NullSink(), CONFIG)
    try:
        for seq in range(2 if stage == 'first_start' else 4):
            now = time.monotonic_ns()
            e.operator(replace(packet(seq, seq), hil_button_pressed=seq > 0), now, now)
        expected_state = HILState.POLICY if stage == 'first_start' else HILState.HANDOFF_HOLD
        assert e.started and e.supervisor.state is expected_state
        boundary, epoch = e.minimum_observation_ns, e.supervisor.policy_epoch
        obs = observation(1, time.monotonic_ns())
        stamps = list(obs.state_timestamps_ns)
        stamps[stale_index] = boundary  # Equality is not a newly received callback.
        old = replace(obs, state_timestamps_ns=tuple(stamps))
        with e.latest_lock:
            e.latest = (old, epoch)
        e.tick(time.monotonic_ns())
        assert not requests and e.real_future is None
        assert e.supervisor.state is expected_state
        assert any(name == 'policy_input_rejected' and
                   fields['reason'] == 'state_not_received_after_control_boundary'
                   for name, fields in events)
        # Only a truly newer snapshot can release the request, without a new START.
        fresh = observation(2, time.monotonic_ns())
        with e.latest_lock:
            e.latest = (fresh, epoch)
        e.last_request_ns = 0  # Remove request-rate waiting, not freshness checks.
        e.tick(time.monotonic_ns())
        assert e.real_future is not None
        e.real_future.result(timeout=1)
        e.tick(time.monotonic_ns())
        assert len(requests) == 1 and requests[0][0] is fresh
        assert e.supervisor.state is HILState.POLICY and e.ready is not None
    finally:
        e.close()


@pytest.mark.parametrize('error', ['ConnectionClosedError: no close frame received or sent',
                                 'PermissionError: telemetry directory unavailable'])
def test_failed_transport_response_preserves_error_without_accepting_actions(error):
    policy=HttpPolicy({'endpoint':'http://127.0.0.1:1'}, 'diagnostic test', lambda a: None)
    with pytest.raises(ValueError, match='policy request failed \\(unverified response\\)') as caught:
        policy.validate_identity({'success':False,'error':error,'actions':[[0.]*16]},
                                 {'contract':CONTRACT}, observation())
    assert error in str(caught.value)


def test_unidentified_success_still_rejected_after_error_reporting_fix():
    policy=HttpPolicy({'endpoint':'http://127.0.0.1:1'}, 'diagnostic test', lambda a: None)
    with pytest.raises(ValueError, match='policy identity type mismatch: contract'):
        policy.validate_identity({'success':True,'actions':[[0.]*16]},
                                 {'contract':CONTRACT}, observation())

import json
import threading
import time

import pytest

from dagger_core.hil_supervisor import HILState
from f1dagger.observation import CameraUnavailable
from f1dagger.policy import F1PolicyEngine
from test_f1_demonstration import recorder, wait_for
from test_f1_policy import CONFIG, Source, Journal
from test_f1_recording_audio import Cues
from test_real_observation_shadow import packet
from dagger_core.recording_sinks import NullSink


class InterruptibleSource(Source):
    def __init__(self):
        super().__init__()
        self.missing = threading.Event()
        self.fatal = False

    def capture(self, started):
        if self.fatal:
            raise RuntimeError('feedback clock regressed')
        if self.missing.is_set():
            raise CameraUnavailable('right_wrist unavailable')
        return super().capture(started)


def test_camera_loss_commits_interrupted_prefix_and_requires_new_start(tmp_path):
    cues=Cues();r=recorder(tmp_path,audio=cues);s=InterruptibleSource();events=[]
    e=F1PolicyEngine(lambda name,**kw:events.append(name),s,None,r,r,CONFIG,
                     teleop_only=True,episodes=r)
    def send(seq,toggle):
        now=time.monotonic_ns();e.operator(packet(seq,toggle),now,now)
        e.tick(now)
    try:
        send(0,0);send(1,1);wait_for(lambda:r.phase=='recording')
        first=r.run_id
        wait_for(lambda:r.active[1].count>0)
        s.missing.set();wait_for(lambda:e.camera_issue is not None)
        send(2,1)
        assert e.supervisor.state is HILState.SAFE_HOLD
        wait_for(lambda:r.phase=='idle')
        assert r.last_saved['outcome']=='interrupted'
        assert r.last_saved['observations']>0
        m=json.loads((tmp_path/'data'/first/'journal/manifest.json').read_text())
        assert m['complete'] and m['binding']['termination']['requires_review']
        assert cues.events==['started']  # Interrupted is never a success cue.
        send(3,2)  # START while camera absent is consumed, never queued.
        assert r.phase=='idle' and e.supervisor.state is HILState.SAFE_HOLD
        s.missing.clear();wait_for(lambda:e.camera_issue is None)
        send(4,2)
        assert r.phase=='idle' and e.supervisor.state is HILState.SAFE_HOLD
        assert e.acquisition_error is None and e.capture_thread.is_alive()
        send(5,3);wait_for(lambda:r.phase=='recording')
        assert r.run_id!=first
        assert 'termination' not in r.active[0].binding
        send(6,4);wait_for(lambda:r.phase=='idle')
        assert cues.events==['started','started','saved']
        assert 'camera_acquisition_interrupted' in events
        assert 'camera_acquisition_restored' in events
    finally:e.close();assert r.close()


def test_camera_fault_still_holds_when_frames_return_before_control_tick():
    s=InterruptibleSource();j=Journal()
    e=F1PolicyEngine(lambda *a,**kw:None,s,None,j,NullSink(),CONFIG,teleop_only=True)
    try:
        now=time.monotonic_ns();e.operator(packet(0,0),now,now)
        e.operator(packet(1,1),now,now)
        s.missing.set();wait_for(lambda:e._camera_fault_pending is not None)
        s.missing.clear();wait_for(lambda:e.camera_issue is None)
        e.tick(time.monotonic_ns())
        assert e.supervisor.state is HILState.SAFE_HOLD
    finally:e.close()


def test_non_camera_failure_stays_fatal():
    s=InterruptibleSource();j=Journal()
    e=F1PolicyEngine(lambda *a,**kw:None,s,None,j,NullSink(),CONFIG,teleop_only=True)
    try:
        s.fatal=True;wait_for(lambda:e.acquisition_error is not None)
        with pytest.raises(RuntimeError,match='feedback clock regressed'):
            e.tick(time.monotonic_ns())
    finally:e.close()


def test_camera_fault_clears_pending_motion_before_dispatch():
    import numpy as np
    from test_f1_runtime import setup_loop
    loop,e,owner,transport,now,records,step=setup_loop()
    try:
        step(0);step(1,1);step(2,1,deadman=True)
        before=owner.last_submitted.copy()
        # Stop acquisition first to keep this deterministic injected fault
        # from being cleared by the fake continuously healthy source.
        e.stop_event.set();e.capture_thread.join(1)
        e.camera_issue=e._camera_fault_pending='right_wrist unavailable'
        step(3,1,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        assert records[-1]['source']=='hold'
        np.testing.assert_equal(owner.last_submitted,before)
        step(4,2,deadman=False)  # No movement despite a START during loss.
        e.camera_issue=None
        step(5,2,deadman=True)
        assert e.supervisor.state is HILState.SAFE_HOLD
        np.testing.assert_equal(owner.last_submitted,before)
    finally:e.close()

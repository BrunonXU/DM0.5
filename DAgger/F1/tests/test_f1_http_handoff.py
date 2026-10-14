"""Actual multipart HTTP across the F1 scheduler's first START/HIL/resume path."""
from dataclasses import replace
from email import policy as email_policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import numpy as np
import pytest
from f1dagger.contract import CONTRACT,CAMERAS
from f1dagger.policy import HttpPolicy,F1PolicyEngine
from g2dagger.hil_supervisor import HILState
from g2dagger.recording_sinks import NullSink
from test_f1_policy import Source,Journal
from test_real_observation_shadow import packet


@pytest.mark.parametrize('delayed_feedback', [False, True], ids=['fresh-state', 'delayed-gripper'])
@pytest.mark.parametrize('delayed_images', [False, True], ids=['fresh-images', 'pre-boundary-image'])
def test_http_old_response_discarded_and_resume_uses_new_images_state_epoch(delayed_feedback, delayed_images):
    class FeedbackSource(Source):
        frozen_stamp = None
        frozen_image_start = None
        gripper_position = 0.
        def capture(self, started):
            obs = super().capture(started)
            stamps, values = list(obs.state_timestamps_ns), list(obs.state16)
            if self.frozen_stamp is not None:
                stamps[14] = self.frozen_stamp
            values[14] = self.gripper_position
            if self.frozen_image_start is not None:
                early = self.frozen_image_start + self.i
                head = replace(obs.frames[0], host_received_monotonic_ns=early,
                               timestamp=replace(obs.frames[0].timestamp, value=early))
                obs = replace(obs, capture_started_monotonic_ns=early, frames=(head, *obs.frames[1:]))
            return replace(obs, state_timestamps_ns=tuple(stamps), state16=tuple(values))
    source = FeedbackSource()
    requests=[];replies={1:threading.Event(),2:threading.Event()};failures=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send_json(self,value):
            body=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            assert self.path=='/health'
            self.send_json(dict(contract=CONTRACT,ready=True,model_id='test-http-model'))
        def do_POST(self):
            try:
                assert self.path=='/process_frame'
                raw=self.rfile.read(int(self.headers['Content-Length']))
                message=BytesParser(policy=email_policy.default).parsebytes(
                    ('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
                parts={p.get_param('name',header='content-disposition'):p for p in message.iter_parts()}
                assert set(parts)=={'metadata',*CAMERAS}
                for name in CAMERAS:
                    assert parts[name].get_content_type()=='image/jpeg'
                    assert parts[name].get_payload(decode=True).startswith(b'\xff\xd8')
                meta=json.loads(parts['metadata'].get_payload(decode=True))
                expected_state = [0.]*16
                if delayed_feedback and meta['request_id'] == 2:
                    expected_state[14] = 43.
                assert meta['state16']==expected_state and meta['task']=='夹取纠正'
                requests.append(meta)
                assert replies[meta['request_id']].wait(2)
                response={key:meta[key] for key in ('contract','request_id','observation_id','policy_epoch','observation_monotonic_ns','frequency_hz')}
                self.send_json(dict(response,model_id='test-http-model',actions=np.full((meta['chunk_steps'],16),meta['request_id']).tolist()))
            except BaseException as exc:
                failures.append(exc)
                raise
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    journal=Journal();events=[]
    config={'capture_hz':30.,'policy':dict(endpoint='http://127.0.0.1:'+str(server.server_port),
        model_id='test-http-model',chunk_steps=3,frequency_hz=10.,timeout_s=2.,max_age_s=2.)}
    checked=[]
    client=HttpPolicy(config['policy'],'夹取纠正',lambda action:checked.append(action.copy()))
    engine=None
    try:
        client.health()
        engine=F1PolicyEngine(lambda name,**fields:events.append((name,fields)),source,client,journal,NullSink(),config)
        sequence=0;toggle=0
        def tick(button=False):
            nonlocal sequence
            now=time.monotonic_ns()
            engine.operator(replace(packet(sequence,toggle),hil_button_pressed=button),now,now)
            engine.tick(now);sequence+=1
            return now
        def until(predicate):
            deadline=time.monotonic()+1.5
            while not predicate() and time.monotonic()<deadline:
                tick();time.sleep(.005)
            assert predicate(),[(name,fields) for name,fields in events if name!='operator_packet']
        tick();time.sleep(.04);tick()
        assert not requests and not engine.started
        if delayed_images:
            source.frozen_image_start = time.monotonic_ns() - 1_000_000
        toggle=1;start_time=tick(True)
        if delayed_images:
            until(lambda:any(args[0].available_monotonic_ns > start_time and
                args[0].capture_started_monotonic_ns < start_time for args in journal.observations))
            assert not requests
            source.frozen_image_start = None
        until(lambda:len(requests)==1)
        assert engine.supervisor.state is HILState.POLICY
        toggle=2;tick(True)
        assert engine.supervisor.state is HILState.HIL_LATCHED
        replies[1].set();until(lambda:engine.real_future is None)
        assert engine.ready is None and engine.chunk is None
        assert any(name=='powered_policy_result' and fields['request_id']==1 and fields['accepted'] is False for name,fields in events)
        if delayed_feedback:
            source.frozen_stamp = time.monotonic_ns()
        if delayed_images:
            source.frozen_image_start = time.monotonic_ns() - 1_000_000
        toggle=3;handoff_time=tick(True)
        assert engine.supervisor.state is HILState.HANDOFF_HOLD
        if delayed_images:
            until(lambda:any(args[0].available_monotonic_ns > handoff_time and
                args[0].capture_started_monotonic_ns < handoff_time for args in journal.observations))
            assert len(requests) == 1 and engine.real_future is None
            assert engine.supervisor.state is HILState.HANDOFF_HOLD
            source.frozen_image_start = None
        if delayed_feedback:
            until(lambda:any(name == 'policy_input_rejected' and
                fields.get('reason') == 'state_not_received_after_control_boundary'
                for name,fields in events))
            assert len(requests) == 1 and engine.real_future is None
            assert engine.supervisor.state is HILState.HANDOFF_HOLD
            source.gripper_position = 43.
            source.frozen_stamp = None
        until(lambda:len(requests)==2)
        first,second=requests
        assert second['observation_id']>first['observation_id']
        assert second['observation_monotonic_ns']>handoff_time
        assert second['policy_epoch']>first['policy_epoch']
        replies[2].set();until(lambda:engine.ready is not None)
        assert engine.supervisor.state is HILState.POLICY
        started=time.monotonic_ns()
        engine.sample(started,np.zeros(16))
        np.testing.assert_allclose(engine.sample(started+150_000_000,np.zeros(16))[0],2.)
        assert engine.chunk_request_id==2 and len(checked)==6 and len(journal.chunks)==2
        assert not failures
    finally:
        for event in replies.values():event.set()
        if engine:engine.close()
        server.shutdown();server.server_close();thread.join(timeout=2)

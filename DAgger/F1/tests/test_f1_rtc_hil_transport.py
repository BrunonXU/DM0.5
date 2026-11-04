"""Loopback binary RTC + real network child; input here is synthetic, not F710."""
import json
import struct
import threading
import time

import numpy as np
import pytest
from dagger_core.hil_supervisor import HILState
from f1dagger.rtc_policy import RTCPolicy
from test_f1_rtc_hil import make_engine, start, send, until


@pytest.mark.parametrize('failure', [False, True])
def test_network_child_late_reply_and_fresh_handoff(failure):
    from websockets.sync.server import serve
    entered, release = threading.Event(), threading.Event()
    requests = []
    def handler(ws):
        for payload in ws:
            size, = struct.unpack('!I', payload[:4])
            meta = json.loads(payload[4:4+size]); requests.append(meta)
            assert [f['camera'] for f in meta['frames']] == ['head', 'left_wrist', 'right_wrist']
            if len(requests) == 1:
                entered.set(); assert release.wait(3)
            c = meta['rtc']
            response = dict(meta, rtc=True, rtc_context={k:c[k] for k in (
                'bootstrap','delay_steps','shift_steps','coverage_steps','plan_origin_ns','base_plan_id')},
                active_action_indices=[7,8,9,10,11,12,13,15], success=True,
                actions=[meta['last_submitted16']]*c['coverage_steps'], absolute_eef10=np.zeros((50,10)).tolist())
            if failure and len(requests) == 1:
                response.update(success=False, reason='ik_rejected', actions=None,
                                diagnostics=[dict(success=False, joints=None)])
            ws.send(json.dumps(response).encode())
    server = serve(handler, '127.0.0.1', 0, compression=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    e, _, journal = make_engine()
    client = RTCPolicy(dict(endpoint='http://127.0.0.1:'+str(server.socket.getsockname()[1]),
        timeout_s=2., max_age_s=3., chunk_steps=20, frequency_hz=30., shadow_only=True),
        'synthetic transport test', lambda a: None, held_getter=lambda:np.zeros(16))
    client.model_id, client.model_version, client.pixel_encoder = 'synthetic', 'v1', None
    e.policy = client
    try:
        start(e); until(e, entered.is_set)
        send(e, 500, 2); assert e.supervisor.state is HILState.HIL_LATCHED
        release.set(); until(e, lambda: e.real_future is None, toggle=2, seq=501)
        assert e.chunk is None and e.supervisor.state is HILState.HIL_LATCHED
        boundary = send(e, 900, 3)
        until(e, lambda: e.chunk is not None, toggle=3, seq=901)
        assert len(requests) == 2 and requests[1]['observation_monotonic_ns'] > boundary
        assert requests[1]['rtc']['bootstrap'] and requests[1]['rtc']['committed_actions16'] == []
        assert requests[1]['policy_epoch'] > requests[0]['policy_epoch']
        assert e.supervisor.state is HILState.POLICY
    finally:
        release.set(); e.close(); client.close(); server.shutdown(); thread.join(2)

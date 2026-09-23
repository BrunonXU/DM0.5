"""Loop fault paths with real response gates and synthetic robot/network I/O."""
from concurrent.futures import Future
from copy import deepcopy
from email import policy as email_policy
from email.parser import BytesParser
import json
from pathlib import Path
import sys
import time

import pytest

TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TASK / "robot"))
from f1_policy_client import runtime
from f1_policy_client.config import load_config
from f1_policy_client.observation import Observation
from f1_policy_client.policy import HttpPolicy
from f1_replay.bundle import ARM_NAMES, BODY_NAMES
from f1_replay.runtime import ReplayError


def test_request_preserves_image_bytes_and_fixed_anchor():
    observation = Observation(tuple([0.] * 14 + [50., 100., 0., 0., 0.]), (0.,) * 5,
                              {c: b"\x00image\xff" + c.encode() for c in ("head", "left", "right")}, 100, {})
    policy = HttpPolicy("http://127.0.0.1:7892", 1)
    captured = []
    policy._read = lambda request: captured.append(request) or {}
    policy.infer(observation, "r1")
    request = captured[0]
    message = BytesParser(policy=email_policy.default).parsebytes(
        ("Content-Type: " + request.get_header("Content-type") + "\r\n\r\n").encode() + request.data)
    parts = {part.get_param("name", header="Content-Disposition"): part.get_payload(decode=True)
             for part in message.iter_parts()}
    assert json.loads(parts.pop("metadata"))["state19"] == list(observation.state19)
    assert parts == observation.images


def test_pending_http_keeps_feedback_polling_and_timeout():
    pending = Future()
    ticks, clock = [], [0.]
    with pytest.raises(ReplayError, match="超时"):
        runtime.wait_request(pending, lambda: ticks.append(clock[0]), .02,
                             now=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay))
    assert len(ticks) > 2


def test_selected_checkpoint_rejects_swapped_endpoint_before_robot_open():
    selected = {"checkpoint": "/checkpoints/checkpoint-5000"}
    health = {"model_binding": {"checkpoint_identity": {"checkpoint": "/checkpoints/checkpoint-20000"}}}
    with pytest.raises(ReplayError, match="checkpoint与本入口不符"):
        runtime.check_selected_checkpoint(selected, health)
    health["model_binding"]["checkpoint_identity"]["checkpoint"] = selected["checkpoint"]
    runtime.check_selected_checkpoint(selected, health)


@pytest.fixture
def loop(monkeypatch, tmp_path):
    doc, profile = load_config()
    profile["inference_body_reference"]["body_positions_deg"] = [0.] * 5
    instances = []

    class Backend:
        def __init__(self, *args):
            self.commands_enabled = False
            self.enabled = 0
            self.sent = []
            self.closed = False
            self.last_publication = None
            instances.append(self)

        def open(self): pass
        def poll(self, _=0): pass
        def close(self): self.closed = True
        def enable_commands(self):
            self.commands_enabled = True
            self.enabled += 1
        def read_controller_parameters(self, **kwargs): return {"parameters": {}}
        def verify_base_parameters(self): return {"fixture": True}
        def snapshot(self):
            stamp = time.monotonic_ns()
            return {"joints": {n: {"position": 0., "received_ns": stamp} for n in ARM_NAMES + BODY_NAMES},
                    "grippers": {s: {"position": v, "received_ns": stamp} for s, v in (("left", 50.), ("right", 100.))},
                    "base": {"received_ns": stamp, "vx": 0., "vy": 0., "wz": 0.,
                             "is_soft_auto": False, "is_hard_auto": True, "is_avoid": False, "is_parking_mode": False},
                    "cameras": {c: {"received_ns": stamp, "data": b"image"} for c in ("head", "left", "right")}}
        def publish(self, action):
            self.sent.append(action)
            return {"sent": True}
        def stop_base(self): return {'zero_sent': True}

    class Policy:
        invalid = False
        failure = False
        def __init__(self, *args): pass
        def health(self): return {"model_binding": {"checkpoint": "fixture"}}
        def infer(self, observation, request_id):
            if self.failure:
                raise ReplayError("fixture HTTP disconnected")
            base = [[0., 0., 0.] for _ in range(20)]
            if self.invalid:
                base[-1][-1] = profile["base_speed_limits"]["wz"] + .01
            return {"contract": "f1_foam_inference_v1", "request_id": request_id,
                    "observation_monotonic_ns": observation.captured_ns,
                    "anchor_joints14_deg": list(observation.anchor_joints14),
                    "model_binding": {"checkpoint": "fixture"}, "ik_recipe_version": "f1_ikfk_demo_v1",
                    "frequency_hz": 30, "execute_steps": 20, "status": "ready",
                    "joint_targets_deg": [[0.] * 14 for _ in range(20)],
                    "gripper_targets": [[50., 100.] for _ in range(20)], "base_velocity_targets": base}

    class Recorder:
        def __init__(self, *args): pass
        def start(self, *args): return "fixture"
        def check(self): pass
        def close(self): return {"started": True}

    stops = []
    monkeypatch.setattr(runtime, "RosObservationBackend", Backend)
    monkeypatch.setattr(runtime, "HttpPolicy", Policy)
    monkeypatch.setattr(runtime, "Recorder", Recorder)
    monkeypatch.setattr(runtime, "check_controller_parameters", lambda *a: None)
    monkeypatch.setattr(runtime, "check_command_peers", lambda *a, **k: {})
    monkeypatch.setattr(runtime, "check_command_activity", lambda *a: None)
    monkeypatch.setattr(runtime, "check_execution_host", lambda *a: None)
    monkeypatch.setattr(runtime, "wait_for_enter", lambda *a: None)
    monkeypatch.setattr(runtime, "select_recording_topics", lambda *a: ([], {}))
    monkeypatch.setattr(runtime, "stop_motion", lambda *a: stops.append(True) or
                        {"zero_velocity_feedback_confirmed": True, "fresh_arm_hold_published": True})
    def schedule(bundle, send, tick, log, *args, **kwargs):
        for frame in bundle.frames:
            tick()
            send(frame.action)
    monkeypatch.setattr(runtime, "run_schedule", schedule)
    return doc, profile, tmp_path, instances, Policy, stops


def test_shadow_never_enables_commands_even_after_valid_ik(loop):
    doc, profile, out, backends, policy, stops = loop
    result = runtime.run(doc, profile, out, chunks=2)
    assert result["chunks_accepted"] == 2
    assert backends[0].enabled == 0 and not backends[0].sent and backends[0].closed
    assert not stops and not result["hardware_published"]


def test_raw20_ik_retry_uses_new_observation_and_never_dispatches_failed_prefix(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    doc['client'].update(inter_chunk_policy='sync_raw_h20_v1',ik_replan_retry_limit=3)
    infer=policy.infer;seen=[]
    def responses(self,observation,request_id):
        seen.append((request_id,observation.captured_ns))
        response=infer(self,observation,request_id)
        if len(seen)==1:
            response.update(status='rejected',reason='ik_rejected',ik={'step':4,'arm':0})
            # Even if invalid commands accompany a rejected response, none may run.
            response['joint_targets_deg']=[[999.]*14 for _ in range(20)]
        return response
    monkeypatch.setattr(policy,'infer',responses)
    result=runtime.run(doc,profile,out,powered=True,chunks=2)
    assert not result.get('error')
    assert result['chunks_rejected']==1 and result['chunks_accepted']==1
    assert result['ik_retries']==1 and result['completed_prefixes']==1
    assert result['native_points_published']==20 and len(backends[0].sent)==20
    assert all(action[0]==0 for action in backends[0].sent)
    assert seen[0][0]!=seen[1][0] and seen[0][1]<seen[1][1]
    assert result['stop_confirmation_waits']==0


def test_first_step_retry_discards_bad_prefix_and_uses_new_request(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    doc['client']['first_arm_retry_limit'] = 3
    infer = policy.infer
    seen = []
    def responses(self, observation, request_id):
        seen.append(request_id)
        response = infer(self, observation, request_id)
        if len(seen) == 1:
            response['joint_targets_deg'][0][0] = profile['inference_first_arm_max_delta_deg'] + .1
        return response
    monkeypatch.setattr(policy, 'infer', responses)
    result = runtime.run(doc, profile, out, powered=True, chunks=1)
    assert result.get('completed') and result['chunks_executed'] == 1
    assert result['first_arm_retries'] == 1 and result['chunks_requested'] == 2
    assert len(set(seen)) == 2 and len(backends[0].sent) == 20
    assert all(action[0] == 0 for action in backends[0].sent)


def test_repeated_first_step_failures_remain_bounded(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    doc['client']['first_arm_retry_limit'] = 3
    infer = policy.infer
    def response(self, observation, request_id):
        value = infer(self, observation, request_id)
        value['joint_targets_deg'][0][0] = profile['inference_first_arm_max_delta_deg'] + .1
        return value
    monkeypatch.setattr(policy, 'infer', response)
    result = runtime.run(doc, profile, out, powered=True, chunks=0)
    assert result['chunks_requested'] == 4 and result['first_arm_retries'] == 3
    assert 'FirstArmStepRejected' in result['error']
    assert not backends[0].sent


def test_first_step_retry_does_not_retry_other_errors(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    doc['client']['first_arm_retry_limit'] = 3
    policy.invalid = True
    result = runtime.run(doc, profile, out, powered=True, chunks=0)
    assert result['chunks_requested'] == 1 and result['first_arm_retries'] == 0
    assert not backends[0].sent and result.get('error')


@pytest.mark.parametrize('missing_confirmation', [
    'zero_velocity_feedback_confirmed', 'fresh_arm_hold_published'])
def test_first_step_retry_requires_confirmed_stop(loop, monkeypatch, missing_confirmation):
    doc, profile, out, backends, policy, stops = loop
    doc['client']['first_arm_retry_limit'] = 3
    infer = policy.infer
    def response(self, observation, request_id):
        value = infer(self, observation, request_id)
        value['joint_targets_deg'][0][0] = profile['inference_first_arm_max_delta_deg'] + .1
        return value
    monkeypatch.setattr(policy, 'infer', response)
    def unconfirmed_stop(*args):
        result = {'zero_velocity_feedback_confirmed': True,
                  'fresh_arm_hold_published': True, 'errors': []}
        result[missing_confirmation] = False
        return result
    monkeypatch.setattr(runtime, 'stop_motion', unconfirmed_stop)
    result = runtime.run(doc, profile, out, powered=True, chunks=0)
    assert result['chunks_requested'] == 1 and result['first_arm_retries'] == 0
    assert '首步重采前停车/保持未确认' in result['error']
    assert not backends[0].sent


def test_recorder_can_discover_publishers_before_first_command(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    checked = []
    def start(recorder, tick, backend):
        assert backend.commands_enabled
        assert backend.sent == []
        tick()
        checked.append(True)
        return "fixture ready"
    monkeypatch.setattr(runtime.Recorder, "start", start)
    result = runtime.run(doc, profile, out, powered=True, chunks=1)
    assert checked and result["chunks_executed"] == 1
    assert len(backends[0].sent) == 20


def test_recorder_not_ready_never_dispatches_policy_actions(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    def start(recorder, tick, backend):
        assert backend.commands_enabled and not backend.sent
        raise ReplayError("fixture recorder not ready")
    monkeypatch.setattr(runtime.Recorder, "start", start)
    result = runtime.run(doc, profile, out, powered=True, chunks=1)
    assert "recorder not ready" in result["error"]
    assert result["chunks_requested"] == result["chunks_executed"] == 0
    assert not backends[0].sent and len(stops) == 1 and backends[0].closed


def test_expired_response_is_discarded_then_new_observation_executes(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    original = runtime.prepare_prefix
    requests = []
    def prepare(response, observation, *args, **kwargs):
        requests.append((response["request_id"], observation.captured_ns))
        if len(requests) == 1:
            raise runtime.ObservationExpired("fixture age=2.66s, budget=2s")
        return original(response, observation, *args, **kwargs)
    monkeypatch.setattr(runtime, "prepare_prefix", prepare)
    result = runtime.run(doc, profile, out, powered=True, chunks=1)
    assert result["completed"] and result["chunks_executed"] == 1
    assert result["chunks_requested"] == 2 and result["expired_responses"] == 1
    assert requests[0][0] != requests[1][0] and requests[0][1] < requests[1][1]
    assert len(backends[0].sent) == 20 and len(stops) == 2


def test_persistent_expiration_is_bounded_and_never_dispatches(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    def prepare(*args, **kwargs):
        raise runtime.ObservationExpired("fixture late")
    monkeypatch.setattr(runtime, "prepare_prefix", prepare)
    result = runtime.run(doc, profile, out, powered=True, chunks=0)
    assert result["chunks_requested"] == result["expired_responses"] == 3
    assert result["chunks_executed"] == 0 and not backends[0].sent
    assert "连续3次观测过期" in result["error"] and len(stops) == 1


def test_fresh_prefix_resets_consecutive_expiration_budget(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    original, count = runtime.prepare_prefix, [0]
    def prepare(*args, **kwargs):
        count[0] += 1
        if count[0] % 3:
            raise runtime.ObservationExpired("fixture late")
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, "prepare_prefix", prepare)
    result = runtime.run(doc, profile, out, powered=True, chunks=2)
    assert result["completed"] and result["chunks_executed"] == 2
    assert result["expired_responses"] == 4 and result["chunks_requested"] == 6


def test_shadow_collects_rejections_without_clipping(loop):
    doc, profile, out, backends, policy, stops = loop
    policy.invalid = True
    result = runtime.run(doc, profile, out, chunks=2)
    assert result["chunks_rejected"] == 2
    assert len(result["rejection_reasons"]) == 1
    assert not backends[0].sent and not stops


def test_execute_stops_between_chunks_and_on_exit(loop):
    doc, profile, out, backends, policy, stops = loop
    result = runtime.run(doc, profile, out, powered=True, chunks=2)
    assert result["chunks_executed"] == 2
    assert len(backends[0].sent) == 40 and backends[0].enabled == 1
    assert len(stops) == 3 and result["recording"]["started"]


def test_bad_last_step_dispatches_no_model_action_and_stops(loop):
    doc, profile, out, backends, policy, stops = loop
    policy.invalid = True
    result = runtime.run(doc, profile, out, powered=True)
    assert result["chunks_rejected"] == 1 and result["error"]
    assert not backends[0].sent and len(stops) == 1


def test_http_disconnect_stops_before_exit(loop):
    doc, profile, out, backends, policy, stops = loop
    policy.failure = True
    result = runtime.run(doc, profile, out, powered=True)
    assert "disconnected" in result["error"]
    assert not backends[0].sent and len(stops) == 1 and backends[0].closed


def test_body_reference_mismatch_prevents_enable(loop):
    doc, profile, out, backends, policy, stops = loop
    profile["inference_body_reference"]["body_positions_deg"][3] = -20
    result = runtime.run(doc, profile, out, powered=True)
    assert result["error"] and backends[0].enabled == 0 and not stops


def test_log_failure_cannot_interrupt_stop_or_close(loop, monkeypatch):
    doc, profile, out, backends, policy, stops = loop
    original = runtime.Journal
    class FailingJournal(original):
        def __call__(self, event, fields):
            if event in ("chunk_accepted", "run_error", "run_end", "stop_result"):
                raise OSError("fixture disk full")
            return super().__call__(event, fields)
    monkeypatch.setattr(runtime, "Journal", FailingJournal)
    result = runtime.run(doc, profile, out, powered=True)
    assert "disk full" in result["error"] and result["logging_errors"]
    assert len(stops) == 1 and backends[0].closed and result["recording"]["started"]


def test_wait_base_settled_restarts_stability_after_transient_zero():
    clock=[10_000_000_000];events=[];samples=[]
    profile=dict(stop_timeout_s=3.,stop_publish_hz=20.,feedback_max_age_s=.5,
                 base_stopped_linear_m_s=.01,base_stopped_angular_rad_s=.02)
    class Backend:
        def stop_base(self):return {'zero_sent':True}
        def snapshot(self):
            # First zero is followed by three moving samples, then sustained stop.
            speed=0.03 if len(samples) in (1,2,3) else 0.
            samples.append(speed)
            return {'base':dict(received_ns=clock[0],vx=0.,vy=speed,wz=0.)}
    result=runtime.wait_base_settled(Backend(),profile,lambda:None,lambda e,v:events.append((e,v)),
        now=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+round(s*1e9)))
    assert result['elapsed_s']>=.5 and result['stable_s']>=.25
    assert samples[:4]==[0.,.03,.03,.03]
    assert events[-1][0]=='inter_chunk_base_settled'


@pytest.mark.parametrize('fault',['never_stops','frozen_feedback','missing_feedback','zero_not_sent','cancelled'])
def test_settle_wait_cannot_turn_missing_or_moving_feedback_into_success(fault):
    clock=[10_000_000_000];stamp=clock[0]+50_000_000
    profile=dict(stop_timeout_s=.8,stop_publish_hz=20.,feedback_max_age_s=.5,
                 base_stopped_linear_m_s=.01,base_stopped_angular_rad_s=.02)
    class Backend:
        def stop_base(self):return None if fault=='zero_not_sent' else {'zero_sent':True}
        def snapshot(self):
            if fault=='missing_feedback':return {}
            return {'base':dict(received_ns=stamp if fault=='frozen_feedback' else clock[0],
                                vx=.04 if fault=='never_stops' else 0.,vy=0.,wz=0.)}
    def tick():
        if fault=='cancelled':raise runtime.ReplayInterrupted('fixture cancel')
    with pytest.raises((ReplayError,runtime.ReplayInterrupted)):
        runtime.wait_base_settled(Backend(),profile,tick,lambda *a:None,
            now=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+round(s*1e9)))
    assert clock[0]<=10_800_000_000


@pytest.mark.parametrize('execution_mode', ['async_native_h50_v1','sync_raw_h50_v1','sync_raw_h20_v1'])
def test_native_mode_switches_chunks_without_any_inter_chunk_stop(loop,monkeypatch,execution_mode):
    doc,profile,out,backends,policy,stops=loop
    steps=20 if execution_mode=='sync_raw_h20_v1' else 50
    doc['client'].update(inter_chunk_policy=execution_mode,execute_steps=steps,
                         prefetch_lookahead_steps=35,min_fresh_steps=12)
    original=policy.infer
    def native_infer(self,observation,request_id):
        r=original(self,observation,request_id);r.update(execute_steps=steps,execution_mode='async_native_h50_v1')
        for key in ('joint_targets_deg','gripper_targets','base_velocity_targets'):r[key]=(r[key]*3)[:steps]
        return r
    monkeypatch.setattr(policy,'infer',native_infer)
    monkeypatch.setattr(policy,'health',lambda self:{'model_binding':{'checkpoint':'fixture'},'execution_mode':'async_native_h50_v1' if steps==50 else None})
    def forbidden(*a,**k):raise AssertionError('native must not wait for stopped feedback between chunks')
    monkeypatch.setattr(runtime,'wait_base_settled',forbidden)
    result=runtime.run(doc,profile,out,powered=True,chunks=3)
    assert result.get('completed'),result
    assert result['chunks_executed']==3 and len(stops)==1
    assert result['stop_confirmation_waits']==0
    if execution_mode=='async_native_h50_v1':
        assert result['native_switches']==2 and result['forced_inter_chunk_stops']==0
    else:
        assert result['native_switches']==0 and result['native_points_published']==3*steps
        assert result['completed_prefixes']==3 and result['forced_inter_chunk_stops']==2
        assert result['full_horizons_executed']==(3 if steps==50 else 0)
    events=[json.loads(l) for l in (out/'events.jsonl').read_text().splitlines()]
    accepted=[e for e in events if e['event']=='chunk_accepted']
    assert len(accepted)==3
    if execution_mode=='async_native_h50_v1':assert accepted[1]['start_index']>0
    else:assert all(e['start_index']==0 and e['fresh_steps']==steps for e in accepted)
    assert not any(e['event']=='inter_chunk_stop' for e in events)
    assert all((out/e['request_id']/'response.json').exists() for e in accepted)
